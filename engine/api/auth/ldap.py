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


class _LDAPAuthError(Exception):
    """Raised when LDAP bind/search fails, carrying a user-facing message."""


class LDAPAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "ldap"

    def _ldap_bind_and_search(self, username: str, password: str) -> dict[str, list[bytes]]:
        """Bind to LDAP with ``username``/``password`` and return the
        user's attribute mapping.

        Raises :class:`_LDAPAuthError` with a user-facing message on bind
        failure or when the user is not found.
        """
        try:
            import ldap
            from ldap.filter import escape_filter_chars

            conn = ldap.initialize(settings.ldap_server_url)
            conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 10)
            conn.set_option(ldap.OPT_TIMEOUT, 10)

            safe_username = escape_filter_chars(username)
            user_dn = f"{settings.ldap_bind_dn.replace('{{username}}', safe_username)}"
            conn.simple_bind_s(user_dn, password)

            search_filter = f"(uid={safe_username})"
            results = conn.search_s(
                settings.ldap_search_base,
                ldap.SCOPE_SUBTREE,
                search_filter,
                ["uid", "mail", "cn", "memberOf"],
            )
            conn.unbind_s()

        except Exception as exc:
            logger.exception("auth.ldap.bind_failed", error=str(exc))
            raise _LDAPAuthError("Invalid credentials") from exc

        if not results:
            raise _LDAPAuthError("User not found in LDAP")

        _, ldap_attrs = results[0]
        return ldap_attrs

    def _compute_ldap_role(self, ldap_groups: list[str]) -> str:
        """Map a user's LDAP group memberships to a single Nexus role
        via ``settings.ldap_role_mapping``."""
        role_mapping = json.loads(settings.ldap_role_mapping) if settings.ldap_role_mapping else {}
        mapped_roles: list[str] = []
        for group_dn in ldap_groups:
            for ldap_group, nexus_role in role_mapping.items():
                if ldap_group in group_dn:
                    mapped_roles.append(nexus_role)

        if not mapped_roles:
            mapped_roles = ["viewer"]

        return self.map_roles(mapped_roles)

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not username or not password or db is None:
            return AuthResult(success=False, error="Username, password, and db session required")

        try:
            ldap_attrs = self._ldap_bind_and_search(username, password)
        except _LDAPAuthError as exc:
            return AuthResult(success=False, error=str(exc))

        ldap_uid = ldap_attrs.get("uid", [b""])[0].decode()
        ldap_mail = ldap_attrs.get("mail", [b""])[0].decode() or f"{username}@ldap"
        ldap_cn = ldap_attrs.get("cn", [b""])[0].decode() or username

        member_of_raw = ldap_attrs.get("memberOf", [])
        ldap_groups = [g.decode() for g in member_of_raw]
        mapped_role = self._compute_ldap_role(ldap_groups)

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
        elif user.role != mapped_role:
            # SEV-741: only mutate an existing user's role when the
            # operator has explicitly opted in via
            # ``auth_overwrite_role_on_login``. Default is False, so a
            # misconfigured or compromised upstream IdP cannot silently
            # downgrade or escalate a previously-granted local role on
            # the next federated login.
            if settings.auth_overwrite_role_on_login:
                logger.info(
                    "auth.ldap.role_overwritten",
                    user_id=str(user.id),
                    previous_role=user.role,
                    new_role=mapped_role,
                )
                user.role = mapped_role
                await db.flush()
            else:
                logger.info(
                    "auth.ldap.role_overwrite_skipped",
                    user_id=str(user.id),
                    existing_role=user.role,
                    idp_role=mapped_role,
                )

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
