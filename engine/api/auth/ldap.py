"""LDAP authentication provider.

The provider performs a single-bind operation against the configured
LDAP server using a DN derived from a configurable template, then runs
a subtree search to fetch user attributes and group memberships.

Security notes:

* TLS options (``OPT_X_TLS_REQUIRE_CERT`` / ``OPT_X_TLS_CACERTFILE``)
  are applied to the **global** ``ldap`` module via ``ldap.set_option``
  *before* ``ldap.initialize()`` is called. Per-connection TLS options
  are unreliable in python-ldap — the global options are read at
  initialize time.

* The bind DN template (``settings.ldap_bind_dn``) is parsed with
  ``ldap.dn.str2dn`` and validated to ensure the ``{{username}}``
  placeholder only appears in a *value* position (never the attribute
  type). The user-supplied username is escaped with
  ``ldap.dn.escape_dn_chars`` and the DN is reassembled via
  ``ldap.dn.dn2str``. Malformed templates are rejected.
"""

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


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LDAPBindTemplateError(Exception):
    """Raised when the configured bind DN template is malformed.

    Surfaced at startup so misconfigurations can't lie latent until the
    first failed login attempt.
    """


class LDAPTLSConfigError(Exception):
    """Raised when the LDAP TLS settings cannot be applied."""


# ---------------------------------------------------------------------------
# Bind DN template — validation + rendering
# ---------------------------------------------------------------------------

_PLACEHOLDER = "{{username}}"


def _validate_bind_dn_template(
    template: str,
) -> list[list[tuple[str, str, int]]]:
    """Parse and validate ``settings.ldap_bind_dn``.

    Returns the parsed DN structure (suitable for ``ldap.dn.dn2str``)
    after asserting:

    * the template parses as a valid LDAP DN;
    * if ``{{username}}`` is present, it appears in a value position
      only — never as an attribute type — and appears at least once.

    Raises ``LDAPBindTemplateError`` on any violation.
    """
    if not template:
        # No template configured -> no validation work; caller renders
        # an empty DN. Skip the ldap import so this path works in
        # environments where python-ldap isn't installed.
        return []

    # ``str2dn`` raises ``ldap.DECODING_ERROR`` (or similar) for malformed
    # DN strings; we normalise to our own error type for callers.
    import ldap.dn  # local import keeps the module importable without python-ldap

    try:
        parsed = ldap.dn.str2dn(template)
    except Exception as exc:
        msg = f"Invalid LDAP bind DN template: {exc}"
        raise LDAPBindTemplateError(msg) from exc

    if _PLACEHOLDER in template:
        placeholder_seen = False
        for rdn in parsed:
            for attr, value, _flags in rdn:
                if _PLACEHOLDER in attr:
                    msg = (
                        "Invalid LDAP bind DN template: "
                        "{{username}} must appear in a value position, "
                        "not as the attribute type"
                    )
                    raise LDAPBindTemplateError(msg)
                if _PLACEHOLDER in value:
                    placeholder_seen = True
        if not placeholder_seen:
            # str2dn may have interpreted the curly braces as part of the
            # value (or split the DN oddly); refuse rather than guess.
            msg = (
                "Invalid LDAP bind DN template: "
                "{{username}} placeholder vanished after parsing"
            )
            raise LDAPBindTemplateError(msg)

    return parsed


def _render_bind_dn(
    parsed: list[list[tuple[str, str, int]]],
    username: str,
) -> str:
    """Substitute ``username`` into the parsed DN template.

    The username is escaped with ``ldap.dn.escape_dn_chars`` so injected
    commas / equals / pluses cannot break out of the value position.
    The result is reassembled with ``ldap.dn.dn2str``.
    """
    import ldap.dn

    escaped = ldap.dn.escape_dn_chars(username)
    rendered: list[list[tuple[str, str, int]]] = []
    for rdn in parsed:
        new_rdn: list[tuple[str, str, int]] = []
        for attr, raw_value, flags in rdn:
            if _PLACEHOLDER in raw_value:
                value = raw_value.replace(_PLACEHOLDER, escaped)
            else:
                value = raw_value
            new_rdn.append((attr, value, flags))
        rendered.append(new_rdn)
    return ldap.dn.dn2str(rendered)


# ---------------------------------------------------------------------------
# TLS — applied globally before ldap.initialize()
# ---------------------------------------------------------------------------


def _apply_tls_options() -> None:
    """Apply LDAP TLS settings via global ``ldap.set_option()`` calls.

    Per-connection TLS options do not work reliably in python-ldap — the
    underlying libldap reads these globals at ``initialize()`` time, so
    setting them on a connection object that has already been created
    is too late. This helper MUST be called before ``ldap.initialize()``.
    """
    import ldap

    if settings.ldap_ca_cert_file:
        ldap.set_option(ldap.OPT_X_TLS_CACERTFILE, settings.ldap_ca_cert_file)

    if settings.ldap_tls_require_cert:
        ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
    else:
        ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LDAPAuthProvider(IAuthProvider):
    """LDAP / Active Directory authentication provider.

    The bind DN template is validated eagerly in ``__init__`` if
    ``python-ldap`` is importable; otherwise validation is deferred to
    the first ``authenticate()`` call (typically inside a test mock
    context). In production this means malformed templates abort app
    startup — the desired behaviour.
    """

    def __init__(self) -> None:
        # ``_validated_for`` caches the bind DN string that the parsed
        # template corresponds to. If the setting changes (e.g. in a
        # test) the next ``authenticate()`` call re-validates.
        self._parsed_bind_dn: list[list[tuple[str, str, int]]] | None = None
        self._validated_for: str | None = None
        # Try to validate eagerly so a misconfigured template aborts
        # app startup. In environments where python-ldap isn't installed
        # (e.g. test mocks), validation is deferred to first
        # ``authenticate()`` call.
        with contextlib.suppress(ImportError):
            self._ensure_template_validated()

    @property
    def name(self) -> str:
        return "ldap"

    def _ensure_template_validated(self) -> None:
        """Validate the bind DN template, caching the parsed result."""
        current = settings.ldap_bind_dn
        if current == self._validated_for and self._parsed_bind_dn is not None:
            return  # cache hit
        if not current:
            # Empty bind DN: nothing to validate; rendered as empty string.
            self._parsed_bind_dn = []
            self._validated_for = ""
            return
        # Will raise LDAPBindTemplateError on a malformed template —
        # callers (including __init__) propagate it.
        self._parsed_bind_dn = _validate_bind_dn_template(current)
        self._validated_for = current

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not username or not password or db is None:
            return AuthResult(success=False, error="Username, password, and db session required")

        try:
            import ldap
            from ldap.filter import escape_filter_chars

            # Validate the bind DN template up-front so a misconfigured
            # template surfaces as an auth-time error rather than a
            # silent bind failure. (Validation is a no-op when cached.)
            self._ensure_template_validated()

            # TLS globals MUST be set before initialize() — see
            # _apply_tls_options docstring.
            _apply_tls_options()

            conn = ldap.initialize(settings.ldap_server_url)
            conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 10)
            conn.set_option(ldap.OPT_TIMEOUT, 10)

            safe_username = escape_filter_chars(username)
            user_dn = _render_bind_dn(self._parsed_bind_dn or [], safe_username)
            conn.simple_bind_s(user_dn, password)

            search_filter = f"(uid={safe_username})"
            results = conn.search_s(
                settings.ldap_search_base,
                ldap.SCOPE_SUBTREE,
                search_filter,
                ["uid", "mail", "cn", "memberOf"],
            )
            conn.unbind_s()

        except LDAPBindTemplateError as exc:
            # Don't leak template internals in user-facing error.
            logger.error("auth.ldap.bind_dn_template_invalid", error=str(exc))
            return AuthResult(success=False, error="LDAP configuration error")
        except Exception as exc:
            logger.exception("auth.ldap.bind_failed", error=str(exc))
            return AuthResult(success=False, error="Invalid credentials")

        if not results:
            return AuthResult(success=False, error="User not found in LDAP")

        _, ldap_attrs = results[0]
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
