from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog
import jwt
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
        self._jwks_cache: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "oidc"

    async def _get_discovery(self) -> dict[str, Any]:
        if self._discovery_cache is not None:
            return self._discovery_cache

        url = settings.oidc_discovery_url
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError(
                f"OIDC discovery URL must use HTTPS, got scheme '{parsed.scheme}': {url}"
            )

        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            self._discovery_cache = resp.json()
        return self._discovery_cache

    async def _get_jwks(self) -> dict[str, Any]:
        if self._jwks_cache is not None:
            return self._jwks_cache
        import httpx

        discovery = await self._get_discovery()
        jwks_uri = discovery["jwks_uri"]
        async with httpx.AsyncClient() as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            self._jwks_cache = resp.json()
        return self._jwks_cache

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
            jwks = await self._get_jwks()
            unverified_header = jwt.get_unverified_header(id_token)
            kid = unverified_header.get("kid")
            signing_key = None
            for key_data in jwks.get("keys", []):
                if key_data.get("kid") == kid:
                    signing_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
                    break
            if signing_key is None:
                msg = f"No matching key found for kid={kid}"
                raise ValueError(msg)
            claims_data = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                audience=settings.oidc_client_id,
            )

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
                is_active=True,
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

    async def get_authorize_url(self, state: str = "") -> str:
        discovery = await self._get_discovery()
        auth_endpoint = discovery["authorization_endpoint"]
        url = (
            f"{auth_endpoint}"
            f"?client_id={settings.oidc_client_id}"
            f"&redirect_uri={settings.oidc_redirect_uri}"
            f"&response_type=code"
            f"&scope=openid email profile"
        )
        if state:
            url += f"&state={state}"
        return url
