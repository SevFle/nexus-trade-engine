from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException

from engine.deps import get_db
from engine.legal import service as legal_service

if TYPE_CHECKING:

    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

#: HTTP status for "Unavailable For Legal Reasons" — returned when the
#: configured user must (re-)accept one or more legal documents before
#: they are allowed to use a consent-gated endpoint.
LEGAL_REACCEPT_STATUS = 451

#: Placeholder principal used to evaluate pending legal re-acceptances while
#: the real auth dependency (``engine.api.auth.dependency.get_current_user``)
#: is not yet wired into the consent-gated routers.
#:
#: ``None`` ⇒ :func:`require_legal_acceptance` is a no-op and returns
#: immediately without touching the database. This keeps the public
#: read-only routes working pre-auth and matches the suite-wide conftest
#: override. Once a value is set (by the auth chain once it is wired in,
#: or by a test) it stands in for the authenticated principal solely for
#: the purpose of computing pending documents via
#: :func:`engine.legal.service.get_pending_acceptances`.
#:
#: Tracked separately as the consent-enforcement follow-up: when
#: ``get_current_user`` is wired into the routers this sentinel is
#: replaced by the resolved user id.
_placeholder_user_id: uuid.UUID | None = None


async def require_legal_acceptance(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    """Enforce that the configured user has accepted the current version of
    every legal document that requires acceptance.

    Resolved as a FastAPI dependency on the consent-gated routers
    (backtest, portfolio, strategies, marketplace, market-data, scoring).

    Behaviour:

    * When :data:`_placeholder_user_id` is ``None`` the dependency is a
      no-op and returns ``None`` immediately without touching the
      database. This is the current state — the real auth dependency
      (:func:`engine.api.auth.dependency.get_current_user`) is not yet
      wired into these routers, so until that happens the public
      read-only routes must keep working.
    * When :data:`_placeholder_user_id` is set, the stored acceptance
      (``document_version`` + ``accepted_at`` per user, in the existing
      ``legal_acceptances`` table — no schema migration involved) is
      compared against each required document's ``current_version`` via
      :func:`engine.legal.service.get_pending_acceptances`. If every
      required document is satisfied the dependency is a no-op and the
      request proceeds; otherwise it raises HTTP 451 listing the pending
      document slugs.
    """
    if _placeholder_user_id is None:
        return

    pending = await legal_service.get_pending_acceptances(db, _placeholder_user_id)
    if pending:
        raise HTTPException(
            status_code=LEGAL_REACCEPT_STATUS,
            detail={
                "code": "legal_re_acceptance_required",
                "documents": [p.slug for p in pending],
            },
        )
    return
