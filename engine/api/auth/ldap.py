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


def _decode_attr(attrs: dict[str, list[bytes]], key: str, default: str = "") -> str:
    """Safely decode the first value of an LDAP attribute.

    LDAP attribute values are *lists* of ``bytes``. A directory entry may
    legitimately omit an attribute (key missing) or return it with an empty
    value list (``[]``). The previous implementation used
    ``attrs.get(key, [b""])[0]`` which raised ``IndexError`` for an empty
    list and ``KeyError``-style failures were silently swallowed by the broad
    ``except Exception`` and mis-reported to the user as "Invalid
    credentials". This helper performs a defensive list access and
    bytes->str decode instead.
    """
    values = attrs.get(key) or []
    if not values:
        return default
    raw = values[0]
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


class LDAPAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "ldap"

    def _query_ldap(self, username: str, password: str) -> AuthResult | dict[str, list[bytes]]:
        """Bind to and search the directory for ``username``.

        Returns the matched entry's attribute map on success, or an
        ``AuthResult`` describing the failure (dependency missing, directory
        unreachable, bad credentials, or no matching entry).
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

        except ImportError as exc:
            # python-ldap is not installed / importable -- an operational or
            # infrastructure failure, not a credential problem. Surface a
            # generic message rather than revealing the dependency state.
            logger.exception("auth.ldap.module_unavailable", error=str(exc))
            return AuthResult(success=False, error="Authentication service unavailable")
        except Exception as exc:
            # ``ldap`` is guaranteed to be bound here: the only way
            # ``import ldap`` fails is ``ImportError`` (handled above), so any
            # exception reaching this branch was raised after the import
            # succeeded. Separate infrastructure/operational errors
            # (directory unreachable, network timeout) from genuine credential
            # failures so users get an actionable message either way.
            infra_errors = tuple(
                e
                for e in (
                    getattr(ldap, "SERVER_DOWN", None),
                    getattr(ldap, "TIMEOUT", None),
                )
                if isinstance(e, type) and issubclass(e, BaseException)
            )
            if infra_errors and isinstance(exc, infra_errors):
                logger.exception("auth.ldap.service_unavailable", error=str(exc))
                return AuthResult(
                    success=False, error="Authentication service unavailable"
                )
            logger.exception("auth.ldap.bind_failed", error=str(exc))
            return AuthResult(success=False, error="Invalid credentials")

        if not results:
            # Return the same message as a bad password to avoid user
            # enumeration through the search step (a missing entry is
            # indistinguishable from wrong credentials to the caller).
            return AuthResult(success=False, error="Invalid credentials")

        _, ldap_attrs = results[0]
        return ldap_attrs

    def _map_groups_to_roles(self, ldap_groups: list[str]) -> list[str]:
        """Map LDAP group DNs to local roles via ``settings.ldap_role_mapping``."""
        role_mapping = json.loads(settings.ldap_role_mapping) if settings.ldap_role_mapping else {}
        mapped_roles: list[str] = []
        for group_dn in ldap_groups:
            for ldap_group, nexus_role in role_mapping.items():
                if ldap_group in group_dn:
                    mapped_roles.append(nexus_role)
        if not mapped_roles:
            mapped_roles = ["user"]
        return mapped_roles

    async def _sync_db_user(
        self,
        db: AsyncSession,
        ldap_uid: str,
        ldap_mail: str,
        ldap_cn: str,
        mapped_role: str,
    ) -> AuthResult | User:
        """Find or create the local ``User`` row for the LDAP identity.

        Returns the resolved ``User``, or an ``AuthResult`` if provisioning
        is blocked (e.g. the email is already claimed by another provider).
        """
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
            user.role = mapped_role
            await db.flush()

        return user

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not username or not password or db is None:
            return AuthResult(success=False, error="Username, password, and db session required")

        ldap_attrs = self._query_ldap(username, password)
        if isinstance(ldap_attrs, AuthResult):
            return ldap_attrs

        # Guard attribute extraction with a safe list access pattern: LDAP
        # attributes are lists and may be present-but-empty, which previously
        # raised IndexError and leaked as a misleading "Invalid credentials".
        ldap_uid = _decode_attr(ldap_attrs, "uid") or username
        ldap_mail = _decode_attr(ldap_attrs, "mail") or f"{username}@ldap"
        ldap_cn = _decode_attr(ldap_attrs, "cn") or username

        member_of_raw = ldap_attrs.get("memberOf", []) or []
        ldap_groups = [
            g.decode("utf-8", errors="replace") if isinstance(g, bytes) else str(g)
            for g in member_of_raw
        ]
        mapped_role = self.map_roles(self._map_groups_to_roles(ldap_groups))

        user = await self._sync_db_user(db, ldap_uid, ldap_mail, ldap_cn, mapped_role)
        if isinstance(user, AuthResult):
            return user

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
