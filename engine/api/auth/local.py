from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from passlib.context import CryptContext
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_MIN_PASSWORD_LENGTH = 8

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class LocalAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "local"

    async def authenticate(self, **kwargs) -> AuthResult:
        email = kwargs.get("email", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not db:
            return AuthResult(success=False, error="Database session required")

        return await self.authenticate_login(email, password, db)

    async def authenticate_login(self, email: str, password: str, db: AsyncSession) -> AuthResult:
        from engine.db.models import User

        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is None or user.hashed_password is None:
            logger.info("auth.local.login_failed", reason="not_found")
            return AuthResult(success=False, error="Invalid credentials")

        if not _pwd_context.verify(password, user.hashed_password):
            logger.info("auth.local.login_failed", reason="bad_password")
            return AuthResult(success=False, error="Invalid credentials")

        if not user.is_active:
            return AuthResult(success=False, error="Account deactivated")

        return AuthResult(
            success=True,
            user_info=UserInfo(
                external_id=str(user.id),
                email=user.email,
                display_name=user.display_name,
                provider="local",
                roles=[user.role],
            ),
        )

    async def register_user(
        self, email: str, password: str, display_name: str, db: AsyncSession
    ) -> AuthResult:
        from engine.db.models import User

        if not settings.auth_local_allow_registration:
            return AuthResult(success=False, error="Registration is disabled")

        if len(password) < _MIN_PASSWORD_LENGTH:
            return AuthResult(success=False, error="Password must be at least 8 characters")

        result = await db.execute(select(User).where(User.email == email))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return AuthResult(success=False, error="A user with this email already exists")

        hashed = _pwd_context.hash(password)
        user = User(
            email=email,
            hashed_password=hashed,
            display_name=display_name,
            role="user",
            auth_provider="local",
        )
        db.add(user)
        await db.flush()

        return AuthResult(
            success=True,
            user_info=UserInfo(
                external_id=str(user.id),
                email=user.email,
                display_name=user.display_name,
                provider="local",
                roles=["user"],
            ),
        )

    async def get_user_info(self, external_id: str) -> UserInfo | None:
        return None

    def map_roles(self, external_roles: list[str]) -> str:
        return "user"
