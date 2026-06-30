from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, status

from engine.api.auth.dependency import get_current_user
from engine.deps import get_db
from engine.legal import service as legal_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from engine.db.models import User

#: HTTP 451 — Unavailable For Legal Reasons (RFC 7725). Kept as a named
#: constant so the status code reads as intent rather than a magic number.
_LEGAL_RE_ACCEPTANCE_REQUIRED = 451


async def require_legal_acceptance(
    db: AsyncSession = Depends(get_db),  # noqa: B008
    principal: User | None = Depends(get_current_user),  # noqa: B008
) -> None:
    """Reject the request unless the authenticated user has accepted every
    required legal document at its current version.

    Principal resolution:

    * **Through FastAPI dependency injection** the ``principal`` is resolved by
      :func:`get_current_user`, which raises HTTP 401 itself when no valid
      credential is present.
    * **Invoked outside DI** (e.g. in a unit test) with an explicitly-resolved
      ``None`` principal, this function **fails closed**: it raises HTTP 401
      unconditionally. There is no global placeholder, fail-open escape hatch,
      or ``Depends``-marker fallback.

    A pending re-acceptance raises HTTP 451 with a structured detail body
    listing the offending document slugs. When everything is in order the
    function returns ``None``.
    """
    # Fail closed: an unresolved principal is never permitted through, whether
    # the principal arrived unresolved via DI or was explicitly passed as
    # ``None``. Authentication is always required — there is no bypass.
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    pending = await legal_service.get_pending_acceptances(db, principal.id)
    if pending:
        raise HTTPException(
            status_code=_LEGAL_RE_ACCEPTANCE_REQUIRED,
            detail={
                "code": "legal_re_acceptance_required",
                "documents": [p.slug for p in pending],
            },
        )
