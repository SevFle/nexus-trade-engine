"""Async repository for Legal Gate acceptance records.

Two data-access operations back the Legal Gate acceptance-tracking slice:

* :func:`record_acceptance` — append a new audit row and return it.
* :func:`get_latest_acceptance` — return the most recent (by ``accepted_at``)
  acceptance for a user, or ``None`` when the user has never accepted.

Both are pure data-access coroutines: no business-rule decisions and no HTTP
concerns. The routes layer composes them with authentication and config
(``settings.legal_terms_version``) to build the ``/api/legal/*`` endpoints.

Design notes
------------

* **Append-only.** Every acceptance is retained so the audit trail is never
  lossy; "current acceptance" is always derived via
  :func:`get_latest_acceptance`. This is the legally defensible choice for a
  consent log.
* **Flush, not commit.** :func:`record_acceptance` only ``flush``es the row
  so it is visible to any same-request follow-up read. The surrounding
  ``get_db`` dependency owns the transaction commit (or rollback on error).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from engine.legal.models import LegalAcceptance

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def record_acceptance(
    db: AsyncSession,
    user_id: str,
    document_version: str,
    ip_address: str,
    *,
    accepted_at: datetime | None = None,
) -> LegalAcceptance:
    """Persist a new acceptance audit row and return it.

    Parameters
    ----------
    db:
        Active async session. Only ``flush``ed — the caller (typically the
        ``get_db`` dependency) is responsible for committing.
    user_id:
        Stable identifier of the accepting user.
    document_version:
        Version of the legal document being accepted.
    ip_address:
        Client IP captured at acceptance time.
    accepted_at:
        Optional explicit timestamp (UTC). Defaults to now; injectable for
        deterministic tests.

    Raises
    ------
    ValueError
        If ``user_id`` or ``document_version`` is empty, or ``ip_address`` is
        ``None`` — these are programmer errors, not user input.
    """
    if not user_id:
        raise ValueError("user_id must be a non-empty string")
    if not document_version:
        raise ValueError("document_version must be a non-empty string")
    if ip_address is None:
        raise ValueError("ip_address is required")

    record = LegalAcceptance(
        user_id=user_id,
        document_version=document_version,
        ip_address=ip_address,
        accepted_at=accepted_at or datetime.now(tz=UTC),
    )
    db.add(record)
    await db.flush()
    await db.refresh(record)
    return record


async def get_latest_acceptance(
    db: AsyncSession,
    user_id: str,
) -> LegalAcceptance | None:
    """Return the most recent acceptance for ``user_id``, or ``None``.

    "Most recent" is defined by ``accepted_at`` descending. Ties on the
    timestamp are broken by ``id`` descending so the result is deterministic
    even when two rows share an identical ``accepted_at``.
    """
    stmt = (
        select(LegalAcceptance)
        .where(LegalAcceptance.user_id == user_id)
        .order_by(LegalAcceptance.accepted_at.desc(), LegalAcceptance.id.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
