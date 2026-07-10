"""LDAP authentication provider.

This module implements LDAP / Active Directory authentication as a provider
in the :mod:`engine.auth` multi-provider package, mirroring the structure of
the OAuth2 / OIDC providers (:mod:`engine.auth.providers.google`,
:mod:`engine.auth.github`):

* **Typed exceptions** -- every failure mode raises a subclass of
  :class:`LDAPAuthError`, which itself derives from the shared
  :class:`engine.auth.base.OAuthError`. ``except OAuthError`` therefore
  catches LDAP failures alongside Google / GitHub failures, exactly as the
  package-level documentation promises.
* **Lazy ldap3 import** -- :mod:`ldap3` is imported inside methods (never at
  module top level) so merely importing this package never fails when the
  optional LDAP dependency is absent, and so unit tests can inject a
  connection factory that never touches the network.
* **Injectable connection factory** -- :meth:`LDAPAuthProvider.authenticate`
  is fully testable: a caller injects a ``connection_factory`` that returns
  ``ldap3.Connection``-like objects, so happy-path / auth-failure /
  connection-error scenarios run with no LDAP server.

Authentication strategy
-----------------------
The provider uses the robust *search-then-bind* flow rather than a fragile
bind-DN template:

1. A service account (``bind_dn`` / ``bind_password``) binds via a connection
   drawn from :class:`LDAPConnectionPool` and searches ``search_base`` with
   ``search_filter`` to resolve the user's DN and directory attributes.
2. A short-lived connection binds as that user DN with the supplied password.
   A successful bind proves the credentials; a failed bind raises
   :class:`LDAPInvalidCredentialsError`.

This decouples "where is the user" (search) from "is the password right"
(bind) and supports any directory layout expressible as a search filter.

Connection pooling
------------------
:class:`LDAPConnectionPool` maintains up to ``pool_size`` service-bound
``ldap3.Connection`` objects for the (frequent) search step. Connections are
reused across authentications, validated (``.bound``) before reuse, and
discarded if the directory dropped them. The per-user bind verification
always uses a fresh transient connection because each bind identity is
unique.
"""

from __future__ import annotations

import threading
import urllib.parse
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from engine.auth.base import OAuthError

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = structlog.get_logger()

# --- Internal role hierarchy ----------------------------------------------
# Mirrors the engine's privilege hierarchy so :meth:`LDAPAuthProvider.map_roles`
# can collapse a list of LDAP group memberships (mapped to internal roles via
# ``role_mapping``) down to a single highest-privilege role. Kept local so
# this provider has no compile-time dependency on ``engine.api.auth``.
_ROLE_PRIORITY: dict[str, int] = {
    "viewer": 0,
    "user": 1,
    "retail_trader": 2,
    "developer": 4,
    "portfolio_manager": 5,
    "admin": 6,
}

# The placeholder substituted with the (escaped) username inside
# ``search_filter``. Using ``{username}`` keeps the filter human-readable in
# configuration (e.g. ``(uid={username})``).
_USERNAME_PLACEHOLDER = "{username}"

# LDAP attribute types returned by a directory are *lists* of values (an
# attribute can be multi-valued). Single-valued convenience attributes (uid,
# mail, cn) are reduced to their first element; multi-valued ones (memberOf)
# stay as lists.
_SINGLE_VALUED = ("uid", "mail", "cn", "displayName", "sAMAccountName")


# --- Exceptions ------------------------------------------------------------
class LDAPAuthError(OAuthError):
    """Base class for every error raised by the LDAP provider.

    Subclasses :class:`engine.auth.base.OAuthError` so a single
    ``except OAuthError`` catches LDAP failures as well as Google / GitHub
    failures -- the package-wide contract.
    """


class LDAPConfigurationError(LDAPAuthError):
    """Raised when the provider is misconfigured (missing server URL, base DN,
    bind DN, or search filter) or invoked with empty credentials.

    A configuration error is a programmer/deployment fault, not an
    authentication failure, so it is surfaced loudly rather than reported as
    "invalid credentials".
    """


class LDAPConnectionError(LDAPAuthError):
    """Raised when the directory cannot be reached or a transport-level error
    occurs during bind/search (DNS failure, connection refused, TLS error,
    socket timeout).

    Distinct from :class:`LDAPInvalidCredentialsError` so callers can retry
    or report "directory unavailable" without leaking whether a user exists.
    """


class LDAPInvalidCredentialsError(LDAPAuthError):
    """Raised when the user bind fails because the password is wrong or the
    account is locked. Maps to the classic "invalid credentials" outcome.

    The error message is intentionally generic to avoid leaking whether the
    username exists.
    """


class LDAPUserNotFoundError(LDAPAuthError):
    """Raised when the search step resolves no directory entry for the user."""


# --- Result ----------------------------------------------------------------
@dataclass
class LDAPUser:
    """Normalized view of an authenticated directory user.

    ``attributes`` preserves the raw decoded attributes returned by the
    directory for callers that need claims we do not model explicitly.
    """

    dn: str
    username: str
    email: str = ""
    display_name: str = ""
    roles: list[str] = field(default_factory=lambda: ["user"])
    groups: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


# Type alias for the injectable connection factory. It must return an
# ``ldap3.Connection``-like object exposing ``bind()``, ``unbind()``,
# ``search()``, ``response`` and ``bound``. Defaulting to ``Any`` keeps the
# optional ``ldap3`` dependency out of the module import graph.
ConnectionFactory = Callable[..., Any]


def _import_ldap3() -> Any:
    """Import and return :mod:`ldap3`, raising a helpful error if absent.

    Imported lazily so the module (and the :mod:`engine.auth` package) can be
    imported on systems without the optional LDAP dependency installed.
    """
    try:
        import ldap3  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise LDAPConfigurationError(
            "ldap3 is required for LDAP authentication; install the 'ldap' extra"
        ) from exc
    return ldap3


def _parse_server_url(server_url: str) -> tuple[str, int | None, bool]:
    """Split ``server_url`` into ``(host, port, use_ssl)``.

    Accepts ``ldap://host:port``, ``ldaps://host:port`` or a bare host. The
    ``use_ssl`` flag is inferred from the scheme (``ldaps`` => TLS) but can
    be overridden on the provider.
    """
    parsed = urllib.parse.urlparse(server_url if "://" in server_url else f"ldap://{server_url}")
    scheme = (parsed.scheme or "ldap").lower()
    use_ssl = scheme == "ldaps"
    host = parsed.hostname or server_url
    port = parsed.port
    return host, port, use_ssl


def _escape_filter_value(value: str) -> str:
    """LDAP-escape a value before interpolating it into a search filter.

    Prevents LDAP-injection (e.g. a username of ``*)(uid=*))``) by routing
    through ``ldap3.utils.conv.escape_filter_chars`` when available, with a
    hand-rolled fallback so correctness does not depend on the ldap3 version.
    """
    try:
        from ldap3.utils.conv import escape_filter_chars  # noqa: PLC0415

        return escape_filter_chars(value)
    except Exception:
        # Minimal RFC4515-aware escaping of the metacharacters that matter.
        return value.translate(
            str.maketrans({"\\": r"\5c", "*": r"\2a", "(": r"\28", ")": r"\29"})
        )


# --- Connection pool -------------------------------------------------------
class LDAPConnectionPool:
    """A bounded pool of reusable service-bound LDAP connections.

    The pool hands out already-bound connections for the (frequent) search
    step and returns them when released, amortizing the cost of TCP + TLS +
    bind handshakes across many authentications. It is thread-safe and
    degrades gracefully: a connection dropped by the directory (``.bound``
    is False on acquire) is discarded and replaced rather than reused.

    Connections are created through the injected ``factory`` (a zero-argument
    callable returning a *bound* connection) so the pool is fully unit-testable
    without a live directory.
    """

    def __init__(self, factory: Callable[[], Any], *, pool_size: int = 5) -> None:
        if pool_size < 1:
            raise LDAPConfigurationError("pool_size must be >= 1")
        self._factory = factory
        self._pool_size = pool_size
        self._idle: deque[Any] = deque()
        self._lock = threading.Lock()
        self._closed = False

    @contextmanager
    def acquire(self) -> Iterator[Any]:
        """Borrow a bound service connection for the duration of a ``with`` block.

        The connection is returned to the pool on exit. If the pool is empty a
        new connection is created via ``factory``.
        """
        conn = self._checkout()
        try:
            yield conn
        finally:
            self._checkin(conn)

    def _checkout(self) -> Any:
        with self._lock:
            if self._closed:
                raise LDAPConfigurationError("LDAP connection pool is closed")
            while self._idle:
                conn = self._idle.popleft()
                # Reuse only live connections; discard dropped ones so a
                # directory-side timeout never surfaces as a silent failure.
                if getattr(conn, "bound", False):
                    return conn
                _safe_unbind(conn)
        # Outside the lock: creating a connection may block on the network.
        return self._factory()

    def _checkin(self, conn: Any) -> None:
        if not getattr(conn, "bound", False):
            _safe_unbind(conn)
            return
        with self._lock:
            if self._closed or len(self._idle) >= self._pool_size:
                _safe_unbind(conn)
                return
            self._idle.append(conn)

    def close(self) -> None:
        """Close every idle connection and mark the pool unusable."""
        with self._lock:
            self._closed = True
            while self._idle:
                _safe_unbind(self._idle.popleft())


def _safe_unbind(conn: Any) -> None:
    """Best-effort ``unbind``; never raises on a half-closed connection."""
    unbind = getattr(conn, "unbind", None)
    if callable(unbind):
        try:
            unbind()
        except Exception:
            logger.debug("auth.ldap.unbind_failed", exc_info=True)


# --- Provider --------------------------------------------------------------
class LDAPAuthProvider:
    """LDAP / Active Directory authentication provider.

    Parameters mirror the standard directory configuration. Everything needed
    for a connection -- the ``ldap3.Server`` and the :class:`LDAPConnectionPool`
    -- is built lazily on first use so an unconfigured provider is cheap to
    construct and simple to introspect.

    For testability, ``connection_factory`` may be injected. It is a callable
    ``(user, password) -> connection`` returning an *unbound*
    ``ldap3.Connection``-like object; the provider performs the bind itself so
    a test double only needs to implement ``bind`` / ``unbind`` / ``search`` /
    ``response`` / ``bound``.
    """

    def __init__(
        self,
        *,
        server_url: str,
        bind_dn: str,
        bind_password: str,
        search_base: str,
        search_filter: str = f"(uid={_USERNAME_PLACEHOLDER})",
        attributes: list[str] | None = None,
        role_mapping: dict[str, str] | None = None,
        use_ssl: bool | None = None,
        connect_timeout: float = 10.0,
        receive_timeout: float = 10.0,
        pool_size: int = 5,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if not server_url:
            raise LDAPConfigurationError("server_url is required")
        if not bind_dn:
            raise LDAPConfigurationError("bind_dn (service account) is required")
        if not search_base:
            raise LDAPConfigurationError("search_base is required")
        if _USERNAME_PLACEHOLDER not in search_filter:
            raise LDAPConfigurationError(
                f"search_filter must contain {_USERNAME_PLACEHOLDER!r} placeholder"
            )

        self.server_url = server_url
        self.bind_dn = bind_dn
        self.bind_password = bind_password
        self.search_base = search_base
        self.search_filter = search_filter
        self.attributes = list(attributes) if attributes else ["uid", "mail", "cn", "memberOf"]
        self.role_mapping = dict(role_mapping) if role_mapping else {}
        self.connect_timeout = connect_timeout
        self.receive_timeout = receive_timeout
        self.pool_size = pool_size

        host, port, inferred_ssl = _parse_server_url(server_url)
        self._host = host
        self._port = port
        self._use_ssl = inferred_ssl if use_ssl is None else use_ssl

        self._server: Any = None  # built lazily
        self._pool: LDAPConnectionPool | None = None  # built lazily
        self._lock = threading.Lock()
        self._connection_factory = connection_factory

    @property
    def name(self) -> str:
        """Stable lowercase provider identifier used as a registry key."""
        return "ldap"

    # -- Lazy resource construction -----------------------------------------
    def _build_server(self) -> Any:
        """Build (once) and cache the underlying ``ldap3.Server``."""
        if self._server is None:
            ldap3 = _import_ldap3()
            self._server = ldap3.Server(
                self._host,
                port=self._port,
                use_ssl=self._use_ssl,
                connect_timeout=self.connect_timeout,
                get_info=ldap3.NONE,
            )
        return self._server

    def _default_connection_factory(self, user: str | None, password: str | None) -> Any:
        """Construct an *unbound* ``ldap3.Connection`` against the cached server.

        ``raise_exceptions=True`` makes bind/search failures raise typed
        ``ldap3`` exceptions (instead of silently returning ``False``) so the
        provider can map them precisely. ``pool_name`` / ``pool_size`` enable
        ldap3's own socket-level pooling underneath our application pool.
        """
        ldap3 = _import_ldap3()
        return ldap3.Connection(
            self._build_server(),
            user=user,
            password=password,
            auto_bind=ldap3.AUTO_BIND_NONE,
            client_strategy="SYNC",
            read_only=False,
            raise_exceptions=True,
            receive_timeout=self.receive_timeout,
            pool_name=f"nexus-ldap-{self.name}",
            pool_size=self.pool_size,
        )

    def _make_connection(self, user: str | None, password: str | None) -> Any:
        """Return an unbound connection via the configured/injected factory."""
        factory = self._connection_factory or self._default_connection_factory
        return factory(user, password)

    def _get_pool(self) -> LDAPConnectionPool:
        """Lazily build the service-connection pool (thread-safe)."""
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = LDAPConnectionPool(
                        self._create_service_connection, pool_size=self.pool_size
                    )
        return self._pool

    def _create_service_connection(self) -> Any:
        """Factory for the pool: a connection bound with the service account."""
        conn = self._make_connection(self.bind_dn, self.bind_password)
        self._bind(conn, identity=self.bind_dn, kind="service")
        return conn

    # -- Core operations ----------------------------------------------------
    def _bind(self, conn: Any, *, identity: str, kind: str) -> None:
        """Bind ``conn`` and map any failure to a typed provider exception."""
        try:
            ok = conn.bind()
        except Exception as exc:
            raise self._map_bind_exception(exc) from exc
        if not ok:
            # Non-raising strategy: treat a False bind as invalid credentials
            # for the user bind, and as a connection problem for the service
            # bind (a service bind should never fail on valid credentials).
            if kind == "service":
                raise LDAPConnectionError("LDAP service bind failed") from None
            raise LDAPInvalidCredentialsError("Invalid credentials") from None
        logger.debug("auth.ldap.bind_ok", kind=kind, identity=identity)

    @staticmethod
    def _map_bind_exception(exc: Exception) -> LDAPAuthError:
        """Translate an ``ldap3`` bind exception into a typed provider error.

        Classified from the exception *name* (rather than ``isinstance``
        against ldap3 classes) so the mapping still works when ldap3 raises a
        subclass we did not import, and so unit tests can raise plain
        builtins (e.g. ``ConnectionError``) to drive each branch.
        """
        name = type(exc).__name__
        message = str(exc) or name
        if "InvalidCredentials" in name:
            return LDAPInvalidCredentialsError("Invalid credentials")
        # Socket / connection / timeout family -> directory unavailable.
        if any(
            token in name
            for token in ("Socket", "Connection", "Timeout", "Unavailable", "Open")
        ):
            return LDAPConnectionError(f"LDAP connection error: {message}")
        return LDAPAuthError(f"LDAP bind error: {message}")

    def search_user(self, username: str) -> tuple[str, dict[str, Any]]:
        """Resolve ``username`` to its ``(dn, attributes)`` via a pooled search.

        Uses a service-bound connection from the pool. Raises
        :class:`LDAPUserNotFoundError` when no entry matches,
        :class:`LDAPConnectionError` on transport failure.
        """
        ldap3 = _import_ldap3()
        escaped = _escape_filter_value(username)
        filt = self.search_filter.replace(_USERNAME_PLACEHOLDER, escaped)

        pool = self._get_pool()
        with pool.acquire() as conn:
            try:
                conn.search(
                    search_base=self.search_base,
                    search_scope=ldap3.SUBTREE,
                    search_filter=filt,
                    attributes=self.attributes,
                )
            except Exception as exc:
                raise self._map_search_exception(exc) from exc
            entries = _read_search_response(conn)

        if not entries:
            raise LDAPUserNotFoundError(f"User {username!r} not found in directory")
        if len(entries) > 1:
            logger.warning("auth.ldap.multiple_matches", username=username, count=len(entries))
        dn, attrs = entries[0]
        return dn, attrs

    @staticmethod
    def _map_search_exception(exc: Exception) -> LDAPAuthError:
        """Translate a search exception (transport vs other) into a typed error."""
        name = type(exc).__name__
        message = str(exc) or name
        if any(token in name for token in ("Socket", "Connection", "Timeout", "Unavailable")):
            return LDAPConnectionError(f"LDAP search error: {message}")
        return LDAPAuthError(f"LDAP search error: {message}")

    def _verify_user_bind(self, user_dn: str, password: str) -> None:
        """Bind as ``user_dn`` with ``password`` to prove the credentials.

        Uses a *transient* connection (not pooled): every user bind has a
        unique identity, so pooling would never reuse the socket.
        """
        conn = self._make_connection(user_dn, password)
        try:
            self._bind(conn, identity=user_dn, kind="user")
        finally:
            _safe_unbind(conn)

    # -- Public API ---------------------------------------------------------
    def authenticate(self, username: str, password: str) -> LDAPUser:
        """Authenticate ``username`` / ``password`` against the directory.

        Flow: search for the user DN (service-bound, pooled), then bind as
        that user (transient) to verify the password. On success returns the
        normalized :class:`LDAPUser` with directory attributes and the mapped
        internal role(s).

        Raises:
            LDAPConfigurationError: empty username/password or bad config.
            LDAPConnectionError: directory unreachable / transport error.
            LDAPInvalidCredentialsError: wrong password / locked account.
            LDAPUserNotFoundError: no directory entry for the username.
        """
        if not username or not isinstance(username, str):
            raise LDAPConfigurationError("username is required")
        if not password or not isinstance(password, str):
            raise LDAPConfigurationError("password is required")

        user_dn, attrs = self.search_user(username)
        self._verify_user_bind(user_dn, password)

        groups = _coerce_list(attrs.get("memberOf") or attrs.get("memberOf;range") or [])
        roles = self._resolve_roles(groups)
        display_name = (
            _first(attrs.get("cn"))
            or _first(attrs.get("displayName"))
            or username
        )
        email = _first(attrs.get("mail")) or ""

        logger.info("auth.ldap.login_success", username=username, roles=roles)
        return LDAPUser(
            dn=user_dn,
            username=_first(attrs.get("uid")) or _first(attrs.get("sAMAccountName")) or username,
            email=email,
            display_name=display_name,
            roles=roles,
            groups=groups,
            attributes=attrs,
        )

    # -- Role mapping -------------------------------------------------------
    def _resolve_roles(self, groups: list[str]) -> list[str]:
        """Map directory group DNs to internal roles via ``role_mapping``.

        ``role_mapping`` keys are matched as case-insensitive substrings
        against each group DN (so ``"cn=admins"`` matches
        ``"cn=admins,ou=groups,dc=example,dc=com"``). Defaults to ``["user"]``
        when nothing maps.
        """
        if not self.role_mapping:
            return ["user"]
        mapped: list[str] = []
        for group_dn in groups:
            lowered = group_dn.lower()
            for pattern, role in self.role_mapping.items():
                if pattern.lower() in lowered:
                    mapped.append(role)
        if not mapped:
            return ["user"]
        # Deduplicate while preserving order, then collapse to the single
        # highest-privilege role; surface the full set too for callers.
        seen: set[str] = set()
        ordered = [r for r in mapped if not (r in seen or seen.add(r))]
        ordered.sort(key=lambda r: _ROLE_PRIORITY.get(r, -1), reverse=True)
        return ordered or ["user"]

    def map_roles(self, groups: list[str]) -> str:
        """Reduce directory group memberships to a single internal role.

        Convenience wrapper around :meth:`_resolve_roles` returning just the
        highest-privilege role (``"user"`` when nothing maps). Kept for
        symmetry with the other providers' role helpers.
        """
        roles = self._resolve_roles(groups)
        return roles[0]

    # -- Lifecycle ----------------------------------------------------------
    def close(self) -> None:
        """Release the pooled service connections."""
        if self._pool is not None:
            self._pool.close()


# --- Attribute helpers -----------------------------------------------------
def _first(value: Any) -> str:
    """Return the first element of an LDAP multi-valued attribute as ``str``."""
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        if not value:
            return ""
        value = value[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _coerce_list(value: Any) -> list[str]:
    """Normalize an LDAP attribute (scalar or list) to a list of ``str``."""
    if value is None:
        return []
    items = list(value) if isinstance(value, list | tuple) else [value]
    result: list[str] = []
    for item in items:
        if isinstance(item, bytes):
            result.append(item.decode("utf-8", errors="replace"))
        elif item is not None:
            result.append(str(item))
    return result


def _read_search_response(conn: Any) -> list[tuple[str, dict[str, Any]]]:
    """Extract ``[(dn, attributes), ...]`` from an ldap3 connection post-search.

    Supports both the dict-style ``conn.response`` (SYNC strategy) and the
    object-style ``conn.entries`` (used by some strategies / test doubles),
    so the provider is robust to the connection strategy in use.
    """
    entries: list[tuple[str, dict[str, Any]]] = []

    response = getattr(conn, "response", None)
    if isinstance(response, list):
        for entry in response:
            if not isinstance(entry, dict):
                continue
            dn = entry.get("dn")
            attrs = entry.get("attributes") or {}
            if dn is None:
                continue
            entries.append((str(dn), _normalize_attributes(attrs)))
        if entries:
            return entries

    obj_entries = getattr(conn, "entries", None)
    if isinstance(obj_entries, list):
        for entry in obj_entries:
            dn = getattr(entry, "entry_dn", None)
            attrs_fn = getattr(entry, "entry_attributes_as_dict", None)
            if dn is None:
                continue
            attrs = attrs_fn() if callable(attrs_fn) else {}
            entries.append((str(dn), _normalize_attributes(attrs)))
    return entries


def _normalize_attributes(attrs: Any) -> dict[str, Any]:
    """Coerce an ldap3 attribute mapping into a plain ``dict[str, Any]``.

    ldap3 returns attributes as a ``CaseInsensitiveDict`` / ``cidict``; we
    keep the value lists intact (callers consume them via :func:`_first` /
    :func:`_coerce_list`).
    """
    if isinstance(attrs, dict):
        return dict(attrs)
    # Some objects expose attribute access instead of mapping access.
    if hasattr(attrs, "items"):
        return dict(attrs.items())
    return {}
