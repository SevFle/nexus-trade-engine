from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


class OIDCAuthProvider(IAuthProvider):
    def __init__(self) -> None:
        self._discovery_doc: dict | None = None

    @property
    def name(self) -> str:
        return "oidc"

    async def _get_discovery(self) -> dict:
        if self._discovery_doc is not None:
            return self._discovery_doc

        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.oidc_discovery_url)
            resp.raise_for_status()
            self._discovery_doc = resp.json()
        return self._discovery_doc

    def get_authorize_url(self, state: str) -> str | None:
        if self._discovery_doc is None:
            return None
        from urllib.parse import urlencode

        params = {
            "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        }
        auth_endpoint = self._discovery_doc.get("authorization_endpoint", "")
        return f"{auth_endpoint}?{urlencode(params)}"

    async def authenticate(self, **kwargs) -> AuthResult:
        code = kwargs.get("code")
        db = kwargs.get("db")
        if not code or not db:
            return AuthResult(success=False, error="Missing code or db session")

        discovery = await self._get_discovery()
        token_endpoint = discovery.get("token_endpoint", "")
        userinfo_endpoint = discovery.get("userinfo_endpoint", "")

        if not token_endpoint:
            return AuthResult(success=False, error="OIDC token endpoint not found")

        import httpx

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                token_endpoint,
                data={
                    "client_id": settings.oidc_client_id,
                    "client_secret": settings.oidc_client_secret,
                    "code": code,
                    "redirect_uri": settings.oidc_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                logger.error("auth.oidc.token_exchange_failed", status=token_resp.status_code)
                return AuthResult(success=False, error="OIDC token exchange failed")

            tokens = token_resp.json()
            access_token = tokens.get("access_token", "")

            if userinfo_endpoint:
                userinfo_resp = await client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if userinfo_resp.status_code != 200:
                    return AuthResult(success=False, error="Failed to fetch OIDC user info")
                claims = userinfo_resp.json()
            else:
                from jose import jwt

                id_token = tokens.get("id_token", "")
                claims = jwt.get_unverified_claims(id_token)

        sub = claims.get("sub", "")
        email = claims.get("email", "")
        name = claims.get("name", email)
        roles_claim = claims.get(settings.oidc_role_claim, [])
        if isinstance(roles_claim, str):
            roles_claim = [roles_claim]

        return await _find_or_create_oidc_user(db, sub, email, name, roles_claim)

    async def get_user_info(self, external_id: str) -> UserInfo | None:
        return None

    def map_roles(self, external_roles: list[str]) -> str:
        if "admin" in external_roles:
            return "admin"
        if "developer" in external_roles:
            return "developer"
        return "user"


async def _find_or_create_oidc_user(
    db: AsyncSession,
    oidc_sub: str,
    email: str,
    display_name: str,
    external_roles: list[str],
) -> AuthResult:
    from engine.db.models import User

    result = await db.execute(
        select(User).where(User.auth_provider == "oidc", User.external_id == oidc_sub)
    )
    user = result.scalar_one_or_none()

    provider = OIDCAuthProvider()
    mapped_role = provider.map_roles(external_roles)

    if user is None:
        user = User(
            email=email,
            hashed_password=None,
            display_name=display_name,
            role=mapped_role,
            auth_provider="oidc",
            external_id=oidc_sub,
        )
        db.add(user)
        await db.flush()
    elif user.role != mapped_role:
        user.role = mapped_role
        await db.flush()

    if not user.is_active:
        return AuthResult(success=False, error="Account deactivated")

    return AuthResult(
        success=True,
        user_info=UserInfo(
            external_id=oidc_sub,
            email=user.email,
            display_name=user.display_name,
            provider="oidc",
            roles=[user.role],
            raw_claims={"external_roles": external_roles},
        ),
    )
