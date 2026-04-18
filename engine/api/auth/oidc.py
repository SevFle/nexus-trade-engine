from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
from jose import jwt as jose_jwt
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings
from engine.db.models import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


class OIDCAuthProvider(IAuthProvider):
    def __init__(self) -> None:
        self._discovery_cache: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "oidc"

    async def _get_discovery(self) -> dict[str, Any]:
        if self._discovery_cache is not None:
            return self._discovery_cache
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.oidc_discovery_url)
            resp.raise_for_status()
            self._discovery_cache = resp.json()
        return self._discovery_cache

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        code = kwargs.get("code")
        db: AsyncSession | None = kwargs.get("db")
        if not code or db is None:
            return AuthResult(success=False, error="Authorization code and db session required")

        try:
            import httpx

            discovery = await self._get_discovery()
            token_endpoint = discovery["token_endpoint"]

            async with httpx.AsyncClient() as client:
                token_resp = await client.post(
                    token_endpoint,
                    data={
                        "code": code,
                        "client_id": settings.oidc_client_id,
                        "client_secret": settings.oidc_client_secret,
                        "redirect_uri": settings.oidc_redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
                token_resp.raise_for_status()
                tokens = token_resp.json()

            id_token = tokens.get("id_token", "")
            claims = jose_jwt.get_unverified_claims(id_token)
            claims_data = json.loads(claims)

        except Exception as exc:
            logger.exception("auth.oidc.failed", error=str(exc))
            return AuthResult(success=False, error="OIDC authentication failed")

        oidc_id = claims_data.get("sub")
        email = claims_data.get("email", "")
        name = claims_data.get("name") or claims_data.get(
            "preferred_username", email.split("@")[0]
        )
        raw_roles = claims_data.get(settings.oidc_role_claim, [])

        if not oidc_id or not email:
            return AuthResult(success=False, error="Incomplete OIDC profile")

        result = await db.execute(
            select(User).where(User.auth_provider == "oidc", User.external_id == oidc_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            existing = await db.execute(select(User).where(User.email == email))
            existing_user = existing.scalar_one_or_none()
            if existing_user is not None:
                return AuthResult(
                    success=False, error="Email already registered with a different provider"
                )

            mapped_role = "user"
            if isinstance(raw_roles, list):
                mapped_role = self.map_roles(raw_roles)

            user = User(
                email=email,
                hashed_password=None,
                display_name=name,
                role=mapped_role,
                auth_provider="oidc",
                external_id=oidc_id,
            )
            db.add(user)
            await db.flush()
            await db.refresh(user)
            logger.info("auth.oidc.user_created", user_id=str(user.id))

        if not user.is_active:
            return AuthResult(success=False, error="Account is disabled")

        return AuthResult(
            success=True,
            user_info=UserInfo(
                external_id=oidc_id,
                email=user.email,
                display_name=user.display_name,
                provider="oidc",
                roles=[user.role],
                raw_claims=claims_data,
            ),
        )

    async def get_authorize_url(self) -> str:
        discovery = await self._get_discovery()
        auth_endpoint = discovery["authorization_endpoint"]
        return (
            f"{auth_endpoint}"
            f"?client_id={settings.oidc_client_id}"
            f"&redirect_uri={settings.oidc_redirect_uri}"
            f"&response_type=code"
            f"&scope=openid email profile"
        )
