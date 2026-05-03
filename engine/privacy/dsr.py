"""DSR registry — recording and listing data-subject requests (gh#157).

A DSRequest row is the audit trail for every privacy request the engine
handles. The registry is intentionally simple: record on intake, list
per-user, transition status as work progresses.

GDPR Art. 12 obliges responding within one month of receipt; the SLA
timer is set on insert and never extended. Operators who need to extend
must add a note and either complete or fail the request before the new
deadline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from engine.db.models import DSRequest

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


DSR_KINDS: frozenset[str] = frozenset({"export", "delete", "rectify", "restrict", "object"})

DSR_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

SLA_DEFAULT_DAYS: int = 30


async def record_request(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: str,
    note: str | None = None,
    details: dict | None = None,
    sla_days: int = SLA_DEFAULT_DAYS,
) -> DSRequest:
    """Record a fresh DSR. Returns the new row (caller commits)."""
    if kind not in DSR_KINDS:
        raise ValueError(f"unknown DSR kind: {kind!r}")
    if sla_days <= 0:
        raise ValueError("sla_days must be positive")

    row = DSRequest(
        user_id=user_id,
        kind=kind,
        status="pending",
        note=note,
        details=details or {},
        sla_due_at=datetime.now(tz=UTC) + timedelta(days=sla_days),
    )
    session.add(row)
    await session.flush()
    return row


async def list_user_requests(session: AsyncSession, user_id: uuid.UUID) -> list[DSRequest]:
    result = await session.execute(
        select(DSRequest).where(DSRequest.user_id == user_id).order_by(DSRequest.created_at.desc())
    )
    return list(result.scalars().all())


async def transition(
    session: AsyncSession,
    request: DSRequest,
    *,
    status: str,
) -> DSRequest:
    """Move a request to a new status. Idempotent on terminal states."""
    if status not in {"pending", "in_progress", *DSR_TERMINAL_STATUSES}:
        raise ValueError(f"unknown DSR status: {status!r}")
    if request.status in DSR_TERMINAL_STATUSES and request.status == status:
        return request
    request.status = status
    now = datetime.now(tz=UTC)
    if status == "completed":
        request.completed_at = now
    elif status == "cancelled":
        request.cancelled_at = now
    await session.flush()
    return request
