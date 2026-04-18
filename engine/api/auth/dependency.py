from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from engine.api.auth.jwt import decode_access_token
from engine.config import settings
from engine.db.models import RefreshToken, User
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_bearer_scheme = HTTPBearer()

ROLE_HIERARCHY: dict[str, int] = {
    "user": 0,
    "developer": 1,
    "admin": 2,
}


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload"
        )

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account deactivated")

    return user


def require_role(minimum_role: str):
    def _check(user: User = Depends(get_current_user)) -> User:
        user_level = ROLE_HIERARCHY.get(user.role, -1)
        required_level = ROLE_HIERARCHY.get(minimum_role, 999)
        if user_level < required_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return _check


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_refresh_token() -> str:
    return secrets.token_hex(32)


async def store_refresh_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    plain_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> RefreshToken:
    token_hash = _hash_token(plain_token)
    expires_at = datetime.now(tz=UTC) + timedelta(days=settings.jwt_refresh_token_expire_days)
    rt = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    db.add(rt)
    await db.flush()
    return rt


async def verify_and_rotate_refresh_token(
    db: AsyncSession,
    plain_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str]:
    token_hash = _hash_token(plain_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    rt = result.scalar_one_or_none()

    if rt is None:
        logger.warning("auth.refresh_token_not_found")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    if rt.revoked_at is not None:
        logger.warning(
            "auth.refresh_token_replay_detected",
            user_id=str(rt.user_id),
        )
        await _revoke_all_user_tokens(db, rt.user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token reuse detected. All sessions terminated.",
        )

    if _ensure_aware(rt.expires_at) < datetime.now(tz=UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired"
        )

    rt.revoked_at = datetime.now(tz=UTC)
    await db.flush()

    result = await db.execute(select(User).where(User.id == rt.user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or deactivated"
        )

    new_plain = generate_refresh_token()
    await store_refresh_token(db, user.id, new_plain, user_agent, ip_address)

    return user, new_plain


async def revoke_refresh_token(db: AsyncSession, plain_token: str) -> None:
    token_hash = _hash_token(plain_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    rt = result.scalar_one_or_none()
    if rt is not None:
        rt.revoked_at = datetime.now(tz=UTC)
        await db.flush()


async def revoke_all_user_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    now = datetime.now(tz=UTC)
    for rt in result.scalars().all():
        rt.revoked_at = now
    await db.flush()


async def _revoke_all_user_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    await revoke_all_user_tokens(db, user_id)
