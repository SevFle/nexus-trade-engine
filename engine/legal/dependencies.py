from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, params, status

from engine.api.auth.dependency import get_current_user
from engine.deps import get_db
from engine.legal import service as legal_service

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from engine.db.models import User

# Stand-in for the authenticated principal used by consent enforcement until
# ``get_current_user`` is wired through every route. Routes that have not yet
# been integrated with the auth dependency call ``require_legal_acceptance``
# directly, so the ``principal`` parameter arrives as an *unresolved* FastAPI
# ``Depends`` marker. In that situation we fall back to this module-level id.
# When it is ``None`` the dependency is a deliberate no-op — this is what lets
# the public read-only routes work pre-auth. Tests exercise the 451/200 paths
# with ``monkeypatch.setattr(dependencies, "_placeholder_user_id", value)``.
_placeholder_user_id: UUID | None = None


async def require_legal_acceptance(
    db: AsyncSession = Depends(get_db),  # noqa: B008
    principal: User | None = Depends(get_current_user),  # noqa: B008
) -> None:
    """Reject the request unless the authenticated user has accepted every
    required legal document at its current version.

    Principal resolution:

    * **Through FastAPI dependency injection** the ``principal`` is resolved by
      :func:`get_current_user`, which raises HTTP 401 itself when no credential
      is present. We keep a defensive guard so the function is safe to invoke
      even outside DI: an explicitly-resolved ``None`` principal surfaces a
      deterministic 401.
    * **Invoked directly** (bypassing DI) ``principal`` is left as the
      unresolved ``Depends`` marker. In that case we fall back to
      :data:`_placeholder_user_id`. When even that is unset the dependency is a
      deliberate no-op so public/read-only routes keep working pre-auth; once
      it is set, a pending re-acceptance surfaces as HTTP 451.

    A pending re-acceptance raises HTTP 451 with a structured detail body
    listing the offending document slugs. When everything is in order the
    function returns ``None``.
    """
    if isinstance(principal, params.Depends):
        # Called outside FastAPI's DI machinery — the principal is still the
        # unresolved ``Depends`` marker. Use the module-level placeholder.
        user_id = _placeholder_user_id
        if user_id is None:
            return
    else:
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        user_id = principal.id

    pending = await legal_service.get_pending_acceptances(db, user_id)
    if pending:
        raise HTTPException(
            status_code=451,
            detail={
                "code": "legal_re_acceptance_required",
                "documents": [p.slug for p in pending],
            },
        )
    return
