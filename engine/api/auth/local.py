from __future__ import annotations

from typing import TYPE_CHECKING, Any

import bcrypt
import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings
from engine.db.models import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

MIN_PASSWORD_LENGTH = 8


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


class LocalAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "local"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        email = kwargs.get("email", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not email or not password or db is None:
            return AuthResult(success=False, error="Email, password, and db session required")

        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is None or user.auth_provider != "local" or user.hashed_password is None:
            _hash_password("dummy-timing-protection")
            return AuthResult(success=False, error="Invalid credentials")

        if not _verify_password(password, user.hashed_password):
            return AuthResult(success=False, error="Invalid credentials")

        if not user.is_active:
            return AuthResult(success=False, error="Account is disabled")

        logger.info("auth.local.login_success", user_id=str(user.id), email=user.email)
        return AuthResult(
            success=True,
            user_info=UserInfo(
                external_id=None,
                email=user.email,
                display_name=user.display_name,
                provider="local",
                roles=[user.role],
            ),
        )

    async def create_user(self, user_info: UserInfo, **kwargs: Any) -> AuthResult:
        db: AsyncSession | None = kwargs.get("db")
        password: str | None = kwargs.get("password")

        if db is None or password is None:
            return AuthResult(success=False, error="db session and password required")

        if not settings.auth_local_allow_registration:
            return AuthResult(success=False, error="Registration is disabled")

        if len(password) < MIN_PASSWORD_LENGTH:
            return AuthResult(success=False, error="Password must be at least 8 characters")

        existing = await db.execute(select(User).where(User.email == user_info.email))
        if existing.scalar_one_or_none() is not None:
            return AuthResult(success=False, error="Email already registered")

        hashed = _hash_password(password)
        user = User(
            email=user_info.email,
            hashed_password=hashed,
            display_name=user_info.display_name or user_info.email.split("@")[0],
            role="user",
            auth_provider="local",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)

        logger.info("auth.local.register_success", user_id=str(user.id), email=user.email)
        return AuthResult(
            success=True,
            user_info=UserInfo(
                external_id=None,
                email=user.email,
                display_name=user.display_name,
                provider="local",
                roles=[user.role],
            ),
        )
