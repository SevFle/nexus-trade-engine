"""Account deletion lifecycle — GDPR Art. 17 right to erasure (gh#157).

Deletion is **not** an immediate hard-delete. The flow is:

1. ``request_deletion`` — records a DSRequest of kind ``delete``,
   status ``pending``. The user account stays usable during a
   grace period (30 days by default) so accidental deletions can
   be undone.
2. The user can call ``cancel_deletion`` at any time during the
   grace window. The DSR row transitions to ``cancelled``.
3. After ``DELETION_GRACE_DAYS``, an offline operator job (not in
   this PR) consumes the still-pending row and performs the
   audit-chain-preserving anonymization described in the spec.

The actual purge / anonymization is intentionally out of scope here:
it touches every domain table and needs careful audit-chain handling.
This module only owns the *intent* and the timer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from engine.db.models import DSRequest
from engine.privacy.dsr import record_request, transition

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


DELETION_GRACE_DAYS: int = 30


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
        raise DeletionError(
            f"user already has an active deletion request ({existing.id})"
        )
    return await record_request(
        session,
        user_id=user_id,
        kind="delete",
        note=note,
        details={"grace_days": DELETION_GRACE_DAYS},
        sla_days=DELETION_GRACE_DAYS,
    )


async def cancel_deletion(session: AsyncSession, *, user_id: uuid.UUID) -> DSRequest:
    """Cancel the active deletion request for ``user_id``.

    Returns the cancelled row. Raises if there is nothing to cancel.
    """
    existing = await _find_active_deletion(session, user_id)
    if existing is None:
        raise DeletionError("no active deletion request to cancel")
    return await transition(session, existing, status="cancelled")


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


async def is_due_for_purge(
    session: AsyncSession, user_id: uuid.UUID
) -> bool:
    """Return True iff a pending deletion's grace window has elapsed.

    The actual purge job is not in this module; this is the predicate
    that job will consume.
    """
    row = await _find_active_deletion(session, user_id)
    if row is None:
        return False
    return row.sla_due_at <= datetime.now(tz=UTC)


async def _find_active_deletion(
    session: AsyncSession, user_id: uuid.UUID
) -> DSRequest | None:
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
