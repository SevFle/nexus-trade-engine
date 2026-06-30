"""Account deletion lifecycle — GDPR Art. 17 right to erasure (gh#157).

Deletion is **not** an immediate hard-delete. The flow is:

1. ``request_deletion`` — records a DSRequest of kind ``delete`` with
   status ``pending`` and creates a :class:`DeletionSchedule` row, and
   disables the account (``is_active=False``). The user keeps their
   data during a grace period (30 days by default) so accidental
   deletions can be undone.
2. The user can call ``cancel_deletion`` at any time during the grace
   window. The DSR row transitions to ``cancelled``, the schedule to
   ``cancelled``, and the account is reactivated.
3. After ``DELETION_GRACE_DAYS`` an operator job consumes the
   still-scheduled row and runs :func:`anonymize_user` — the
   audit-chain-preserving anonymization described in the spec. The
   user row is tombstoned (PII replaced) rather than hard-deleted so
   referentially-protected legal/audit rows survive intact; owned
   domain data (portfolios, backtests, strategies, webhooks, API
   keys, sessions) is deleted. Legal retention exceptions (e.g. trade
   records for 7 years, governed by gh#90) are recorded on the
   schedule for the periodic review job.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from engine.db.models import (
    ApiKey,
    BacktestResult,
    ConsentRecord,
    DeletionSchedule,
    DSRequest,
    Portfolio,
    RefreshToken,
    User,
    WebhookConfig,
)
from engine.privacy.dsr import record_request, transition

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


DELETION_GRACE_DAYS: int = 30

#: Records kept past deletion for legal reasons. Values are dataset
#: names; the periodic review job (gh#90 retention) consumes them.
#: ``legal_acceptances`` is structurally retained because its FK to
#: ``users`` is ``ondelete=RESTRICT`` and the user row is tombstoned,
#: not deleted.
DEFAULT_RETENTION_EXCEPTIONS: dict[str, str] = {
    "legal_acceptances": "legal compliance — acceptance audit trail",
    "trade_records": "tax compliance — 7-year retention (gh#90)",
}


class DeletionError(Exception):
    """Raised on invalid deletion-flow transitions (e.g., double request)."""


async def request_deletion(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    note: str | None = None,
) -> DSRequest:
    """Initiate the deletion grace period for ``user_id``.

    Refuses if the user already has an active (pending or in_progress)
    deletion request — operators should cancel that first.
    """
    existing = await _find_active_deletion(session, user_id)
    if existing is not None:
        raise DeletionError(f"user already has an active deletion request ({existing.id})")
    request = await record_request(
        session,
        user_id=user_id,
        kind="delete",
        note=note,
        details={"grace_days": DELETION_GRACE_DAYS},
        sla_days=DELETION_GRACE_DAYS,
    )
    # Disable the account for the duration of the grace window so no
    # further processing occurs, but keep the data so the deletion is
    # reversible until the purge runs.
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is not None:
        user.is_active = False
    await session.flush()
    await schedule_deletion(session, request=request)
    return request


async def cancel_deletion(session: AsyncSession, *, user_id: uuid.UUID) -> DSRequest:
    """Cancel the active deletion request for ``user_id``.

    Reactivates the account, cancels the schedule, and transitions the
    DSR to ``cancelled``. Raises if there is nothing to cancel.
    """
    existing = await _find_active_deletion(session, user_id)
    if existing is None:
        raise DeletionError("no active deletion request to cancel")
    request = await transition(session, existing, status="cancelled")
    # Reactivate the account and tear down the pending schedule.
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is not None:
        user.is_active = True
    schedule = await _find_schedule(session, existing.id)
    if schedule is not None and schedule.status == "scheduled":
        schedule.status = "cancelled"
    await session.flush()
    return request


async def is_pending_deletion(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[bool, datetime | None]:
    """Return ``(pending?, sla_due_at)`` for the user.

    Useful for UI surfaces and middleware that want to warn / restrict.
    """
    row = await _find_active_deletion(session, user_id)
    if row is None:
        return False, None
    return True, row.sla_due_at


async def is_due_for_purge(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Return True iff a pending deletion's grace window has elapsed.

    The actual purge job is not in this module; this is the predicate
    that job will consume.
    """
    row = await _find_active_deletion(session, user_id)
    if row is None:
        return False
    return row.sla_due_at <= datetime.now(tz=UTC)


async def _find_active_deletion(session: AsyncSession, user_id: uuid.UUID) -> DSRequest | None:
    result = await session.execute(
        select(DSRequest)
        .where(
            DSRequest.user_id == user_id,
            DSRequest.kind == "delete",
            DSRequest.status.in_(("pending", "in_progress")),
        )
        .order_by(DSRequest.created_at.desc())
    )
    return result.scalars().first()


def remaining_grace(now: datetime, sla_due_at: datetime) -> timedelta:
    """Helper for UI countdown displays."""
    delta = sla_due_at - now
    return delta if delta.total_seconds() > 0 else timedelta(0)


# ---------------------------------------------------------------------------
# Scheduling + purge
# ---------------------------------------------------------------------------


@dataclass
class AnonymizationResult:
    """Outcome of :func:`anonymize_user` — what was purged and what stayed."""

    user_id: uuid.UUID
    anonymized_label: str
    purged: dict[str, int] = field(default_factory=dict)
    retention_exceptions: dict[str, str] = field(default_factory=dict)
    dsr_request_id: uuid.UUID | None = None
    schedule_id: uuid.UUID | None = None


async def schedule_deletion(
    session: AsyncSession,
    *,
    request: DSRequest,
    retention_exceptions: dict[str, str] | None = None,
) -> DeletionSchedule:
    """Create the post-grace anonymization schedule for a deletion request.

    Idempotent: returns the existing schedule if one already exists for
    the request. The purge date mirrors the DSR's SLA due date.
    """
    existing = await _find_schedule(session, request.id)
    if existing is not None:
        return existing

    schedule = DeletionSchedule(
        user_id=request.user_id,
        dsr_request_id=request.id,
        scheduled_for=request.sla_due_at,
        status="scheduled",
        retention_exceptions=dict(retention_exceptions or DEFAULT_RETENTION_EXCEPTIONS),
    )
    session.add(schedule)
    await session.flush()
    return schedule


async def anonymize_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    dsr_request_id: uuid.UUID | None = None,
) -> AnonymizationResult:
    """Perform the audit-chain-preserving purge for ``user_id`` (gh#157).

    - Tombstones the user row (PII replaced with a stable
      ``anonymized:<hash>`` marker). The row is **kept** because legal
      acceptances reference it via a ``RESTRICT`` foreign key and the
      privacy audit trail must remain referentially intact.
    - Deletes owned domain data: portfolios (cascading to positions,
      orders, tax lots, installed strategies), backtests tied to those
      portfolios, webhooks, API keys, refresh tokens, and consents.
    - Records retention exceptions (e.g. trade records, legal
      acceptances) on the schedule for the periodic review job.

    Raises ``LookupError`` if the user does not exist.
    """
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise LookupError(f"user not found: {user_id}")

    user_hash = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]
    label = f"anonymized:{user_hash}"
    now = datetime.now(tz=UTC)

    purged: dict[str, int] = {}

    # Owned domain data. Portfolios cascade to positions / orders /
    # tax lots / installed strategies via ondelete=CASCADE.
    portfolio_ids = [
        row.id
        for row in (
            await session.execute(select(Portfolio.id).where(Portfolio.user_id == user_id))
        ).all()
    ]
    if portfolio_ids:
        await session.execute(
            delete(BacktestResult).where(BacktestResult.portfolio_id.in_(portfolio_ids))
        )
        result = await session.execute(delete(Portfolio).where(Portfolio.user_id == user_id))
        purged["portfolios"] = result.rowcount or 0

    result = await session.execute(delete(WebhookConfig).where(WebhookConfig.user_id == user_id))
    purged["webhooks"] = result.rowcount or 0
    result = await session.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
    purged["api_keys"] = result.rowcount or 0
    result = await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
    purged["refresh_tokens"] = result.rowcount or 0
    result = await session.execute(delete(ConsentRecord).where(ConsentRecord.user_id == user_id))
    purged["consents"] = result.rowcount or 0

    # Tombstone the user row (kept for referential integrity).
    user.email = f"{label}@anonymized.local"
    user.hashed_password = None
    user.display_name = "Deleted User"
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.mfa_backup_codes = None
    user.external_id = None
    user.is_active = False
    user.processing_restricted = False
    user.updated_at = now
    await session.flush()

    # Close out the schedule + DSR (if provided) so the audit trail is
    # consistent. Retention exceptions are surfaced for the review job.
    schedule_id: uuid.UUID | None = None
    retention_exceptions = dict(DEFAULT_RETENTION_EXCEPTIONS)
    if dsr_request_id is not None:
        request = (
            await session.execute(select(DSRequest).where(DSRequest.id == dsr_request_id))
        ).scalar_one_or_none()
        if request is not None:
            await transition(session, request, status="completed")
        schedule = await _find_schedule(session, dsr_request_id)
        if schedule is not None:
            schedule.status = "purged"
            schedule.purged_at = now
            schedule.anonymized_label = label
            schedule.retention_exceptions = retention_exceptions
            schedule_id = schedule.id

    return AnonymizationResult(
        user_id=user_id,
        anonymized_label=label,
        purged=purged,
        retention_exceptions=retention_exceptions,
        dsr_request_id=dsr_request_id,
        schedule_id=schedule_id,
    )


async def list_due_schedules(
    session: AsyncSession, *, now: datetime | None = None
) -> list[DeletionSchedule]:
    """Return all schedules past their purge date that are still pending."""
    now = now or datetime.now(tz=UTC)
    result = await session.execute(
        select(DeletionSchedule)
        .where(
            DeletionSchedule.status == "scheduled",
            DeletionSchedule.scheduled_for <= now,
        )
        .order_by(DeletionSchedule.scheduled_for)
    )
    return list(result.scalars().all())


async def process_due_deletions(
    session: AsyncSession, *, now: datetime | None = None
) -> list[AnonymizationResult]:
    """Run :func:`anonymize_user` for every schedule past its purge date.

    This is the body of the operator purge job. The caller commits.
    """
    return [
        await anonymize_user(session, schedule.user_id, dsr_request_id=schedule.dsr_request_id)
        for schedule in await list_due_schedules(session, now=now)
    ]


async def _find_schedule(
    session: AsyncSession, dsr_request_id: uuid.UUID
) -> DeletionSchedule | None:
    result = await session.execute(
        select(DeletionSchedule).where(DeletionSchedule.dsr_request_id == dsr_request_id)
    )
    return result.scalars().first()
