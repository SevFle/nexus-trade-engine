from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
_GITHUB_API_USER = "https://api.github.com/user"


class GitHubAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "github"

    def get_authorize_url(self, state: str) -> str:
        params = {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.github_redirect_uri,
            "scope": "user:email",
            "state": state,
        }
        from urllib.parse import urlencode

        return f"{_GITHUB_AUTH_URL}?{urlencode(params)}"

    async def authenticate(self, **kwargs) -> AuthResult:
        code = kwargs.get("code")
        db = kwargs.get("db")
        if not code or not db:
            return AuthResult(success=False, error="Missing code or db session")

        import httpx

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                _GITHUB_TOKEN_URL,
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                    "redirect_uri": settings.github_redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            if token_resp.status_code != 200:
                logger.error("auth.github.token_exchange_failed", status=token_resp.status_code)
                return AuthResult(success=False, error="GitHub token exchange failed")

            tokens = token_resp.json()
            access_token = tokens.get("access_token", "")

            user_resp = await client.get(
                _GITHUB_API_USER,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if user_resp.status_code != 200:
                return AuthResult(success=False, error="Failed to fetch GitHub user info")

            profile = user_resp.json()

        github_id = str(profile.get("id", ""))
        login = profile.get("login", "")
        name = profile.get("name") or login
        email = profile.get("email") or f"{login}@users.noreply.github.com"

        return await _find_or_create_github_user(db, github_id, email, name)

    async def get_user_info(self, external_id: str) -> UserInfo | None:
        return None


async def _find_or_create_github_user(
    db: AsyncSession, github_id: str, email: str, display_name: str
) -> AuthResult:
    from engine.db.models import User

    result = await db.execute(
        select(User).where(User.auth_provider == "github", User.external_id == github_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            email=email,
            hashed_password=None,
            display_name=display_name,
            role="user",
            auth_provider="github",
            external_id=github_id,
        )
        db.add(user)
        await db.flush()

    if not user.is_active:
        return AuthResult(success=False, error="Account deactivated")

    return AuthResult(
        success=True,
        user_info=UserInfo(
            external_id=github_id,
            email=user.email,
            display_name=user.display_name,
            provider="github",
            roles=[user.role],
        ),
    )
