from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


class LDAPAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "ldap"

    async def authenticate(self, **kwargs) -> AuthResult:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not db:
            return AuthResult(success=False, error="Database session required")

        try:
            import ldap

            conn = ldap.initialize(settings.ldap_server_url)
            conn.protocol_version = 3
            conn.set_option(ldap.OPT_REFERRALS, 0)

            user_dn = f"uid={username},{settings.ldap_search_base}"
            conn.simple_bind_s(user_dn, password)

            result = conn.search_s(
                settings.ldap_search_base,
                ldap.SCOPE_SUBTREE,
                f"(uid={username})",
                ["cn", "mail", "memberOf"],
            )
            conn.unbind_s()

        except Exception:
            logger.info("auth.ldap.bind_failed", username=username)
            return AuthResult(success=False, error="Invalid credentials")

        if not result:
            return AuthResult(success=False, error="Invalid credentials")

        _, entry = result[0]
        cn = entry.get("cn", [b""])[0].decode()
        mail = entry.get("mail", [b""])[0].decode()
        member_of_raw = entry.get("memberOf", [])
        groups = [g.decode() for g in member_of_raw]

        mapped_role = self.map_roles(groups)
        ldap_id = f"ldap:{username}"

        return await _find_or_create_ldap_user(
            db, ldap_id, mail or f"{username}@ldap", cn, mapped_role
        )

    async def get_user_info(self, external_id: str) -> UserInfo | None:
        return None

    def map_roles(self, external_roles: list[str]) -> str:
        role_mapping = json.loads(settings.ldap_role_mapping)
        for dn in external_roles:
            for group, role in role_mapping.items():
                if group in dn:
                    return role
        return "user"


async def _find_or_create_ldap_user(
    db: AsyncSession,
    ldap_id: str,
    email: str,
    display_name: str,
    role: str,
) -> AuthResult:
    from engine.db.models import User

    result = await db.execute(
        select(User).where(User.auth_provider == "ldap", User.external_id == ldap_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            email=email,
            hashed_password=None,
            display_name=display_name,
            role=role,
            auth_provider="ldap",
            external_id=ldap_id,
        )
        db.add(user)
        await db.flush()
    elif user.role != role:
        user.role = role
        await db.flush()

    if not user.is_active:
        return AuthResult(success=False, error="Account deactivated")

    return AuthResult(
        success=True,
        user_info=UserInfo(
            external_id=ldap_id,
            email=user.email,
            display_name=user.display_name,
            provider="ldap",
            roles=[user.role],
        ),
    )
