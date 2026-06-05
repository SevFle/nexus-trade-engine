"""Security-focused tests for LDAP TLS enforcement and log sanitization.

Covers the SEV-508 follow-up:

1. ``engine.api.auth.ldap.LDAPAuthProvider`` must configure
   ``OPT_X_TLS_REQUIRE_CERT = OPT_X_TLS_DEMAND`` on every connection
   by default, and must honour an operator-supplied CA certificate
   path via ``settings.ldap_ca_cert_path``.

2. ``engine.api.auth.base._sanitize_for_log`` must strip C0 control
   characters, ANSI escapes and DEL from any string that flows into a
   structlog payload, and must truncate overly long strings to bound
   log line size.  ``map_roles`` must use this helper to defend
   against log injection via crafted upstream IdP role claims.

The LDAP code paths are exercised without a real LDAP server — we
patch ``sys.modules['ldap']`` and ``sys.modules['ldap.filter']`` so
the provider's TLS configuration calls can be observed directly.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    _LOG_MAX_LENGTH,
    AuthResult,
    IAuthProvider,
    _sanitize_for_log,
    _sanitize_role_list,
)
from engine.api.auth.ldap import LDAPAuthProvider
from engine.config import Settings

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeLDAPConn:
    """Recording fake of ``ldap.ldapobject.LDAPObject``.

    Every ``set_option`` call is captured in :attr:`options` so the
    TLS tests can assert which constants were forwarded.
    """

    def __init__(
        self,
        search_results: list[tuple[str, dict[str, list[bytes]]]] | None = None,
    ):
        self._search_results = search_results or []
        self.options: dict[int, Any] = {}
        self.bind_called = False
        self.unbind_called = False

    def set_option(self, opt: int, value: Any) -> None:
        self.options[opt] = value

    def simple_bind_s(self, dn: str, password: str) -> None:
        self.bind_called = True
        self.bind_dn = dn

    def search_s(self, base: str, scope: int, filterstr: str, attrlist: list[str]):
        return self._search_results

    def unbind_s(self) -> None:
        self.unbind_called = True


def _build_ldap_module(
    fake_conn: _FakeLDAPConn | None = None,
    *,
    include_tls: bool = True,
    include_cacert: bool = True,
    search_results: list[tuple[str, dict[str, list[bytes]]]] | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build mock ``ldap`` and ``ldap.filter`` modules.

    When *include_tls* is True the mock advertises
    ``OPT_X_TLS_REQUIRE_CERT`` / ``OPT_X_TLS_DEMAND`` constants; the
    TLS-enforcement tests toggle this to ensure the provider is robust
    against their absence on legacy python-ldap builds.
    """
    if fake_conn is None:
        fake_conn = _FakeLDAPConn(search_results=search_results)
    elif search_results is not None:
        fake_conn._search_results = search_results
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(return_value=fake_conn)
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    if include_tls:
        mock_ldap.OPT_X_TLS_REQUIRE_CERT = 0x6A
        mock_ldap.OPT_X_TLS_DEMAND = 0x03
    if include_cacert:
        mock_ldap.OPT_X_TLS_CACERTFILE = 0x6B
    mock_ldap._test_conn = fake_conn

    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter


def _make_settings(**overrides: Any) -> Settings:
    """Return a :class:`Settings` instance with sensible LDAP defaults."""
    base: dict[str, Any] = {
        "ldap_server_url": "ldaps://ldap.example.com:636",
        "ldap_bind_dn": "uid={{username}},ou=users,dc=example,dc=com",
        "ldap_search_base": "ou=users,dc=example,dc=com",
        "ldap_role_mapping": "{}",
        "ldap_tls_demand": True,
        "ldap_ca_cert_path": "",
    }
    base.update(overrides)
    return Settings(**base)


def _make_mock_db():
    """Build a mock DB session that tracks added users and simulates refresh.

    Mirrors the helper in ``tests/test_ldap_auth.py``: newly-added
    users get ``is_active=True`` so the LDAP provider's post-create
    "disabled user" guard doesn't short-circuit the success path.
    """
    mock_db = AsyncMock(spec=AsyncSession)

    def track_add(user):
        user.is_active = True

    async def mock_refresh(user):
        user.is_active = True

    mock_db.add = MagicMock(side_effect=track_add)
    mock_db.refresh = AsyncMock(side_effect=mock_refresh)
    mock_db.flush = AsyncMock()
    # No existing user → no email conflict path.
    no_user = MagicMock()
    no_user.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=no_user)
    return mock_db


def _attrs_for(uid: str = "tlstester") -> dict[str, list[bytes]]:
    return {
        "uid": [uid.encode()],
        "mail": [b"tls@example.com"],
        "cn": [b"TLS Tester"],
        "memberOf": [],
    }


def _default_search_result(uid: str = "tlstester"):
    """A single LDAP search result that satisfies the provider's
    "user must exist" check, used by TLS tests that aren't focused
    on the user-creation code path."""
    return [(f"uid={uid},ou=users,dc=example,dc=com", _attrs_for(uid))]


# ---------------------------------------------------------------------------
# Settings exposure
# ---------------------------------------------------------------------------


class TestLDAPTLSSettingsPresent:
    """``Settings`` must expose the new TLS knobs with secure defaults."""

    def test_ldap_tls_demand_default_is_true(self):
        s = Settings(_env_file=None)
        assert s.ldap_tls_demand is True, (
            "ldap_tls_demand must default to True — the secure posture "
            "must be opt-out, not opt-in (SEV-508)."
        )

    def test_ldap_ca_cert_path_default_is_empty(self):
        s = Settings(_env_file=None)
        assert s.ldap_ca_cert_path == ""

    def test_ldap_tls_demand_is_a_bool(self):
        s = Settings(_env_file=None)
        assert isinstance(s.ldap_tls_demand, bool)

    def test_ldap_tls_demand_can_be_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("NEXUS_LDAP_TLS_DEMAND", "false")
        s = Settings(_env_file=None)
        assert s.ldap_tls_demand is False

    def test_ldap_ca_cert_path_can_be_overridden_via_env(self, monkeypatch):
        monkeypatch.setenv("NEXUS_LDAP_CA_CERT_PATH", "/etc/ssl/ca.pem")
        s = Settings(_env_file=None)
        assert s.ldap_ca_cert_path == "/etc/ssl/ca.pem"


# ---------------------------------------------------------------------------
# TLS enforcement at bind time
# ---------------------------------------------------------------------------


class TestTLSDemandEnforced:
    """When ``ldap_tls_demand`` is True (default), the provider MUST
    set ``OPT_X_TLS_REQUIRE_CERT = OPT_X_TLS_DEMAND`` on every
    fresh connection before invoking ``simple_bind_s``."""

    async def test_demand_set_when_tls_demand_enabled(self, monkeypatch):
        s = _make_settings(ldap_tls_demand=True)
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap, mock_filter = _build_ldap_module(
            search_results=_default_search_result()
        )
        mock_db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="tlstester", password="correctpass", db=mock_db
            )

        assert result.success is True, f"Expected success, got error={result.error}"
        conn = mock_ldap._test_conn
        # The constant for OPT_X_TLS_REQUIRE_CERT must be present and
        # set to OPT_X_TLS_DEMAND.
        assert mock_ldap.OPT_X_TLS_REQUIRE_CERT in conn.options, (
            "Provider did not call set_option(OPT_X_TLS_REQUIRE_CERT, ...)"
        )
        assert conn.options[mock_ldap.OPT_X_TLS_REQUIRE_CERT] == mock_ldap.OPT_X_TLS_DEMAND

    async def test_demand_not_set_when_tls_demand_disabled(self, monkeypatch):
        """Operators must be able to opt out for legacy directories."""
        s = _make_settings(ldap_tls_demand=False)
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap, mock_filter = _build_ldap_module(
            search_results=_default_search_result()
        )
        mock_db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="tlstester", password="correctpass", db=mock_db
            )

        assert result.success is True
        conn = mock_ldap._test_conn
        assert mock_ldap.OPT_X_TLS_REQUIRE_CERT not in conn.options, (
            "TLS demand must NOT be set when operator opted out via "
            "settings.ldap_tls_demand=False"
        )

    async def test_tls_set_before_bind(self, monkeypatch):
        """Order matters: TLS settings must be applied BEFORE
        ``simple_bind_s`` is invoked, otherwise the first handshake
        could complete with the insecure default."""
        s = _make_settings(ldap_tls_demand=True)
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap, mock_filter = _build_ldap_module(
            search_results=_default_search_result()
        )
        mock_db = _make_mock_db()

        call_order: list[str] = []
        conn = mock_ldap._test_conn

        def record_set_option(opt, value):
            call_order.append(f"set:{opt}")
            conn.options[opt] = value

        def record_bind(dn, password):
            call_order.append("bind")
            conn.bind_called = True

        conn.set_option = MagicMock(side_effect=record_set_option)
        conn.simple_bind_s = MagicMock(side_effect=record_bind)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="order", password="pass", db=mock_db
            )

        tls_opt = mock_ldap.OPT_X_TLS_REQUIRE_CERT
        try:
            tls_idx = call_order.index(f"set:{tls_opt}")
            bind_idx = call_order.index("bind")
        except ValueError:
            pytest.fail(
                f"Expected both set_option(TLS) and bind in call order; "
                f"got {call_order}"
            )
        assert tls_idx < bind_idx, (
            f"TLS option must be set before bind; order was {call_order}"
        )

    async def test_robust_to_missing_tls_constants(self, monkeypatch):
        """If the installed python-ldap is too old to expose
        ``OPT_X_TLS_DEMAND``, the provider must NOT raise — the bind
        must still succeed (with whatever default policy the legacy
        library uses).  Operators who need hard enforcement are
        expected to upgrade python-ldap."""
        s = _make_settings(ldap_tls_demand=True)
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap, mock_filter = _build_ldap_module(
            include_tls=False,
            search_results=_default_search_result(),
        )
        mock_db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="legacy", password="pass", db=mock_db
            )

        assert result.success is True


# ---------------------------------------------------------------------------
# CA certificate path configuration
# ---------------------------------------------------------------------------


class TestCACertPathConfiguration:
    """``settings.ldap_ca_cert_path`` must be plumbed through to
    ``OPT_X_TLS_CACERTFILE`` on the underlying connection."""

    async def test_cacertfile_set_when_path_provided(self, monkeypatch):
        s = _make_settings(ldap_ca_cert_path="/etc/ssl/certs/company-ca.pem")
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap, mock_filter = _build_ldap_module(
            search_results=_default_search_result()
        )
        mock_db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="ca", password="pass", db=mock_db
            )

        assert result.success is True
        conn = mock_ldap._test_conn
        assert mock_ldap.OPT_X_TLS_CACERTFILE in conn.options, (
            "Provider did not call set_option(OPT_X_TLS_CACERTFILE, ...)"
        )
        assert conn.options[mock_ldap.OPT_X_TLS_CACERTFILE] == "/etc/ssl/certs/company-ca.pem"

    async def test_cacertfile_not_set_when_path_empty(self, monkeypatch):
        s = _make_settings(ldap_ca_cert_path="")
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap, mock_filter = _build_ldap_module(
            search_results=_default_search_result()
        )
        mock_db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="nocert", password="pass", db=mock_db
            )

        assert result.success is True
        conn = mock_ldap._test_conn
        assert mock_ldap.OPT_X_TLS_CACERTFILE not in conn.options

    async def test_cacertfile_applies_alongside_tls_demand(self, monkeypatch):
        """Both knobs must be configurable simultaneously."""
        s = _make_settings(
            ldap_tls_demand=True,
            ldap_ca_cert_path="/etc/ssl/certs/combined-ca.pem",
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        mock_ldap, mock_filter = _build_ldap_module(
            search_results=_default_search_result()
        )
        mock_db = _make_mock_db()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="combined", password="pass", db=mock_db
            )

        assert result.success is True
        conn = mock_ldap._test_conn
        assert conn.options[mock_ldap.OPT_X_TLS_REQUIRE_CERT] == mock_ldap.OPT_X_TLS_DEMAND
        assert conn.options[mock_ldap.OPT_X_TLS_CACERTFILE] == "/etc/ssl/certs/combined-ca.pem"


# ---------------------------------------------------------------------------
# Log sanitization helpers
# ---------------------------------------------------------------------------


class TestSanitizeForLog:
    """Direct unit tests for ``_sanitize_for_log``."""

    def test_strips_newline_control_chars(self):
        # Attack vector: inject fake log lines via newline + fake level.
        # Newlines are replaced with spaces to preserve word boundaries.
        assert _sanitize_for_log("user\nWARN fake log line") == "user WARN fake log line"

    def test_carriage_return_replaced_with_space(self):
        # \r is in the newline-like set; replaced, not stripped.
        assert _sanitize_for_log("evil\rrole") == "evil role"

    def test_tab_replaced_with_space(self):
        assert _sanitize_for_log("role\tname") == "role name"

    def test_strips_nul_byte(self):
        # NUL truncation attacks against log aggregators.
        assert _sanitize_for_log("admin\x00") == "admin"

    def test_strips_bell_and_other_c0(self):
        assert _sanitize_for_log("\x07alert\x08backspace") == "alertbackspace"

    def test_strips_ansi_escape(self):
        # ESC (0x1B) is in C0 but not whitespace — stripped outright.
        assert _sanitize_for_log("\x1b[31mRED\x1b[0m") == "[31mRED[0m"

    def test_strips_del(self):
        assert _sanitize_for_log("del\x7f") == "del"

    def test_collapses_whitespace_runs(self):
        assert _sanitize_for_log("too   much\t\t space\n\nhere") == "too much space here"

    def test_strips_leading_and_trailing_whitespace(self):
        assert _sanitize_for_log("   trimmed   ") == "trimmed"

    def test_empty_string_round_trips(self):
        assert _sanitize_for_log("") == ""

    def test_none_returns_empty(self):
        assert _sanitize_for_log(None) == ""

    def test_non_string_input_is_coerced(self):
        # Defensive — callers should not pass ints but the helper
        # tolerates it.
        assert _sanitize_for_log(42) == "42"

    def test_truncates_overly_long_input(self):
        long_input = "A" * (_LOG_MAX_LENGTH * 4)
        result = _sanitize_for_log(long_input)
        assert len(result) <= _LOG_MAX_LENGTH + 3  # +3 for the trailing "..."
        assert result.endswith("...")

    def test_preserves_normal_role_names_unchanged(self):
        assert _sanitize_for_log("developer") == "developer"
        assert _sanitize_for_log("portfolio_manager") == "portfolio_manager"

    def test_preserves_unicode_letters(self):
        # Only C0 + DEL are stripped; printable Unicode stays intact.
        assert _sanitize_for_log(" rôle ") == "rôle"

    def test_attack_pattern_fake_admin_warning(self):
        """Realistic attack: try to forge a fake 'admin role granted'
        line into the operator's log stream."""
        malicious = "user\nERROR auth.role.elevated admin_granted=True"
        sanitized = _sanitize_for_log(malicious)
        assert "\n" not in sanitized
        assert sanitized == "user ERROR auth.role.elevated admin_granted=True"

    def test_idempotent(self):
        # Calling sanitize twice must be a no-op on the second call.
        once = _sanitize_for_log("a\nb")
        twice = _sanitize_for_log(once)
        assert once == twice


class TestSanitizeRoleList:
    def test_returns_new_list_not_mutates_input(self):
        original = ["admin", "evil\nbogus"]
        sanitized = _sanitize_role_list(original)
        assert original == ["admin", "evil\nbogus"], "Original must not be mutated"
        assert sanitized == ["admin", "evil bogus"]

    def test_empty_list_returns_empty(self):
        assert _sanitize_role_list([]) == []

    def test_each_element_sanitized(self):
        roles = ["a\nx", "b\rc", " normal ", "d\x00"]
        assert _sanitize_role_list(roles) == ["a x", "b c", "normal", "d"]


# ---------------------------------------------------------------------------
# map_roles uses sanitized logging
# ---------------------------------------------------------------------------


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-ldap-sec"

    async def authenticate(self, **kwargs):  # pragma: no cover - not exercised
        return AuthResult()


class TestMapRolesSanitizedLogPayload:
    """The structlog payload produced by ``map_roles`` must not
    contain raw control characters — even when the upstream IdP
    supplied them."""

    def _patch_logger(self, monkeypatch):
        calls: list[dict[str, Any]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, *_a, **_kw):  # pragma: no cover
                pass

            def error(self, *_a, **_kw):  # pragma: no cover
                pass

        from engine.api.auth import base

        monkeypatch.setattr(base, "logger", _Stub())
        return calls

    def test_no_newlines_in_unrecognized_payload(self, monkeypatch):
        calls = self._patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["admin", "evil\nWARN fake"])
        assert calls, "Expected a warning to be emitted"
        payload = calls[0]
        for entry in payload["unrecognized"]:
            assert "\n" not in entry, (
                f"Newline must be stripped from unrecognized payload; got {entry!r}"
            )

    def test_no_control_chars_anywhere_in_payload(self, monkeypatch):
        calls = self._patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(
            ["admin", "evil\nbogus", "ANSI\x1b[31m", "NUL\x00inject", "tab\there"]
        )
        assert calls
        payload = calls[0]
        payload_str = json.dumps(payload, default=str)
        # No raw newline / tab / NUL inside the JSON serialization.
        for ch in ("\n", "\r", "\t", "\x00", "\x1b", "\x07"):
            assert ch not in payload_str, (
                f"Control char {ch!r} leaked into log payload: {payload_str!r}"
            )

    def test_recognized_roles_are_also_sanitized_in_payload(self, monkeypatch):
        """Even recognized roles pass through sanitization in case a
        future refactor moves them into the unrecognized bucket."""
        calls = self._patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["user\nadmin", "bogus"])
        # ``user\nadmin`` is NOT recognized (because of the newline)
        # so it should land in unrecognized, sanitized.
        assert calls
        unrecognized = calls[0]["unrecognized"]
        for r in unrecognized:
            assert "\n" not in r

    def test_mapped_value_in_payload_is_sanitized(self, monkeypatch):
        calls = self._patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["admin", "evil\nbogus"])
        assert calls
        mapped = calls[0]["mapped"]
        assert isinstance(mapped, str)
        for ch in ("\n", "\r", "\t", "\x00", "\x1b"):
            assert ch not in mapped

    def test_attack_log_injection_is_neutralized(self, monkeypatch):
        """End-to-end regression: a real-world log-injection attack
        must produce a payload that an operator can read without
        seeing fake log lines."""
        calls = self._patch_logger(monkeypatch)
        attack = (
            "user\n2025-01-01 00:00:00,000 ERROR engine.api.auth "
            "auth.role.elevated admin_granted=True source=attacker"
        )
        _ConcreteProvider().map_roles([attack])
        assert calls
        # Re-serialize — if any raw newline slipped through, json.dumps
        # would still escape it (\\n), but the actual string value must
        # contain real newline-free content.
        payload = calls[0]
        leaked = [
            r for r in payload["unrecognized"]
            if "\n" in r or "\r" in r
        ]
        assert not leaked, (
            f"Log-injection attack survived sanitization: leaked={leaked}"
        )


# ---------------------------------------------------------------------------
# Smoke test: existing map_roles behaviour is preserved
# ---------------------------------------------------------------------------


class TestMapRolesStillWorks:
    """Sanitization must not change which role is selected — only the
    payload that reaches the logger."""

    def test_admin_still_wins(self):
        assert _ConcreteProvider().map_roles(["user", "admin"]) == "admin"

    def test_empty_input_returns_user(self):
        assert _ConcreteProvider().map_roles([]) == "user"

    def test_all_unrecognized_falls_back_to_user(self):
        assert _ConcreteProvider().map_roles(["bogus_a", "bogus_b"]) == "user"
