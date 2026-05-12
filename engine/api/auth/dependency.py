from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from engine.api.auth.api_keys import (
    find_active_by_token,
    is_engine_token,
    touch_last_used,
)
from engine.api.auth.jwt import decode_token
from engine.db.models import ApiKey, User
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_bearer_scheme = HTTPBearer(auto_error=False)

ROLE_HIERARCHY: dict[str, int] = {"user": 0, "developer": 1, "admin": 2}


_UNAUTHORIZED_MISSING = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
)


def _resolve_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """Pull a credential from either Authorization: Bearer or X-API-Key."""
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    api_key_header = request.headers.get("x-api-key")
    if api_key_header:
        return api_key_header
    return None


async def _user_from_jwt(token: str, db: AsyncSession) -> User:
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload"
        )

    try:
        user_uuid = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload"
        ) from None

    return await _load_active_user(db, user_uuid)


async def _user_from_api_key(token: str, db: AsyncSession) -> tuple[User, ApiKey]:
    row = await find_active_by_token(db, token)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key"
        )
    user = await _load_active_user(db, row.user_id)
    await touch_last_used(db, row)
    return user, row


async def _load_active_user(db: AsyncSession, user_uuid: uuid.UUID) -> User:
    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User account is disabled"
        )
    return user


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = _resolve_token(request, credentials)
    if token is None:
        raise _UNAUTHORIZED_MISSING

    if is_engine_token(token):
        user, api_key = await _user_from_api_key(token, db)
        # Stash the active API key on request.state so scope-aware dependencies
        # downstream can read it without re-authenticating.
        request.state.api_key = api_key
        return user

    return await _user_from_jwt(token, db)


def require_role(minimum_role: str):
    min_level = ROLE_HIERARCHY.get(minimum_role, 0)

    async def _check(user: User = Depends(get_current_user)) -> User:
        user_level = ROLE_HIERARCHY.get(user.role, 0)
        if user_level < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum_role} role or higher",
            )
        return user

    return _check


# ---------------------------------------------------------------------------
# API-key scope enforcement (gh#86 / gh#94)
# ---------------------------------------------------------------------------
#
# When the request was authenticated via an engine API key, we enforce the
# scope vocabulary declared on that key (e.g. ``["read"]``). When the request
# was authenticated via JWT (i.e. an interactive operator session) we treat
# the principal as full-scope and skip the check — JWT auth is gated by the
# role check instead.
#
# Scope semantics:
#   ``read``   — GET / HEAD only.
#   ``trade``  — POST / PUT / PATCH for backtest, portfolio, webhooks, etc.
#   ``admin``  — equivalent to the ``admin`` role; supersedes both above.


_SCOPE_HIERARCHY: dict[str, int] = {"read": 0, "trade": 1, "admin": 2}


def _scope_satisfied(granted: list[str] | None, required: str) -> bool:
    required_level = _SCOPE_HIERARCHY.get(required, 0)
    return any(_SCOPE_HIERARCHY.get(s, -1) >= required_level for s in granted or [])


def require_api_scope(required_scope: str):
    """FastAPI dependency factory that enforces an API-key scope.

    JWT-authenticated requests bypass this check (they are gated by
    :func:`require_role`). API-key requests must declare a scope at least
    as privileged as ``required_scope`` per :data:`_SCOPE_HIERARCHY`.
    """
    if required_scope not in _SCOPE_HIERARCHY:
        raise ValueError(f"unknown scope: {required_scope!r}")

    async def _check(
        request: Request,
        user: User = Depends(get_current_user),
    ) -> User:
        api_key: ApiKey | None = getattr(request.state, "api_key", None)
        if api_key is None:
            # JWT auth — full-scope.
            return user
        if not _scope_satisfied(list(api_key.scopes or []), required_scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key missing required scope: {required_scope}",
            )
        return user

    return _check
