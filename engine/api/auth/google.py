from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class GoogleAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "google"

    def get_authorize_url(self, state: str) -> str:
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        }
        from urllib.parse import urlencode

        return f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def authenticate(self, **kwargs) -> AuthResult:
        code = kwargs.get("code")
        db = kwargs.get("db")
        if not code or not db:
            return AuthResult(success=False, error="Missing code or db session")

        import httpx

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "code": code,
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                logger.error("auth.google.token_exchange_failed", status=token_resp.status_code)
                return AuthResult(success=False, error="Google token exchange failed")

            tokens = token_resp.json()
            access_token = tokens.get("access_token", "")

            userinfo_resp = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_resp.status_code != 200:
                return AuthResult(success=False, error="Failed to fetch Google user info")

            profile = userinfo_resp.json()

        google_id = profile.get("sub", "")
        email = profile.get("email", "")
        name = profile.get("name", email)

        return await _find_or_create_user(db, google_id, email, name)

    async def get_user_info(self, external_id: str) -> UserInfo | None:
        return None


async def _find_or_create_user(
    db: AsyncSession, google_id: str, email: str, display_name: str
) -> AuthResult:
    from engine.db.models import User

    result = await db.execute(
        select(User).where(User.auth_provider == "google", User.external_id == google_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is not None:
            return AuthResult(
                success=False, error="Email already registered with a different provider"
            )

        user = User(
            email=email,
            hashed_password=None,
            display_name=display_name,
            role="user",
            auth_provider="google",
            external_id=google_id,
        )
        db.add(user)
        await db.flush()

    if not user.is_active:
        return AuthResult(success=False, error="Account deactivated")

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
