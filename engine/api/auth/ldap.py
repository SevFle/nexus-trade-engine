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


class LDAPError(Exception):
    """Base class for all LDAP authentication failures."""


class LDAPInvalidCredentialsError(LDAPError):
    """Raised when LDAP credentials are genuinely rejected.

    This covers two cases:

    * The directory server explicitly rejected the bind
      (``ldap.INVALID_CREDENTIALS`` / error 49).
    * The bind succeeded but the identity has no corresponding directory
      entry (``search_s`` returned no results). Treating this as a
      credential failure — rather than a distinct "user not found" result —
      avoids leaking which usernames exist to a caller probing the API.

    Callers typically translate this into an HTTP 401.
    """


class LDAPServiceUnavailableError(LDAPError):
    """Raised when the LDAP backend is unavailable for a non-credential
    reason.

    Triggers include:

    * The optional ``python-ldap`` dependency is not installed
      (``ImportError``).
    * Network failures (``ldap.SERVER_DOWN``, connection refused, DNS).
    * Timeouts (``ldap.TIMEOUT``, socket timeout).
    * Any other unexpected infrastructure error.

    This MUST NOT be raised for genuine credential rejections — those use
    :class:`LDAPInvalidCredentialsError`. Callers typically translate this
    into an HTTP 503.
    """


def _decode_first(ldap_attrs: dict[str, list[bytes]], key: str) -> str:
    """Safely decode the first value of a multi-valued LDAP attribute.

    ``ldap_attrs.get(key, [b""])[0]`` crashes with ``IndexError`` when the
    attribute is present but empty (``"uid": []``). This helper guards
    against that by returning an empty string for a missing key or an empty
    value list.
    """
    values = ldap_attrs.get(key, [])
    return values[0].decode() if values else ""


class LDAPAuthProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "ldap"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not username or not password or db is None:
            return AuthResult(success=False, error="Username, password, and db session required")

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
            # The optional python-ldap dependency is not installed. This is an
            # infrastructure/availability problem, NOT a credential failure.
            logger.exception("auth.ldap.dependency_missing", error=str(exc))
            raise LDAPServiceUnavailableError(
                "LDAP backend dependency is not installed"
            ) from exc
        except Exception as exc:
            # Classify the failure: a genuine credential rejection
            # (ldap.INVALID_CREDENTIALS) becomes LDAPInvalidCredentialsError;
            # everything else (SERVER_DOWN, network, timeout, ...) is treated
            # as an infrastructure failure via LDAPServiceUnavailableError.
            cred_exc_type = getattr(ldap, "INVALID_CREDENTIALS", None)
            if cred_exc_type is not None and isinstance(exc, cred_exc_type):
                logger.info("auth.ldap.invalid_credentials", error=str(exc))
                raise LDAPInvalidCredentialsError("Invalid credentials") from exc
            logger.exception("auth.ldap.service_unavailable", error=str(exc))
            raise LDAPServiceUnavailableError("LDAP service unavailable") from exc

        if not results:
            # Bind succeeded but the identity has no directory entry. Report
            # this as a credential failure to avoid leaking which usernames
            # are valid (user-enumeration protection).
            raise LDAPInvalidCredentialsError("Invalid credentials")

        _, ldap_attrs = results[0]
        # Safe indexing: an attribute may be present but empty ("uid": []),
        # which would otherwise raise IndexError on [0].
        ldap_uid = _decode_first(ldap_attrs, "uid")
        ldap_mail = _decode_first(ldap_attrs, "mail") or f"{username}@ldap"
        ldap_cn = _decode_first(ldap_attrs, "cn") or username
        mapped_role = self._map_ldap_groups_to_role(ldap_attrs)

        user = await self._resolve_user(db, ldap_uid, ldap_mail, ldap_cn, mapped_role)
        if user is None:
            return AuthResult(
                success=False, error="Email already registered with a different provider"
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

    def _map_ldap_groups_to_role(self, ldap_attrs: dict[str, list[bytes]]) -> str:
        """Map the LDAP ``memberOf`` groups to a single application role.

        Each group DN is matched by substring against the configured role
        mapping; when nothing matches the caller is assigned the default
        ``user`` role.
        """
        member_of_raw = ldap_attrs.get("memberOf", [])
        ldap_groups = [g.decode() for g in member_of_raw]

        role_mapping = (
            json.loads(settings.ldap_role_mapping) if settings.ldap_role_mapping else {}
        )
        mapped_roles: list[str] = []
        for group_dn in ldap_groups:
            for ldap_group, nexus_role in role_mapping.items():
                if ldap_group in group_dn:
                    mapped_roles.append(nexus_role)

        if not mapped_roles:
            mapped_roles = ["user"]
        return self.map_roles(mapped_roles)

    async def _resolve_user(
        self,
        db: AsyncSession,
        ldap_uid: str,
        ldap_mail: str,
        ldap_cn: str,
        mapped_role: str,
    ) -> User | None:
        """Look up the local user for an LDAP identity, creating it if needed.

        Returns the resolved :class:`User`, or ``None`` when the email is
        already registered under a different auth provider (the caller then
        reports a conflict). An existing user's role is updated in place when
        it has changed.
        """
        result = await db.execute(
            select(User).where(User.auth_provider == "ldap", User.external_id == ldap_uid)
        )
        user = result.scalar_one_or_none()

        if user is not None:
            if user.role != mapped_role:
                user.role = mapped_role
                await db.flush()
            return user

        existing = await db.execute(select(User).where(User.email == ldap_mail))
        if existing.scalar_one_or_none() is not None:
            return None

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
        return user
