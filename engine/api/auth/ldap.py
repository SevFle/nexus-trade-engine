from __future__ import annotations

import contextlib
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


_DN_SPECIALS = {'"', "+", ",", ";", "<", ">", "\\"}


def _escape_dn_chars(value: str) -> str:
    """Escape ``value`` for safe inclusion in an LDAP distinguished name.

    Implements RFC 4514 DN-string escaping for the *attribute value*
    component. The following characters require a leading backslash:
    ``"``, ``+``, ``,``, ``;``, ``<``, ``>``, ``\\``. A leading ``#``
    (only when the value is non-empty) and leading / trailing spaces are
    also escaped.
    """
    if value == "":
        return ""

    escaped: list[str] = []
    last_idx = len(value) - 1

    for i, ch in enumerate(value):
        if ch in _DN_SPECIALS:
            escaped.append("\\" + ch)
        elif ch == "#" and i == 0:
            escaped.append("\\#")
        elif ch == " " and i in (0, last_idx):
            escaped.append("\\ ")
        else:
            escaped.append(ch)

    return "".join(escaped)


def _escape_filter_chars(value: str) -> str:
    """Minimal RFC 4515 filter escaping used only for the search filter."""
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\5c")
        elif ch == "*":
            out.append("\\2a")
        elif ch == "(":
            out.append("\\28")
        elif ch == ")":
            out.append("\\29")
        elif ch == "\x00":
            out.append("\\00")
        else:
            out.append(ch)
    return "".join(out)


def _apply_tls_hardening(conn: Any, ldap_module: Any) -> None:
    """Configure TLS options and raise on negotiation failure.

    The caller is responsible for catching the raised exception to avoid
    silent cleartext fallback.
    """
    conn.set_option(ldap_module.OPT_NETWORK_TIMEOUT, 10)
    conn.set_option(ldap_module.OPT_TIMEOUT, 10)
    conn.set_option(ldap_module.OPT_X_TLS_REQUIRE_CERT, ldap_module.OPT_X_TLS_DEMAND)
    conn.set_option(ldap_module.OPT_X_TLS, ldap_module.OPT_X_TLS_HARD)
    conn.start_tls_s()


def _safe_unbind(conn: Any) -> None:
    """Best-effort unbind; never raises."""
    with contextlib.suppress(Exception):
        conn.unbind_s()


class _LDAPLookupResult:
    """Outcome of a single LDAP bind + search attempt."""

    def __init__(
        self,
        success: bool,
        error: str = "",
        attrs: dict[str, list[bytes]] | None = None,
    ):
        self.success = success
        self.error = error
        self.attrs = attrs


def _ldap_lookup(username: str, password: str) -> _LDAPLookupResult:
    """Perform the TLS-hardened LDAP bind + search.

    Returns a result object describing the outcome. The caller is
    responsible for converting it to an ``AuthResult``.
    """
    import ldap

    conn = ldap.initialize(settings.ldap_server_url)

    try:
        _apply_tls_hardening(conn, ldap)
    except Exception as tls_exc:
        logger.warning(
            "auth.ldap.start_tls_failed",
            error=str(tls_exc),
            server=settings.ldap_server_url,
        )
        _safe_unbind(conn)
        return _LDAPLookupResult(
            success=False,
            error="TLS negotiation failed; cleartext fallback disabled",
        )

    try:
        safe_username_dn = _escape_dn_chars(username)
        safe_username_filter = _escape_filter_chars(username)
        user_dn = settings.ldap_bind_dn.replace("{{username}}", safe_username_dn)
        conn.simple_bind_s(user_dn, password)

        search_filter = f"(uid={safe_username_filter})"
        results = conn.search_s(
            settings.ldap_search_base,
            ldap.SCOPE_SUBTREE,
            search_filter,
            ["uid", "mail", "cn", "memberOf"],
        )
    except Exception as exc:
        logger.exception("auth.ldap.bind_failed", error=str(exc))
        _safe_unbind(conn)
        return _LDAPLookupResult(success=False, error="Invalid credentials")

    _safe_unbind(conn)

    if not results:
        return _LDAPLookupResult(success=False, error="User not found in LDAP")

    _, attrs = results[0]
    return _LDAPLookupResult(success=True, attrs=attrs)


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

        lookup = _ldap_lookup(username, password)
        if not lookup.success:
            return AuthResult(success=False, error=lookup.error)

        assert lookup.attrs is not None  # success implies attrs present
        ldap_attrs = lookup.attrs
        ldap_uid = ldap_attrs.get("uid", [b""])[0].decode()
        ldap_mail = ldap_attrs.get("mail", [b""])[0].decode() or f"{username}@ldap"
        ldap_cn = ldap_attrs.get("cn", [b""])[0].decode() or username

        member_of_raw = ldap_attrs.get("memberOf", [])
        ldap_groups = [g.decode() for g in member_of_raw]

        role_mapping = json.loads(settings.ldap_role_mapping) if settings.ldap_role_mapping else {}
        mapped_roles: list[str] = []
        for group_dn in ldap_groups:
            for ldap_group, nexus_role in role_mapping.items():
                if ldap_group in group_dn:
                    mapped_roles.append(nexus_role)

        if not mapped_roles:
            mapped_roles = ["user"]

        mapped_role = self.map_roles(mapped_roles)

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
