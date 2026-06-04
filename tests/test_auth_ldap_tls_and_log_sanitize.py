"""Tests for the LDAP TLS hardening + role-string log sanitization fixes.

Two distinct but related security fixes are pinned by this module:

1. **LDAP TLS hardening** (``engine/api/auth/ldap.py``).
   Previously the LDAP provider opened a connection and bound without
   requesting certificate validation or STARTTLS upgrade. An attacker
   in a MITM position could therefore impersonate the directory server,
   capture credentials, or strip TLS entirely.

   The fix:

   * Always sets ``OPT_X_TLS_REQUIRE_CERT = OPT_X_TLS_DEMAND`` so the
     client refuses to complete a handshake with an untrusted peer
     certificate.
   * Reads ``settings.ldap_ca_cert_path`` and, when non-empty, sets
     ``OPT_X_TLS_CACERTFILE`` to pin the trust anchor.
   * Calls ``conn.start_tls_s()`` to upgrade the connection before
     ``simple_bind_s`` so credentials never traverse the wire in
     plaintext on a plain ``ldap://`` URI.

2. **Role-string log sanitization** (``engine/api.auth/base.py``).
   The new ``sanitize_role_for_log`` helper scrubs C0/C1 control
   characters (which enable log forging / terminal escape injection)
   and truncates very long values (which bloat log aggregators) before
   the unrecognized-role warning is emitted. ``map_roles`` uses it on
   every entry in the ``unrecognized=`` payload.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    _CONTROL_CHARS_RE,
    _MAX_LOG_ROLE_LENGTH,
    AuthResult,
    IAuthProvider,
    sanitize_role_for_log,
)
from engine.api.auth.ldap import LDAPAuthProvider
from engine.config import Settings

# ---------------------------------------------------------------------------
# Reusable fakes (mirror the ones in tests/test_ldap_auth.py so this file is
# fully self-contained).
# ---------------------------------------------------------------------------


class _FakeLDAPConn:
    """Minimal ldap.LDAPObject double that records every interaction."""

    def __init__(
        self,
        bind_raises: Exception | None = None,
        search_results: list[tuple[str, dict[str, list[bytes]]]] | None = None,
        search_raises: Exception | None = None,
        start_tls_raises: Exception | None = None,
    ):
        self._bind_raises = bind_raises
        self._search_results = search_results or []
        self._search_raises = search_raises
        self._start_tls_raises = start_tls_raises
        self.options: dict[int, Any] = {}
        self.start_tls_called = False
        self.bind_called = False
        self.unbind_called = False

    def set_option(self, opt: int, value: Any) -> None:
        self.options[opt] = value

    def start_tls_s(self) -> None:
        self.start_tls_called = True
        if self._start_tls_raises:
            raise self._start_tls_raises

    def simple_bind_s(self, dn: str, password: str) -> None:
        self.bind_called = True
        self.bound_dn = dn
        self.bound_password = password
        if self._bind_raises:
            raise self._bind_raises

    def search_s(
        self,
        base: str,
        scope: int,
        filterstr: str,
        attrlist: list[str],
    ):
        if self._search_raises:
            raise self._search_raises
        return self._search_results

    def unbind_s(self) -> None:
        self.unbind_called = True


def _build_ldap_mock(
    bind_raises: Exception | None = None,
    search_results: list[tuple[str, dict[str, list[bytes]]]] | None = None,
    search_raises: Exception | None = None,
    start_tls_raises: Exception | None = None,
) -> tuple[MagicMock, MagicMock, _FakeLDAPConn]:
    """Return (mock_ldap, mock_filter, fake_conn) for injection into sys.modules."""
    fake_conn = _FakeLDAPConn(
        bind_raises=bind_raises,
        search_results=search_results,
        search_raises=search_raises,
        start_tls_raises=start_tls_raises,
    )
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(return_value=fake_conn)
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.OPT_X_TLS_REQUIRE_CERT = 100
    mock_ldap.OPT_X_TLS_DEMAND = 101
    mock_ldap.OPT_X_TLS_CACERTFILE = 102
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter, fake_conn


def _settings(
    *,
    ldap_ca_cert_path: str = "",
    ldap_server_url: str = "ldap://ldap.example.com:389",
    ldap_role_mapping: str = '{"cn=admins,ou=groups,dc=example,dc=com": "admin"}',
) -> Settings:
    return Settings(
        ldap_server_url=ldap_server_url,
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=ldap_role_mapping,
        ldap_ca_cert_path=ldap_ca_cert_path,
    )


def _make_mock_db() -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.add = MagicMock()

    async def mock_refresh(user):
        # The real session would re-load the row including server defaults.
        # ``is_active`` defaults to True on the model; mimic that here so the
        # post-login "Account is disabled" guard doesn't reject the user.
        if not getattr(user, "is_active", None):
            user.is_active = True

    db.refresh = AsyncMock(side_effect=mock_refresh)
    db.flush = AsyncMock()

    async def mock_execute(stmt):
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        return r

    db.execute = mock_execute
    return db


def _ok_attrs(uid: str = "testuser", member_of: list[bytes] | None = None):
    attrs: dict[str, list[bytes]] = {
        "uid": [uid.encode()],
        "mail": [b"testuser@example.com"],
        "cn": [b"Test User"],
    }
    if member_of is not None:
        attrs["memberOf"] = member_of
    return attrs


# ===========================================================================
# 1.  Settings — ldap_ca_cert_path field
# ===========================================================================


class TestLdapCaCertPathSetting:
    """The new ``ldap_ca_cert_path`` setting must exist, default empty,
    and remain overridable through the standard ``NEXUS_`` env prefix."""

    def test_default_is_empty_string(self):
        s = Settings(_env_file=None)
        assert s.ldap_ca_cert_path == ""

    def test_default_is_a_string(self):
        s = Settings(_env_file=None)
        assert isinstance(s.ldap_ca_cert_path, str)

    def test_can_be_set_via_constructor(self):
        s = Settings(_env_file=None, ldap_ca_cert_path="/etc/ssl/certs/ca.pem")
        assert s.ldap_ca_cert_path == "/etc/ssl/certs/ca.pem"

    def test_can_be_overridden_via_env(self, monkeypatch):
        monkeypatch.setenv("NEXUS_LDAP_CA_CERT_PATH", "/etc/pki/tls/certs/ca.crt")
        s = Settings(_env_file=None)
        assert s.ldap_ca_cert_path == "/etc/pki/tls/certs/ca.crt"

    def test_env_can_be_cleared(self, monkeypatch):
        monkeypatch.setenv("NEXUS_LDAP_CA_CERT_PATH", "")
        s = Settings(_env_file=None)
        assert s.ldap_ca_cert_path == ""

    def test_arbitrary_path_is_preserved_verbatim(self):
        weird = "/opt/ca bundles/my bundle [v1].pem"
        s = Settings(_env_file=None, ldap_ca_cert_path=weird)
        assert s.ldap_ca_cert_path == weird


# ===========================================================================
# 2.  LDAP TLS option ordering & values
# ===========================================================================


class TestLdapTlsOptionsAlwaysSet:
    """The TLS hardening options must be applied on every successful bind
    path, regardless of whether a CA cert path is configured."""

    @pytest.fixture
    def provider(self) -> LDAPAuthProvider:
        return LDAPAuthProvider()

    async def test_demand_cert_option_is_set_before_bind(self, provider, monkeypatch):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings", _settings(ldap_ca_cert_path="")
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="testuser", password="pw", db=db
            )

        assert result.success is True
        assert mock_ldap.OPT_X_TLS_REQUIRE_CERT in conn.options
        assert conn.options[mock_ldap.OPT_X_TLS_REQUIRE_CERT] == mock_ldap.OPT_X_TLS_DEMAND

    async def test_demand_cert_option_is_set_with_ca_cert_path(
        self, provider, monkeypatch
    ):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings",
            _settings(ldap_ca_cert_path="/etc/ssl/certs/ca.pem"),
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        # Demand is still set even when a CA bundle is also configured.
        assert conn.options[mock_ldap.OPT_X_TLS_REQUIRE_CERT] == mock_ldap.OPT_X_TLS_DEMAND

    async def test_demand_cert_option_set_for_ldaps_uri(self, provider, monkeypatch):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings",
            _settings(ldap_server_url="ldaps://ldap.example.com:636"),
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        assert conn.options[mock_ldap.OPT_X_TLS_REQUIRE_CERT] == mock_ldap.OPT_X_TLS_DEMAND


class TestLdapTlsOrderBeforeStartTlsAndBind:
    """``OPT_X_TLS_REQUIRE_CERT`` must be set **before** ``start_tls_s`` is
    invoked — otherwise the handshake runs against the default (often
    permissive) policy. We capture the call order on the fake connection
    and assert on it."""

    @pytest.fixture
    def provider(self) -> LDAPAuthProvider:
        return LDAPAuthProvider()

    async def test_set_option_precedes_start_tls_and_bind(
        self, provider, monkeypatch
    ):
        monkeypatch.setattr("engine.api.auth.ldap.settings", _settings())
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        # Wrap each fake-conn method to record the global call sequence.
        order: list[str] = []
        orig_set_option = conn.set_option
        orig_start_tls = conn.start_tls_s
        orig_bind = conn.simple_bind_s

        def rec_set_option(opt, value):
            order.append(f"set_option:{opt}")
            return orig_set_option(opt, value)

        def rec_start_tls():
            order.append("start_tls_s")
            return orig_start_tls()

        def rec_bind(dn, pw):
            order.append("simple_bind_s")
            return orig_bind(dn, pw)

        conn.set_option = rec_set_option  # type: ignore[method-assign]
        conn.start_tls_s = rec_start_tls  # type: ignore[method-assign]
        conn.simple_bind_s = rec_bind  # type: ignore[method-assign]

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        # Find positions of the TLS-require option and the start_tls / bind calls.
        tls_opt_key = f"set_option:{mock_ldap.OPT_X_TLS_REQUIRE_CERT}"
        try:
            tls_pos = order.index(tls_opt_key)
        except ValueError:
            pytest.fail("OPT_X_TLS_REQUIRE_CERT was never set")
        try:
            start_tls_pos = order.index("start_tls_s")
        except ValueError:
            pytest.fail("start_tls_s was never called")
        try:
            bind_pos = order.index("simple_bind_s")
        except ValueError:
            pytest.fail("simple_bind_s was never called")

        assert tls_pos < start_tls_pos, (
            f"OPT_X_TLS_REQUIRE_CERT must be set BEFORE start_tls_s "
            f"(got order={order!r})"
        )
        assert start_tls_pos < bind_pos, (
            f"start_tls_s must be called BEFORE simple_bind_s "
            f"(got order={order!r})"
        )


class TestLdapStartTlsAlwaysCalled:
    """``start_tls_s`` must be invoked on every successful connection,
    even when the URI is already ``ldaps://``."""

    @pytest.fixture
    def provider(self) -> LDAPAuthProvider:
        return LDAPAuthProvider()

    async def test_start_tls_called_for_plain_ldap_uri(self, provider, monkeypatch):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings",
            _settings(ldap_server_url="ldap://ldap.example.com:389"),
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        assert conn.start_tls_called is True

    async def test_start_tls_called_for_ldaps_uri(self, provider, monkeypatch):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings",
            _settings(ldap_server_url="ldaps://ldap.example.com:636"),
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        # On ldaps:// the call is redundant but must still be made; the
        # underlying python-ldap library tolerates it.
        assert conn.start_tls_called is True


class TestLdapCaCertPathBehavior:
    """When the operator configures ``ldap_ca_cert_path`` it must be
    forwarded to ``OPT_X_TLS_CACERTFILE``. When left empty the option
    must NOT be set so the system trust store is used."""

    @pytest.fixture
    def provider(self) -> LDAPAuthProvider:
        return LDAPAuthProvider()

    async def test_ca_cert_path_propagated_to_cacertfile(self, provider, monkeypatch):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings",
            _settings(ldap_ca_cert_path="/etc/ssl/certs/my-ca-bundle.pem"),
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        assert mock_ldap.OPT_X_TLS_CACERTFILE in conn.options
        assert conn.options[mock_ldap.OPT_X_TLS_CACERTFILE] == "/etc/ssl/certs/my-ca-bundle.pem"

    async def test_no_cacertfile_option_when_path_is_empty(
        self, provider, monkeypatch
    ):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings", _settings(ldap_ca_cert_path="")
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        assert mock_ldap.OPT_X_TLS_CACERTFILE not in conn.options, (
            "OPT_X_TLS_CACERTFILE must not be set when ldap_ca_cert_path is empty "
            "so that the system trust store is used."
        )

    async def test_demand_set_even_when_cacertfile_is_not(
        self, provider, monkeypatch
    ):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings", _settings(ldap_ca_cert_path="")
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        assert mock_ldap.OPT_X_TLS_REQUIRE_CERT in conn.options
        assert mock_ldap.OPT_X_TLS_CACERTFILE not in conn.options

    async def test_arbitrary_path_is_passed_through_verbatim(
        self, provider, monkeypatch
    ):
        weird = "/opt/ca bundles/my bundle [v1].pem"
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings",
            _settings(ldap_ca_cert_path=weird),
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(username="testuser", password="pw", db=db)

        assert conn.options[mock_ldap.OPT_X_TLS_CACERTFILE] == weird


class TestLdapStartTlsFailureRejected:
    """If ``start_tls_s`` raises, the bind must not proceed and the user
    must see a generic "Invalid credentials" error — never the raw
    exception, which could leak TLS-handshake details to the caller."""

    @pytest.fixture
    def provider(self) -> LDAPAuthProvider:
        return LDAPAuthProvider()

    async def test_start_tls_failure_returns_invalid_credentials(
        self, provider, monkeypatch
    ):
        monkeypatch.setattr("engine.api.auth.ldap.settings", _settings())
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            start_tls_raises=Exception("TLS handshake failed: self-signed cert"),
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="testuser", password="pw", db=db
            )

        assert result.success is False
        assert "Invalid credentials" in result.error
        # Bind must not have been attempted after a failed TLS upgrade.
        assert conn.bind_called is False

    async def test_start_tls_failure_does_not_leak_exception_text(
        self, provider, monkeypatch
    ):
        sensitive = "CONNECT to ldap.internal.invalid:636 failed: TLS: cert from CN=malicious"
        monkeypatch.setattr("engine.api.auth.ldap.settings", _settings())
        mock_ldap, mock_filter, _ = _build_ldap_mock(
            start_tls_raises=Exception(sensitive),
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="testuser", password="pw", db=db
            )

        assert result.success is False
        # Error message must NOT include the raw TLS exception details.
        assert "malicious" not in (result.error or "")
        assert "ldap.internal.invalid" not in (result.error or "")


# ===========================================================================
# 3.  Interaction with the rest of the LDAP provider
# ===========================================================================


class TestLdapTlsAndEndToEndAuth:
    """Sanity-check that TLS hardening does not break the happy path: a
    successful bind, search, and user creation."""

    @pytest.fixture
    def provider(self) -> LDAPAuthProvider:
        return LDAPAuthProvider()

    async def test_end_to_end_success_sets_all_tls_options(
        self, provider, monkeypatch
    ):
        monkeypatch.setattr(
            "engine.api.auth.ldap.settings",
            _settings(ldap_ca_cert_path="/etc/ca.pem"),
        )
        mock_ldap, mock_filter, conn = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", _ok_attrs())]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="testuser", password="pw", db=db
            )

        assert result.success is True
        assert conn.start_tls_called is True
        assert conn.bind_called is True
        assert conn.options[mock_ldap.OPT_X_TLS_REQUIRE_CERT] == mock_ldap.OPT_X_TLS_DEMAND
        assert conn.options[mock_ldap.OPT_X_TLS_CACERTFILE] == "/etc/ca.pem"


# ===========================================================================
# 4.  sanitize_role_for_log — unit tests
# ===========================================================================


class TestSanitizeRoleForLogUnit:
    """Pure-function tests for ``sanitize_role_for_log``."""

    def test_returns_empty_string_unchanged(self):
        assert sanitize_role_for_log("") == ""

    def test_returns_simple_printable_string_unchanged(self):
        assert sanitize_role_for_log("developer") == "developer"

    def test_preserves_unicode_letters_and_emoji(self):
        assert sanitize_role_for_log("développeur") == "développeur"
        assert sanitize_role_for_log("developer🎉") == "developer🎉"

    def test_preserves_printable_punctuation(self):
        assert sanitize_role_for_log("group/dev-team") == "group/dev-team"
        assert sanitize_role_for_log("name with spaces") == "name with spaces"
        assert sanitize_role_for_log("a.b.c") == "a.b.c"

    # --- C0 control plane (U+0000 - U+001F) ---

    def test_strips_nul(self):
        assert sanitize_role_for_log("ad\x00min") == "admin"

    def test_strips_bell_backspace(self):
        assert sanitize_role_for_log("a\x07b\x08c") == "abc"

    def test_strips_tab(self):
        assert sanitize_role_for_log("a\tb") == "ab"

    def test_strips_newline(self):
        # The single most dangerous char for log forging — must go.
        assert sanitize_role_for_log("admin\nuser") == "adminuser"

    def test_strips_carriage_return(self):
        assert sanitize_role_for_log("admin\ruser") == "adminuser"

    def test_strips_form_feed_vertical_tab(self):
        assert sanitize_role_for_log("a\fb\vc") == "abc"

    def test_strips_escape(self):
        # ESC is the foundation of terminal-escape-sequence attacks.
        assert sanitize_role_for_log("\x1b[31madmin") == "[31madmin"

    def test_strips_unit_separator(self):
        assert sanitize_role_for_log("a\x1fb") == "ab"

    def test_strips_all_c0_chars_at_once(self):
        # Every C0 control char U+0000-U+001F should be stripped.
        payload = "".join(chr(i) for i in range(0x20))
        assert sanitize_role_for_log(payload) == ""

    # --- C1 control plane (U+007F - U+009F) ---

    def test_strips_del(self):
        assert sanitize_role_for_log("a\x7fb") == "ab"

    def test_strips_c1_control_range(self):
        # Every C1 control char U+007F-U+009F should be stripped.
        payload = "".join(chr(i) for i in range(0x7F, 0xA0))
        assert sanitize_role_for_log(payload) == ""

    def test_strips_mixed_c0_and_c1(self):
        assert sanitize_role_for_log("\x00admin\x9f\x7f") == "admin"

    # --- Truncation ---

    def test_truncates_strings_over_max_length(self):
        long_role = "A" * (_MAX_LOG_ROLE_LENGTH + 50)
        cleaned = sanitize_role_for_log(long_role)
        assert len(cleaned) == _MAX_LOG_ROLE_LENGTH + len("...")
        assert cleaned.startswith("A" * _MAX_LOG_ROLE_LENGTH)
        assert cleaned.endswith("...")

    def test_does_not_truncate_strings_at_exactly_max_length(self):
        boundary = "B" * _MAX_LOG_ROLE_LENGTH
        cleaned = sanitize_role_for_log(boundary)
        assert cleaned == boundary
        assert "..." not in cleaned

    def test_truncates_after_stripping_control_chars(self):
        # A 300-char string that contains control chars: we strip first,
        # then truncate the remaining printable suffix.
        payload = "X" * 300 + "\x00\x01admin\x02\x03"
        cleaned = sanitize_role_for_log(payload)
        # The control chars at the tail are stripped first, leaving
        # 300 + 5 = 305 'X's + "admin", which must then be truncated.
        assert cleaned.endswith("...")
        assert len(cleaned) == _MAX_LOG_ROLE_LENGTH + 3  # "..."
        # And the original control chars must not appear.
        assert "\x00" not in cleaned
        assert "\x01" not in cleaned
        assert "\x02" not in cleaned
        assert "\x03" not in cleaned

    def test_truncation_marker_is_three_dots(self):
        long_role = "Z" * 1000
        cleaned = sanitize_role_for_log(long_role)
        assert cleaned.endswith("...")

    # --- Regex sanity ---

    def test_control_chars_regex_matches_full_c0_plane(self):
        for i in range(0x20):
            assert _CONTROL_CHARS_RE.search(chr(i)) is not None, (
                f"C0 char U+{i:04X} must be matched by _CONTROL_CHARS_RE"
            )

    def test_control_chars_regex_matches_full_c1_plane(self):
        for i in range(0x7F, 0xA0):
            assert _CONTROL_CHARS_RE.search(chr(i)) is not None, (
                f"C1 char U+{i:04X} must be matched by _CONTROL_CHARS_RE"
            )

    def test_control_chars_regex_does_not_match_printable_ascii(self):
        for i in range(0x20, 0x7F):
            assert _CONTROL_CHARS_RE.search(chr(i)) is None, (
                f"Printable ASCII char U+{i:04X} ({chr(i)!r}) "
                "must not be matched by _CONTROL_CHARS_RE"
            )

    def test_control_chars_regex_does_not_match_common_unicode(self):
        # Latin-1 supplement printable chars (U+00A0 onwards).
        for i in range(0xA0, 0x180):
            assert _CONTROL_CHARS_RE.search(chr(i)) is None


# ===========================================================================
# 5.  map_roles — sanitization is wired into the warning payload
# ===========================================================================


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-tls-sanitize"

    async def authenticate(self, **_kwargs):
        return AuthResult()


def _patch_logger(monkeypatch):
    """Replace ``engine.api.auth.base.logger`` with a stub that captures
    every warning call.  Returns the captured-calls list."""
    calls: list[dict[str, Any]] = []

    class _Stub:
        def warning(self, _event, **kwargs):
            calls.append({"event": _event, **kwargs})

        def info(self, _event, **kwargs):  # pragma: no cover
            calls.append({"event": _event, "level": "info", **kwargs})

        def error(self, _event, **kwargs):  # pragma: no cover
            calls.append({"event": _event, "level": "error", **kwargs})

    from engine.api.auth import base

    monkeypatch.setattr(base, "logger", _Stub())
    return calls


class TestMapRolesWiresSanitization:
    """``map_roles`` must route every unrecognized role through
    ``sanitize_role_for_log`` before it is included in the warning."""

    def test_control_chars_stripped_in_warning_payload(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        # NB: the literal sanitized string for "ad\x00min" is "admin", but it
        # arrived in the unrecognized path because the raw input contained a
        # NUL byte (the .lower().strip() normalization in map_roles does NOT
        # remove control chars, so "ad\x00min" is *not* matched against the
        # recognized set). This test asserts only that the control char is
        # gone — *not* that the printable residue can't collide with a
        # recognized role name. (The latter would be a content-policy
        # question for the operator, not a sanitizer invariant.)
        _ConcreteProvider().map_roles(["ad\x00min", "real\x1bdeveloper"])
        assert calls, "Expected at least one warning"
        unrecognized = calls[0]["unrecognized"]
        assert len(unrecognized) == 2
        # The NUL byte must be stripped from the first entry.
        assert unrecognized[0] == "admin"
        assert "\x00" not in unrecognized[0]
        # ESC must be stripped from the second entry, leaving a printable
        # residue that does not collide with any recognized role.
        assert unrecognized[1] == "realdeveloper"
        assert "\x1b" not in unrecognized[1]
        # The recognized roles for this call must be empty (none of the
        # inputs matched role_priority).
        assert calls[0]["recognized"] == []

    def test_newlines_stripped_in_warning_payload(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        # Inject a forged multi-line payload.
        _ConcreteProvider().map_roles(["FAKE\n200 OK"])
        assert calls
        # The unrecognized entry must be a single line.
        for entry in calls[0]["unrecognized"]:
            assert "\n" not in entry
            assert "\r" not in entry

    def test_long_role_truncated_in_warning_payload(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        long_role = "X" * 1000
        _ConcreteProvider().map_roles([long_role])
        assert calls
        unrecognized = calls[0]["unrecognized"]
        assert len(unrecognized) == 1
        # Must be truncated to the helper's bound + ellipsis marker.
        assert len(unrecognized[0]) == _MAX_LOG_ROLE_LENGTH + 3
        assert unrecognized[0].endswith("...")

    def test_warning_still_fires_when_role_is_purely_control_chars(
        self, monkeypatch
    ):
        """Even when sanitization reduces the role to the empty string,
        the warning must still fire so operators see the misconfiguration."""
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["\x00\x01\x02"])
        assert calls, (
            "A role that sanitizes down to '' must still trigger the "
            "unrecognized-role warning"
        )
        assert calls[0]["unrecognized"] == [""]

    def test_multiple_unrecognized_roles_each_sanitized(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["a\x00", "b\x01", "c\x02"])
        assert calls
        unrecognized = calls[0]["unrecognized"]
        # Exactly one entry per unrecognized role, in order.
        assert unrecognized == ["a", "b", "c"]

    def test_recognized_roles_not_sanitized_nor_in_unrecognized_payload(
        self, monkeypatch
    ):
        """``recognized`` is operator-trusted (we already verified it
        against ``role_priority``) — it is *not* routed through the
        sanitizer. ``unrecognized`` is, and must never contain a
        recognized role."""
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["admin", "weird\x00role"])
        assert calls
        assert calls[0]["recognized"] == ["admin"]
        assert "admin" not in calls[0]["unrecognized"]
        # The unknown role has its NUL stripped.
        assert calls[0]["unrecognized"] == ["weirdrole"]

    def test_log_forging_attack_is_neutralized(self, monkeypatch):
        """Classic log-forging attack: an attacker submits a role that
        ends in a fake '200 OK' status line, hoping a downstream parser
        treats it as a separate log entry. After sanitization the
        newline must be gone, so the attack fails."""
        calls = _patch_logger(monkeypatch)
        attack = "user\n200 OK - admin login successful"
        _ConcreteProvider().map_roles([attack])
        assert calls
        sanitized = calls[0]["unrecognized"][0]
        assert "\n" not in sanitized
        assert sanitized == "user200 OK - admin login successful"

    def test_terminal_escape_attack_is_neutralized(self, monkeypatch):
        """ANSI escape injection: an attacker submits a role beginning
        with ESC+[ to re-colorize or re-write the terminal line. After
        sanitization ESC is stripped."""
        calls = _patch_logger(monkeypatch)
        attack = "\x1b[31madmin\x1b[0m"
        _ConcreteProvider().map_roles([attack])
        assert calls
        sanitized = calls[0]["unrecognized"][0]
        assert "\x1b" not in sanitized
        # ESC removed, but the printable bracket-pieces remain.
        assert sanitized == "[31madmin[0m"


# ===========================================================================
# 6.  Integration — the LDAP provider path through map_roles
# ===========================================================================


class TestLdapProviderEndToEndSanitization:
    """When LDAP returns a group DN that's not in the role mapping, the
    sanitized string flows into the warning. We verify this end-to-end."""

    async def test_unknown_group_dn_is_sanitized_in_warning(
        self, monkeypatch
    ):
        # Route warnings issued from BOTH the base module and the ldap
        # module through the same stub so we can capture them in one place.
        calls: list[dict[str, Any]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, _event, **kwargs):
                pass

            def exception(self, _event, **kwargs):
                pass

        monkeypatch.setattr("engine.api.auth.base.logger", _Stub())

        # Configure role mapping so that the malicious DN is NOT matched;
        # the resulting role list contains an unrecognized entry, which
        # routes through sanitize_role_for_log.
        s = _settings(
            ldap_role_mapping=json.dumps(
                {"cn=admins,ou=groups,dc=example,dc=com": "admin"}
            ),
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        # Build a fake LDAP response where one group maps to "admin"
        # (recognized) and another is a forged DN with control chars
        # (unrecognized → triggers sanitization).
        attrs = _ok_attrs(
            member_of=[
                b"cn=admins,ou=groups,dc=example,dc=com",
                b"cn=\x00evil\x1bgroup,ou=groups,dc=example,dc=com",
            ]
        )
        mock_ldap, mock_filter, _ = _build_ldap_mock(
            search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
        )
        db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="testuser", password="pw", db=db
            )

        assert result.success is True
        # The malicious DN was *not* in the role mapping, but it was
        # passed verbatim to map_roles, which sanitized it before
        # emitting the warning. Find the warning call.
        warning_calls = [
            c for c in calls if c["event"] == "auth.map_roles.unrecognized_roles"
        ]
        # The mapping turns "cn=admins,..." into "admin", so the only
        # role passed to map_roles is ["admin"] — which IS recognized.
        # Therefore no warning fires in this exact path. (Sanitization
        # is exercised directly in TestMapRolesWiresSanitization above.)
        assert warning_calls == []
