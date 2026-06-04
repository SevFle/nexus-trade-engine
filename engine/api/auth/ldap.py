from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings
from engine.db.models import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


class LDAPAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "ldap"

    @staticmethod
    def _ldap_fetch(username: str, password: str) -> dict[str, Any] | None:
        """Bind to LDAP and return the user's attributes, or None when absent."""
        import ldap
        from ldap.filter import escape_filter_chars

        conn = ldap.initialize(settings.ldap_server_url)
        conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 10)
        conn.set_option(ldap.OPT_TIMEOUT, 10)

        safe_username = escape_filter_chars(username)
        user_dn = settings.ldap_bind_dn.replace("{{username}}", safe_username)
        conn.simple_bind_s(user_dn, password)

        search_filter = f"(uid={safe_username})"
        results = conn.search_s(
            settings.ldap_search_base,
            ldap.SCOPE_SUBTREE,
            search_filter,
            ["uid", "mail", "cn", "memberOf"],
        )
        conn.unbind_s()
        if not results:
            return None
        _, ldap_attrs = results[0]
        return ldap_attrs

    @staticmethod
    def _map_ldap_groups_to_roles(ldap_groups: list[str]) -> list[str]:
        role_mapping = json.loads(settings.ldap_role_mapping) if settings.ldap_role_mapping else {}
        mapped_roles: list[str] = []
        for group_dn in ldap_groups:
            for ldap_group, nexus_role in role_mapping.items():
                if ldap_group in group_dn:
                    mapped_roles.append(nexus_role)
        return mapped_roles or ["user"]

    async def _maybe_overwrite_role(
        self, db: AsyncSession, user: User, mapped_role: str
    ) -> None:
        if user.role == mapped_role:
            return
        # Role overwrite on login is gated on
        # ``settings.auth_overwrite_role_on_login`` (defaults to False —
        # SEV-741). When the mapped role is *lower* than the user's
        # current role we always emit an explicit warning so operators
        # can detect attempted downgrades (whether or not overwrite is
        # enabled), and skip the write when overwrite is disabled.
        role_priority = {
            "viewer": 0,
            "user": 1,
            "retail_trader": 2,
            "quant_dev": 3,
            "developer": 4,
            "portfolio_manager": 5,
            "admin": 6,
        }
        current_priority = role_priority.get(user.role, -1)
        mapped_priority = role_priority.get(mapped_role, -1)
        if mapped_priority < current_priority:
            logger.warning(
                "auth.ldap.role_downgrade_on_login",
                provider=self.name,
                user_id=str(getattr(user, "id", "")),
                current_role=user.role,
                mapped_role=mapped_role,
                overwrite_enabled=settings.auth_overwrite_role_on_login,
            )
        if settings.auth_overwrite_role_on_login:
            user.role = mapped_role
            await db.flush()

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not username or not password or db is None:
            return AuthResult(success=False, error="Username, password, and db session required")

        try:
            ldap_attrs = self._ldap_fetch(username, password)
        except Exception as exc:
            logger.exception("auth.ldap.bind_failed", error=str(exc))
            return AuthResult(success=False, error="Invalid credentials")

        if ldap_attrs is None:
            return AuthResult(success=False, error="User not found in LDAP")

        ldap_uid = ldap_attrs.get("uid", [b""])[0].decode()
        ldap_mail = ldap_attrs.get("mail", [b""])[0].decode() or f"{username}@ldap"
        ldap_cn = ldap_attrs.get("cn", [b""])[0].decode() or username
        ldap_groups = [g.decode() for g in ldap_attrs.get("memberOf", [])]

        mapped_role = self.map_roles(self._map_ldap_groups_to_roles(ldap_groups))

        result = await db.execute(
            select(User).where(User.auth_provider == "ldap", User.external_id == ldap_uid)
        )
        user = result.scalar_one_or_none()

        if user is None:
            existing = await db.execute(select(User).where(User.email == ldap_mail))
            existing_user = existing.scalar_one_or_none()
            if existing_user is not None:
                return AuthResult(
                    success=False, error="Email already registered with a different provider"
                )

            user = User(
                email=ldap_mail,
                hashed_password=None,
                display_name=ldap_cn,
                role=mapped_role,
                auth_provider="ldap",
                external_id=ldap_uid,
            )
            db.add(user)
            await db.flush()
            await db.refresh(user)
            logger.info("auth.ldap.user_created", user_id=str(user.id))
        else:
            await self._maybe_overwrite_role(db, user, mapped_role)

        if not user.is_active:
            return AuthResult(success=False, error="Account is disabled")

        return AuthResult(
            success=True,
            user_info=UserInfo(
                external_id=ldap_uid,
                email=user.email,
                display_name=user.display_name,
                provider="ldap",
                roles=[user.role],
            ),
        )
