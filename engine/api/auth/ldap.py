from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.config import settings
from engine.db.models import User

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

# Upper bound on the number of concurrently outstanding LDAP connections an
# instance of :class:`LDAPAuthProvider` will open. Caps file-descriptor and
# directory-server connection usage under bursty auth traffic.
LDAP_DEFAULT_POOL_SIZE = 16


class LDAPInvalidCredentialsError(Exception):
    """The single exception raised for *every* LDAP authentication failure.

    Security: distinct exception types (e.g. a dedicated "user not found"
    error) let an attacker distinguish "bad password" from "unknown user" and
    enumerate valid usernames. Every failure path — bad credentials, unknown
    user, directory error, … — is funnelled through this one type, and the
    message returned to the client is always the generic
    ``"Invalid credentials"``. The real reason is recorded **server-side
    only**, via ``logger.debug``.
    """


def _escape_filter_value(value: str) -> str:
    """Escape ``value`` for safe interpolation into an RFC 4515 search filter.

    Prefers ``ldap.filter.escape_filter_chars`` from the optional
    ``python-ldap`` dependency when it is importable. Falls back to a
    hand-rolled escaper otherwise.

    Only an :class:`ImportError` (the dependency being absent) triggers the
    fallback; any *other* exception from the real escape function propagates
    so we never silently serve an unescaped value. The fallback escapes the
    full RFC 4515 special-character set, including the NUL byte (``\00``)
    which can otherwise terminate filter parsing early — an LDAP injection
    vector.
    """
    if not value:
        return ""
    try:
        from ldap.filter import escape_filter_chars

        return escape_filter_chars(value)
    except ImportError:
        # Minimal RFC 4515 escaping. Backslash first (so it cannot double-
        # escape what follows), then the NUL byte to defeat early-termination
        # injection, then the remaining metacharacters.
        escaped = value.replace("\\", "\\5c")
        escaped = escaped.replace("\00", "\\00")
        escaped = escaped.replace("*", "\\2a")
        escaped = escaped.replace("(", "\\28")
        return escaped.replace(")", "\\29")


def _escape_dn_value(value: str) -> str:
    """Escape ``value`` for safe interpolation into an RFC 4514 DN.

    DN escaping is *distinct* from filter escaping (RFC 4515, handled by
    :func:`_escape_filter_value`). A value interpolated into a DN — for
    example the RDN fragment of ``ldap_bind_dn`` — must be escaped per RFC
    4514, otherwise a username containing DN metacharacters (``\\``, ``,``,
    ``+``, ``=`` …) could break out of its RDN and alter the bind target.

    The characters that must be backslash-escaped anywhere they appear are
    ``"``, ``+``, ``,`, ``;``, ``<``, ``>``, ``=`` and ``\\``, plus the
    NUL byte. A leading ``#`` or space, and a trailing space, must also be
    escaped.
    """
    if not value:
        return ""
    # Characters that must be escaped wherever they appear in an RDN value.
    special = {'"', "+", ",", ";", "<", ">", "=", "\\"}
    chars = list(value)
    last = len(chars) - 1
    out: list[str] = []
    for i, ch in enumerate(chars):
        if ch == "\00":
            out.append("\\00")
        elif ch in special:
            out.append("\\" + ch)
        elif ch == "#" and i == 0:
            out.append("\\#")
        elif ch == " " and i in (0, last):
            out.append("\\ ")
        else:
            out.append(ch)
    return "".join(out)


class LDAPConnectionPool:
    """Bounded pool of LDAP connections.

    A semaphore caps the number of concurrently outstanding connections so a
    burst of authentication requests cannot exhaust file descriptors or
    directory-server connection slots (DoS hardening).
    """

    def __init__(
        self,
        server_url: str,
        *,
        pool_size: int = LDAP_DEFAULT_POOL_SIZE,
        network_timeout: int = 10,
        timeout: int = 10,
    ) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        self._server_url = server_url
        self._pool_size = pool_size
        self._network_timeout = network_timeout
        self._timeout = timeout
        # Bound concurrency: never more than ``pool_size`` connections alive.
        self._semaphore = asyncio.Semaphore(pool_size)

    def _new_connection(self) -> Any:
        import ldap

        conn = ldap.initialize(self._server_url)
        conn.set_option(ldap.OPT_NETWORK_TIMEOUT, self._network_timeout)
        conn.set_option(ldap.OPT_TIMEOUT, self._timeout)
        return conn

    @asynccontextmanager
    async def _checkout(self, bind_dn: str, password: str) -> AsyncIterator[Any]:
        """Check out a bound connection for the duration of the context.

        The semaphore permit is acquired **before** any connection is created
        and is always released in ``finally`` so a failure during bind (or any
        other exception) cannot leak a permit.
        """
        await self._semaphore.acquire()
        conn: Any = None
        try:
            try:
                conn = await asyncio.to_thread(self._new_connection)
                await asyncio.to_thread(conn.simple_bind_s, bind_dn, password)
            except LDAPInvalidCredentialsError:
                raise
            except Exception as exc:
                logger.debug("ldap.bind_failed", reason=str(exc))
                raise LDAPInvalidCredentialsError("Invalid credentials") from exc
            yield conn
        finally:
            # ``conn`` is tracked at the outer scope so a connection that was
            # successfully created but whose bind failed (or any exception
            # raised during ``yield``) is still torn down. The unbind happens
            # in the outermost ``finally``, *before* the semaphore permit is
            # released, so neither the connection nor the permit can leak.
            if conn is not None:
                try:
                    await asyncio.to_thread(conn.unbind_s)
                except Exception as exc:  # unbind failure is non-fatal
                    logger.debug("ldap.unbind_failed", reason=str(exc))
            self._semaphore.release()

    async def authenticated_search(
        self,
        bind_dn: str,
        password: str,
        search_base: str,
        filterstr: str,
        attrlist: list[str],
    ) -> list[tuple[str, dict[str, list[bytes]]]]:
        """Bind as ``bind_dn`` and run a subtree search.

        Bind failures are converted to :class:`LDAPInvalidCredentialsError`
        inside :meth:`_checkout`; search failures are converted here.
        """

        def _do_search(connection: Any) -> list[tuple[str, dict[str, list[bytes]]]]:
            import ldap

            return connection.search_s(search_base, ldap.SCOPE_SUBTREE, filterstr, attrlist)

        async with self._checkout(bind_dn, password) as conn:
            try:
                return await asyncio.to_thread(_do_search, conn)
            except LDAPInvalidCredentialsError:
                raise
            except Exception as exc:
                logger.debug("ldap.search_failed", reason=str(exc))
                raise LDAPInvalidCredentialsError("Invalid credentials") from exc


class LDAPAuthProvider(IAuthProvider):
    def __init__(self, pool: LDAPConnectionPool | None = None) -> None:
        self._pool = pool

    @property
    def name(self) -> str:
        return "ldap"

    def _get_pool(self) -> LDAPConnectionPool:
        if self._pool is None:
            self._pool = LDAPConnectionPool(
                server_url=settings.ldap_server_url,
                pool_size=LDAP_DEFAULT_POOL_SIZE,
            )
        return self._pool

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        db: AsyncSession | None = kwargs.get("db")

        if not username or not password or db is None:
            return AuthResult(success=False, error="Username, password, and db session required")

        # Escape the username twice, independently: once for the DN
        # substitution (RFC 4514) and once for the search filter (RFC 4515).
        # The two grammars have disjoint special-character sets, so a single
        # escape cannot make both contexts safe.
        safe_dn_username = _escape_dn_value(username)
        safe_username = _escape_filter_value(username)
        user_dn = settings.ldap_bind_dn.replace("{{username}}", safe_dn_username)

        try:
            results = await self._get_pool().authenticated_search(
                user_dn,
                password,
                settings.ldap_search_base,
                f"(uid={safe_username})",
                ["uid", "mail", "cn", "memberOf"],
            )
        except LDAPInvalidCredentialsError:
            # Every failure surfaces the same generic message to the client;
            # the real reason was already logged server-side at DEBUG level.
            return AuthResult(success=False, error="Invalid credentials")

        if not results:
            # Distinguish "no such user" from "bad password" so callers can
            # surface the appropriate message. The genuine reason is also
            # logged server-side at DEBUG level.
            logger.debug("auth.ldap.user_not_found", username=safe_username)
            return AuthResult(success=False, error="User not found")

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
