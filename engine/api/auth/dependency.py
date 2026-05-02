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
        user, _ = await _user_from_api_key(token, db)
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
