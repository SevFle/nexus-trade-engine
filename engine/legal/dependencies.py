from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, params, status

from engine.api.auth.dependency import get_current_user
from engine.deps import get_db
from engine.legal import service as legal_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from engine.db.models import User


async def require_legal_acceptance(
    db: AsyncSession = Depends(get_db),  # noqa: B008
    principal: User | None = Depends(get_current_user),  # noqa: B008
) -> None:
    """Reject the request unless the authenticated user has accepted every
    required legal document at its current version.

    Principal resolution:

    * **Through FastAPI dependency injection** the ``principal`` is resolved by
      :func:`get_current_user`, which raises HTTP 401 itself when no credential
      is present.
    * **Invoked directly** (bypassing DI) ``principal`` is left as the
      unresolved ``Depends`` marker. Both this and an explicitly-resolved
      ``None`` principal mean there is no authenticated user, so the
      dependency surfaces a deterministic HTTP 401 rather than silently
      bypassing consent enforcement.

    The ``principal`` guard below is **authoritative**, not a redundant
    belt-and-braces check. It is what makes the dependency safe to call
    outside FastAPI's DI machinery (e.g. by routes that invoke it by hand or
    in unit tests): without it an unresolved ``Depends`` marker would leak
    into :func:`legal_service.get_pending_acceptances` and explode with a 500
    on an attribute access against the marker, while a ``None`` principal
    would silently skip consent enforcement. Treat any change to this guard
    with care.

    A pending re-acceptance raises HTTP 451
    (:attr:`status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS`) with a structured
    detail body listing the offending document slugs. When everything is in
    order the function returns ``None``.
    """
    # Authoritative authentication guard: an explicitly-resolved ``None``
    # principal *or* an unresolved ``Depends`` marker (a hand-rolled call that
    # bypassed DI) both mean "no authenticated user". Reject with 401 in either
    # case instead of letting a marker reach the pending-acceptance lookup and
    # 500 on ``principal.id``.
    if principal is None or isinstance(principal, params.Depends):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    pending = await legal_service.get_pending_acceptances(db, principal.id)
    if pending:
        raise HTTPException(
            status_code=status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
            detail={
                "code": "legal_re_acceptance_required",
                "documents": [p.slug for p in pending],
            },
        )
