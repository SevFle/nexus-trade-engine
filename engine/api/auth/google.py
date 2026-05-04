from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings
from engine.db.models import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class GoogleAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "google"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        code = kwargs.get("code")
        db: AsyncSession | None = kwargs.get("db")
        if not code or db is None:
            return AuthResult(success=False, error="Authorization code and db session required")

        try:
            import httpx

            token_data = {
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            }
            async with httpx.AsyncClient() as client:
                token_resp = await client.post(_GOOGLE_TOKEN_URL, data=token_data)
                token_resp.raise_for_status()
                tokens = token_resp.json()

                userinfo_resp = await client.get(
                    _GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                )
                userinfo_resp.raise_for_status()
                profile = userinfo_resp.json()
        except Exception as exc:
            logger.exception("auth.google.failed", error=str(exc))
            return AuthResult(success=False, error="Google authentication failed")

        google_id = profile.get("sub")
        email = profile.get("email", "")
        name = profile.get("name", email.split("@")[0])

        if not google_id or not email:
            return AuthResult(success=False, error="Incomplete Google profile")

        result = await db.execute(
            select(User).where(User.auth_provider == "google", User.external_id == google_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            existing = await db.execute(select(User).where(User.email == email))
            existing_user = existing.scalar_one_or_none()
            if existing_user is not None:
                return AuthResult(
                    success=False, error="Email already registered with a different provider"
                )

            user = User(
                email=email,
                hashed_password=None,
                display_name=name,
                role="user",
                auth_provider="google",
                external_id=google_id,
                is_active=True,
            )
            db.add(user)
            await db.flush()
            await db.refresh(user)
            logger.info("auth.google.user_created", user_id=str(user.id))

        if not user.is_active:
            return AuthResult(success=False, error="Account is disabled")

        return AuthResult(
            success=True,
            user_info=UserInfo(
                external_id=google_id,
                email=user.email,
                display_name=user.display_name,
                provider="google",
                roles=[user.role],
            ),
        )

    def get_authorize_url(self, state: str = "") -> str:
        url = (
            f"https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={settings.google_client_id}"
            f"&redirect_uri={settings.google_redirect_uri}"
            f"&response_type=code"
            f"&scope=openid email profile"
        )
        if state:
            url += f"&state={state}"
        return url
