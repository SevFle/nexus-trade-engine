from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, params, status

from engine.api.auth.dependency import get_current_user
from engine.deps import get_db
from engine.legal import service as legal_service

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from engine.db.models import User

# Testability affordance (see ADR-0005 / SEV-501 B2).
#
# ``require_legal_acceptance`` is wired into routers as a FastAPI dependency,
# so in production the principal is resolved by ``get_current_user``. When the
# function is *also* invoked directly (outside of dependency injection — e.g.
# from tests or while the auth surface is still landing) the ``principal``
# argument is left at its unresolved ``Depends`` default. In that situation
# this module-level id stands in for the caller:
#
#   * ``None``  -> enforcement is a no-op (the request is allowed through),
#                  matching the documented pre-auth behaviour.
#   * set       -> treated as the caller's user id, exercising the pending
#                  acceptance path without plumbing a full ``User``.
#
# Tests monkeypatch this attribute directly; it must remain a module attribute.
_placeholder_user_id: uuid.UUID | None = None


async def require_legal_acceptance(
    principal: User | None = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    """Enforce outstanding legal document acceptance for the caller.

    Two invocation modes are supported:

    * **Via FastAPI** (the normal path) — ``principal`` is resolved by
      :func:`get_current_user`. To be fail-closed we explicitly reject the
      request with ``401`` when no principal could be resolved (e.g. the auth
      dependency was overridden to return ``None``) rather than silently
      allowing it through. Outstanding required documents surface as ``451``.

    * **Direct invocation** (tests / pre-auth wiring) — ``principal`` stays as
      its unresolved ``Depends`` default. We then fall back to
      :data:`_placeholder_user_id` so the dependency can be exercised with a
      plain ``db`` session. Keeping the dependency callable this way is what
      unblocks targeted unit tests without a full ASGI/auth stack.
    """
    user_id: uuid.UUID
    if isinstance(principal, params.Depends):
        # Direct invocation: FastAPI did not resolve the dependency.
        if _placeholder_user_id is None:
            return
        user_id = _placeholder_user_id
    elif principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    else:
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
