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

_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_API_USER = "https://api.github.com/user"


class GitHubAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "github"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        code = kwargs.get("code")
        db: AsyncSession | None = kwargs.get("db")
        if not code or db is None:
            return AuthResult(success=False, error="Authorization code and db session required")

        try:
            import httpx

            token_data = {
                "code": code,
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "redirect_uri": settings.github_redirect_uri,
            }
            async with httpx.AsyncClient() as client:
                token_resp = await client.post(
                    _GITHUB_TOKEN_URL,
                    data=token_data,
                    headers={"Accept": "application/json"},
                )
                token_resp.raise_for_status()
                tokens = token_resp.json()

                userinfo_resp = await client.get(
                    _GITHUB_API_USER,
                    headers={
                        "Authorization": f"Bearer {tokens['access_token']}",
                        "Accept": "application/json",
                    },
                )
                userinfo_resp.raise_for_status()
                profile = userinfo_resp.json()
        except Exception as exc:
            logger.exception("auth.github.failed", error=str(exc))
            return AuthResult(success=False, error="GitHub authentication failed")

        github_id = str(profile.get("id", ""))
        email = profile.get("email") or f"{profile.get('login', '')}@github"
        name = profile.get("name") or profile.get("login", "GitHub User")

        if not github_id:
            return AuthResult(success=False, error="Incomplete GitHub profile")

        result = await db.execute(
            select(User).where(User.auth_provider == "github", User.external_id == github_id)
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
                role="viewer",
                auth_provider="github",
                external_id=github_id,
            )
            db.add(user)
            await db.flush()
            await db.refresh(user)
            logger.info("auth.github.user_created", user_id=str(user.id))
        # SEV-741: existing federated users keep their previously
        # granted role unless the operator has explicitly opted in
        # to per-login role reflection via
        # ``auth_overwrite_role_on_login``.  GitHub IdP does not
        # surface internal roles in this implementation, so the
        # only effect of opting in here is to log a notice — there
        # is no upstream role claim to apply.  The guard is kept
        # for symmetry with the OIDC / LDAP paths and to ensure
        # that if a future change adds GitHub role extraction the
        # safety check is already in place.
        elif settings.auth_overwrite_role_on_login:
            logger.info(
                "auth.github.role_overwrite_noop",
                user_id=str(user.id),
                role=user.role,
            )

        if not user.is_active:
            return AuthResult(success=False, error="Account is disabled")

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

    def get_authorize_url(self, state: str = "") -> str:
        url = (
            f"https://github.com/login/oauth/authorize"
            f"?client_id={settings.github_client_id}"
            f"&redirect_uri={settings.github_redirect_uri}"
            f"&scope=user:email"
        )
        if state:
            url += f"&state={state}"
        return url
