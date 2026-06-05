"""Targeted tests for the three SEV-741 follow-up fixes.

These tests pin the three behaviors introduced in this change set:

1. ``map_roles`` default fallback changed from ``"user"`` to
   ``"viewer"`` (the least-privileged internal role), and an explicit
   ``auth.map_roles.fallback_to_viewer`` warning is emitted whenever
   this fallback fires. Closing a silent privilege-floor escalation.

2. New ``sanitize_role()`` helper strips ASCII control characters
   and truncates to 128 characters before logging unrecognized role
   strings. Defends log aggregators and SIEM pipelines from log
   injection / unbounded storage growth.

3. The actual consumer of ``auth_overwrite_role_on_login`` lives in
   the federated login paths (``engine.api.auth.ldap`` and
   ``engine.api.auth.oidc``). When the setting is ``False`` (default),
   an existing user's role must NOT be mutated by an upstream IdP
   claim. When ``True`` (opt-in), the role IS mutated and an audit
   log entry is emitted.

These tests are designed to be deterministic, isolated, and free of
network or DB dependencies.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    AuthResult,
    IAuthProvider,
    UserInfo,
    sanitize_role,
)
from engine.config import Settings

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _ConcreteProvider(IAuthProvider):
    """Minimal concrete provider used to exercise ``map_roles``."""

    @property
    def name(self) -> str:
        return "test-concrete"

    async def authenticate(self, **kwargs: Any) -> AuthResult:  # pragma: no cover
        return AuthResult()


def _patch_logger(monkeypatch):
    """Replace the module-level structlog logger in engine.api.auth.base
    with a stub that records every call. Returns the call list so tests
    can assert on event names and bound kwargs.
    """
    calls: list[dict[str, object]] = []

    class _Stub:
        def warning(self, _event, **kwargs):
            calls.append({"event": _event, "level": "warning", **kwargs})

        def info(self, _event, **kwargs):
            calls.append({"event": _event, "level": "info", **kwargs})

        def error(self, _event, **kwargs):  # pragma: no cover
            calls.append({"event": _event, "level": "error", **kwargs})

        def exception(self, _event, **kwargs):  # pragma: no cover
            calls.append({"event": _event, "level": "exception", **kwargs})

    from engine.api.auth import base

    monkeypatch.setattr(base, "logger", _Stub())
    return calls


# ===========================================================================
# 1. map_roles fallback is now 'viewer' (least-privileged)
# ===========================================================================


class TestMapRolesViewerFallback:
    """SEV-741 follow-up: ``map_roles`` must fall back to ``"viewer"``
    (the least-privileged internal role) when no recognized role is
    present in the upstream claim. Previously it fell back to
    ``"user"``, silently granting read-write-eligible access to a
    federated principal whose IdP failed to assert any recognized
    role.
    """

    def test_empty_input_returns_viewer(self):
        assert _ConcreteProvider().map_roles([]) == "viewer"

    def test_all_unrecognized_returns_viewer(self):
        assert _ConcreteProvider().map_roles(["superuser", "god", "root"]) == "viewer"

    def test_whitespace_only_returns_viewer(self):
        # Whitespace-only input is normalized to the empty string,
        # which is not a known role. Falls through to ``viewer``.
        assert _ConcreteProvider().map_roles(["   "]) == "viewer"

    def test_empty_strings_only_return_viewer(self):
        assert _ConcreteProvider().map_roles(["", "", ""]) == "viewer"

    def test_recognized_roles_still_take_precedence_over_fallback(self):
        """Mix of recognized + unrecognized: recognized wins, fallback
        does NOT fire."""
        p = _ConcreteProvider()
        assert p.map_roles(["developer", "bogus"]) == "developer"
        assert p.map_roles(["viewer", "bogus"]) == "viewer"

    def test_viewer_does_not_silently_become_user(self):
        """Regression guard for the specific privilege escalation we
        are closing. A bogus claim must NEVER promote a user to
        ``user``-level access; the floor is ``viewer``."""
        for external in ([], ["bogus"], ["bogus", "", "   "]):
            assert _ConcreteProvider().map_roles(external) == "viewer"

    def test_admin_is_still_admin_when_present(self):
        """Sanity check: when a recognized role IS present, the
        fallback never fires — the recognized role wins as before."""
        assert _ConcreteProvider().map_roles(["admin", "completely_unknown"]) == "admin"


class TestFallbackWarningEmitted:
    """When the ``viewer`` fallback fires, a dedicated
    ``auth.map_roles.fallback_to_viewer`` warning must be emitted so
    operators can alert specifically on this privilege-floor event.

    Implementation note: we monkeypatch the module-level structlog
    logger to keep the tests deterministic and free of structlog
    config coupling.
    """

    def test_fallback_warning_fires_for_empty_input(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        assert _ConcreteProvider().map_roles([]) == "viewer"
        fallback_events = [c for c in calls if c["event"] == "auth.map_roles.fallback_to_viewer"]
        assert len(fallback_events) == 1
        assert fallback_events[0]["provider"] == "test-concrete"

    def test_fallback_warning_fires_for_all_unrecognized(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        assert _ConcreteProvider().map_roles(["bogus_a", "bogus_b"]) == "viewer"
        # Exactly one fallback event AND one unrecognized-roles event.
        fallback_events = [c for c in calls if c["event"] == "auth.map_roles.fallback_to_viewer"]
        unrecognized_events = [
            c for c in calls if c["event"] == "auth.map_roles.unrecognized_roles"
        ]
        assert len(fallback_events) == 1
        assert len(unrecognized_events) == 1

    def test_fallback_warning_does_not_fire_when_recognized_present(self, monkeypatch):
        """When a recognized role is present (even alongside unrecognized
        ones), the fallback must NOT fire — recognized wins."""
        calls = _patch_logger(monkeypatch)
        assert _ConcreteProvider().map_roles(["user", "bogus"]) == "user"
        assert not any(c["event"] == "auth.map_roles.fallback_to_viewer" for c in calls)

    def test_fallback_warning_payload_includes_external_role_count(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["bogus_a", "bogus_b", "bogus_c"])
        fallback = next(c for c in calls if c["event"] == "auth.map_roles.fallback_to_viewer")
        # Operators rely on the count to detect drift vs. the IdP.
        assert fallback["external_role_count"] == 3

    def test_fallback_warning_payload_includes_external_roles(self, monkeypatch):
        """The bound ``external_roles=`` payload must include the
        sanitized, unrecognized role strings (not the recognized
        ones)."""
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["bogus_a", "bogus_b"])
        fallback = next(c for c in calls if c["event"] == "auth.map_roles.fallback_to_viewer")
        roles = fallback["external_roles"]
        assert "bogus_a" in roles
        assert "bogus_b" in roles

    def test_fallback_warning_payload_includes_provider_name(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles([])
        fallback = next(c for c in calls if c["event"] == "auth.map_roles.fallback_to_viewer")
        assert fallback["provider"] == "test-concrete"

    def test_fallback_warning_fires_once_per_call(self, monkeypatch):
        """A single map_roles call must produce exactly one fallback
        event (operators rely on this for alert deduplication)."""
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles([])
        assert sum(1 for c in calls if c["event"] == "auth.map_roles.fallback_to_viewer") == 1

    def test_unrecognized_event_mapped_payload_is_viewer_on_fallback(self, monkeypatch):
        """The ``auth.map_roles.unrecognized_roles`` event's
        ``mapped=`` payload must report the actual mapped role, which
        is ``viewer`` when the fallback fires."""
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["bogus"])
        unrecognized = next(c for c in calls if c["event"] == "auth.map_roles.unrecognized_roles")
        assert unrecognized["mapped"] == "viewer"

    def test_no_warnings_at_all_when_all_roles_recognized(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles(["user", "admin"])
        assert calls == []


# ===========================================================================
# 2. sanitize_role() helper
# ===========================================================================


class TestSanitizeRole:
    """``sanitize_role`` is a pure-function helper that prepares raw
    external role strings for safe inclusion in log records.

    Behavior pinned by these tests:

    * Strips ASCII control characters (0x00-0x1F and 0x7F).
    * Truncates to a maximum of 128 characters.
    * Strips leading/trailing whitespace.
    * Returns an empty string for empty / non-string input.
    * Does NOT modify the rest of the string content.
    * Is idempotent (sanitizing an already-sanitized string is a
      no-op).
    """

    def test_passthrough_for_normal_string(self):
        assert sanitize_role("user") == "user"

    def test_empty_string_returns_empty(self):
        assert sanitize_role("") == ""

    def test_whitespace_only_returns_empty(self):
        assert sanitize_role("   ") == ""

    def test_strips_leading_and_trailing_whitespace(self):
        assert sanitize_role("  user  ") == "user"

    def test_strips_null_byte(self):
        # Classic log-injection / terminal-control attack.
        assert sanitize_role("user\x00admin") == "useradmin"

    def test_strips_newline(self):
        # Multi-line log injection.
        assert sanitize_role("user\nadmin") == "useradmin"

    def test_strips_carriage_return(self):
        assert sanitize_role("user\radmin") == "useradmin"

    def test_strips_tab(self):
        assert sanitize_role("user\tadmin") == "useradmin"

    def test_strips_bell_and_other_control_chars(self):
        # \x07 is BEL, \x1b is ESC (ANSI escape).
        assert sanitize_role("\x07admin\x1b") == "admin"

    def test_strips_delete_char(self):
        # \x7f is DEL.
        assert sanitize_role("admin\x7f") == "admin"

    def test_strips_multiple_distinct_control_chars(self):
        assert sanitize_role("\x00\x01\x02admin\n\x1b\x7f") == "admin"

    def test_truncates_long_string_to_128(self):
        long_role = "a" * 200
        assert sanitize_role(long_role) == "a" * 128

    def test_truncation_happens_after_strip(self):
        # Leading whitespace stripped first, then truncated.
        long_role = "   " + "b" * 200
        result = sanitize_role(long_role)
        assert len(result) == 128
        assert result == "b" * 128

    def test_string_of_exactly_128_chars_is_not_truncated(self):
        exact = "c" * 128
        assert sanitize_role(exact) == exact
        assert len(sanitize_role(exact)) == 128

    def test_string_of_129_chars_is_truncated_to_128(self):
        just_over = "d" * 129
        assert sanitize_role(just_over) == "d" * 128

    def test_idempotent(self):
        role = "user\x00admin" + "e" * 200
        once = sanitize_role(role)
        twice = sanitize_role(once)
        assert once == twice

    def test_non_string_input_returns_empty(self):
        assert sanitize_role(None) == ""  # type: ignore[arg-type]
        assert sanitize_role(123) == ""  # type: ignore[arg-type]
        assert sanitize_role(["a", "b"]) == ""  # type: ignore[arg-type]
        assert sanitize_role({"key": "val"}) == ""  # type: ignore[arg-type]

    def test_preserves_internal_whitespace(self):
        assert sanitize_role("my role") == "my role"

    def test_preserves_special_chars_that_are_not_control(self):
        # These are valid in role names and must NOT be stripped.
        assert sanitize_role("role-with-dash") == "role-with-dash"
        assert sanitize_role("role_with_underscore") == "role_with_underscore"
        assert sanitize_role("role.with.dot") == "role.with.dot"

    def test_preserves_unicode_letters(self):
        # Non-ASCII letters (Letter category) are preserved.
        assert sanitize_role("rôle") == "rôle"
        assert sanitize_role(" rôle ") == "rôle"

    def test_strips_unicode_control_chars(self):
        """``sanitize_role`` is documented to strip ASCII control
        characters specifically; this test pins that contract for
        known-ASCII control chars."""
        # U+0000 to U+001F are ASCII control; U+007F is DEL.
        # We verify each boundary value.
        for byte in range(0x20):
            assert sanitize_role(f"pre{chr(byte)}post") == "prepost"
        assert sanitize_role(f"pre{chr(0x7F)}post") == "prepost"


class TestSanitizeRoleUsedInUnrecognizedWarning:
    """``map_roles`` must run each unrecognized role through
    ``sanitize_role`` before including it in the
    ``auth.map_roles.unrecognized_roles`` payload."""

    def test_control_chars_stripped_from_unrecognized_payload(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        # Inject a role with a null byte and a newline — classic
        # log-injection payload.
        _ConcreteProvider().map_roles(["user\x00admin", "real\nfake"])
        unrecognized_event = next(
            c for c in calls if c["event"] == "auth.map_roles.unrecognized_roles"
        )
        unrecognized = unrecognized_event["unrecognized"]
        # Control chars must have been stripped before logging.
        assert all("\x00" not in r for r in unrecognized)
        assert all("\n" not in r for r in unrecognized)
        # The sanitized values are still distinguishable.
        assert "useradmin" in unrecognized
        assert "realfake" in unrecognized

    def test_long_unrecognized_role_truncated_in_payload(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        long_role = "x" * 500
        _ConcreteProvider().map_roles([long_role])
        unrecognized_event = next(
            c for c in calls if c["event"] == "auth.map_roles.unrecognized_roles"
        )
        unrecognized = unrecognized_event["unrecognized"]
        assert len(unrecognized[0]) == 128


# ===========================================================================
# 3. auth_overwrite_role_on_login consumer (LDAP + OIDC)
# ===========================================================================


def _ldap_search_results(uid="testuser", member_of=None):
    """Build a minimal LDAP search result tuple suitable for the
    fake LDAP connection used in unit tests."""
    attrs: dict[str, list[bytes]] = {
        "uid": [uid.encode()],
        "mail": [f"{uid}@example.com".encode()],
        "cn": [uid.encode()],
    }
    if member_of is not None:
        attrs["memberOf"] = member_of
    return [(f"uid={uid},ou=users,dc=example,dc=com", attrs)]


class _FakeLDAPConn:
    """Drop-in fake for ldap.ldapobject.LDAPObject."""

    def __init__(self, search_results):
        self._search_results = search_results

    def set_option(self, _opt, _value):
        pass

    def simple_bind_s(self, _dn, _password):
        pass

    def search_s(self, _base, _scope, _filter, _attrlist):
        return self._search_results

    def unbind_s(self):
        pass


def _patch_ldap(monkeypatch, search_results):
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(return_value=_FakeLDAPConn(search_results))
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    monkeypatch.setattr(
        "sys.modules",
        {**(__import__("sys").modules), "ldap": mock_ldap, "ldap.filter": mock_filter},
    )


def _ldap_settings(monkeypatch, **kwargs):
    """Build a Settings instance with sensible LDAP defaults plus any
    explicit overrides the test wants."""
    s = Settings(
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping='{"cn=admins,ou=groups,dc=example,dc=com": "admin"}',
        **kwargs,
    )
    monkeypatch.setattr("engine.api.auth.ldap.settings", s)
    return s


class TestLDAPAuthOverwriteRoleOnLogin:
    """SEV-741 follow-up: LDAP federated login must NOT mutate an
    existing user's role unless the operator has explicitly opted in
    via ``auth_overwrite_role_on_login=True``.

    These tests pin the consumer of the setting and its audit-log
    behavior.
    """

    async def test_default_setting_blocks_role_overwrite(self, monkeypatch):
        """Default (False): existing user's role is preserved even
        when IdP asserts a different role."""
        from engine.db.models import User

        _ldap_settings(monkeypatch)  # default auth_overwrite_role_on_login=False

        _patch_ldap(
            monkeypatch,
            _ldap_search_results(
                uid="promoted",
                member_of=[b"cn=admins,ou=groups,dc=example,dc=com"],
            ),
        )

        from engine.api.auth.ldap import LDAPAuthProvider

        existing = User(
            email="promoted@example.com",
            display_name="Promoted",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="promoted",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        result = await LDAPAuthProvider().authenticate(
            username="promoted", password="pass", db=mock_db
        )

        assert result.success is True
        # Role unchanged — overwrite was not opted in.
        assert existing.role == "user"
        mock_db.flush.assert_not_called()

    async def test_opt_in_allows_role_overwrite(self, monkeypatch):
        """When ``auth_overwrite_role_on_login=True`` the role IS
        mutated to reflect the IdP-asserted role."""
        from engine.db.models import User

        _ldap_settings(monkeypatch, auth_overwrite_role_on_login=True)

        _patch_ldap(
            monkeypatch,
            _ldap_search_results(
                uid="promoted",
                member_of=[b"cn=admins,ou=groups,dc=example,dc=com"],
            ),
        )

        from engine.api.auth.ldap import LDAPAuthProvider

        existing = User(
            email="promoted@example.com",
            display_name="Promoted",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="promoted",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        result = await LDAPAuthProvider().authenticate(
            username="promoted", password="pass", db=mock_db
        )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_called_once()

    async def test_no_overwrite_when_role_unchanged(self, monkeypatch):
        """When IdP-asserted role == current role, the equality
        short-circuit prevents both the mutation and the warning —
        no spurious audit-log noise on every login."""
        from engine.db.models import User

        _ldap_settings(monkeypatch, auth_overwrite_role_on_login=True)

        _patch_ldap(
            monkeypatch,
            _ldap_search_results(
                uid="stable",
                member_of=[b"cn=admins,ou=groups,dc=example,dc=com"],
            ),
        )

        from engine.api.auth.ldap import LDAPAuthProvider

        existing = User(
            email="stable@example.com",
            display_name="Stable",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="stable",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        result = await LDAPAuthProvider().authenticate(
            username="stable", password="pass", db=mock_db
        )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_skipped_overwrite_emits_warning(self, monkeypatch):
        """When overwrite is skipped (default), an audit-log warning
        must be emitted so operators can detect drift between the
        local role and the IdP assertion."""
        from engine.db.models import User

        _ldap_settings(monkeypatch)  # default = False

        _patch_ldap(
            monkeypatch,
            _ldap_search_results(
                uid="drift",
                member_of=[b"cn=admins,ou=groups,dc=example,dc=com"],
            ),
        )

        captured: list[tuple[str, dict[str, Any]]] = []

        class _StubLogger:
            def warning(self, event, **kwargs):
                captured.append((event, kwargs))

            def info(self, _event, **_kwargs):  # pragma: no cover
                pass

            def exception(self, _event, **_kwargs):  # pragma: no cover
                pass

        monkeypatch.setattr("engine.api.auth.ldap.logger", _StubLogger())

        from engine.api.auth.ldap import LDAPAuthProvider

        existing = User(
            email="drift@example.com",
            display_name="Drift",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="drift",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        await LDAPAuthProvider().authenticate(username="drift", password="pass", db=mock_db)

        skipped = [c for c in captured if c[0] == "auth.ldap.role_overwrite_skipped"]
        assert len(skipped) == 1
        payload = skipped[0][1]
        assert payload["current_role"] == "user"
        assert payload["idp_asserted_role"] == "admin"

    async def test_applied_overwrite_emits_info(self, monkeypatch):
        """When overwrite IS applied (opt-in), an info-level audit
        log entry must be emitted recording both old and new roles."""
        from engine.db.models import User

        _ldap_settings(monkeypatch, auth_overwrite_role_on_login=True)

        _patch_ldap(
            monkeypatch,
            _ldap_search_results(
                uid="promoted",
                member_of=[b"cn=admins,ou=groups,dc=example,dc=com"],
            ),
        )

        captured: list[tuple[str, dict[str, Any]]] = []

        class _StubLogger:
            def info(self, event, **kwargs):
                captured.append((event, kwargs))

            def warning(self, _event, **_kwargs):  # pragma: no cover
                pass

            def exception(self, _event, **_kwargs):  # pragma: no cover
                pass

        monkeypatch.setattr("engine.api.auth.ldap.logger", _StubLogger())

        from engine.api.auth.ldap import LDAPAuthProvider

        existing = User(
            email="promoted@example.com",
            display_name="Promoted",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="promoted",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        await LDAPAuthProvider().authenticate(username="promoted", password="pass", db=mock_db)

        applied = [c for c in captured if c[0] == "auth.ldap.role_overwritten"]
        assert len(applied) == 1
        payload = applied[0][1]
        assert payload["previous_role"] == "user"
        assert payload["new_role"] == "admin"

    async def test_downgrade_blocked_by_default(self, monkeypatch):
        """A misconfigured IdP claiming a LOWER role than the user's
        current role must NOT downgrade the user when overwrite is
        disabled (the default). This is the core SEV-741 defense."""
        from engine.db.models import User

        _ldap_settings(monkeypatch)  # default = False

        # IdP asserts no recognized group → mapped_role = viewer.
        _patch_ldap(
            monkeypatch,
            _ldap_search_results(uid="admin_user", member_of=[]),
        )

        from engine.api.auth.ldap import LDAPAuthProvider

        existing = User(
            email="admin@example.com",
            display_name="Admin",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="admin_user",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        result = await LDAPAuthProvider().authenticate(
            username="admin_user", password="pass", db=mock_db
        )

        assert result.success is True
        # Role NOT downgraded.
        assert existing.role == "admin"


class TestOIDCAuthOverwriteRoleOnLogin:
    """SEV-741 follow-up: OIDC federated login must follow the same
    gating rule as LDAP — no role overwrite on existing users unless
    ``auth_overwrite_role_on_login=True``."""

    @pytest.fixture
    def oidc_settings(self, monkeypatch):
        s = Settings(
            oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
            oidc_client_id="cid",
            oidc_client_secret="csec",
            oidc_redirect_uri="https://app.example.com/callback",
            oidc_role_claim="roles",
        )
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)
        return s

    @pytest.fixture
    def rsa_keys(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return private_key, private_key.public_key()

    def _build_fake_client(self, rsa_keys, claims):
        from tests.test_oidc_auth import (
            DISCOVERY_DOC,
            _FakeAsyncClient,
            _FakeHttpxResponse,
            _make_jwk_kid,
            _sign_id_token,
        )

        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)
        all_claims = {"aud": "cid", **claims}
        id_token = _sign_id_token(all_claims, private_key, kid)
        return _FakeAsyncClient(
            get_responses=[
                _FakeHttpxResponse(json_data=DISCOVERY_DOC),
                _FakeHttpxResponse(json_data={"keys": [jwk_dict]}),
            ],
            post_responses=[
                _FakeHttpxResponse(json_data={"id_token": id_token, "access_token": "at"})
            ],
        )

    async def test_default_blocks_overwrite_on_existing_oidc_user(self, oidc_settings, rsa_keys):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        fake_client = self._build_fake_client(
            rsa_keys,
            {
                "sub": "existing-oidc",
                "email": "existing@example.com",
                "name": "Existing",
                "roles": ["admin"],
            },
        )
        existing = User(
            email="existing@example.com",
            display_name="Existing",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="existing-oidc",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        # Default = False → role NOT overwritten.
        assert existing.role == "user"
        mock_db.flush.assert_not_called()

    async def test_opt_in_overwrites_existing_oidc_user_role(self, monkeypatch, rsa_keys):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        s = Settings(
            oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
            oidc_client_id="cid",
            oidc_client_secret="csec",
            oidc_redirect_uri="https://app.example.com/callback",
            oidc_role_claim="roles",
            auth_overwrite_role_on_login=True,
        )
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)

        fake_client = self._build_fake_client(
            rsa_keys,
            {
                "sub": "existing-oidc",
                "email": "existing@example.com",
                "name": "Existing",
                "roles": ["admin"],
            },
        )
        existing = User(
            email="existing@example.com",
            display_name="Existing",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="existing-oidc",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_called_once()

    async def test_skipped_overwrite_emits_warning(self, oidc_settings, rsa_keys):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        fake_client = self._build_fake_client(
            rsa_keys,
            {
                "sub": "drift-oidc",
                "email": "drift@example.com",
                "name": "Drift",
                "roles": ["admin"],
            },
        )
        existing = User(
            email="drift@example.com",
            display_name="Drift",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="drift-oidc",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        captured: list[tuple[str, dict[str, Any]]] = []

        class _StubLogger:
            def warning(self, event, **kwargs):
                captured.append((event, kwargs))

            def info(self, *a, **k):  # pragma: no cover
                pass

            def exception(self, *a, **k):  # pragma: no cover
                pass

        import engine.api.auth.oidc as oidc_mod

        original_logger = oidc_mod.logger
        oidc_mod.logger = _StubLogger()
        try:
            with patch("httpx.AsyncClient", return_value=fake_client):
                await OIDCAuthProvider().authenticate(code="auth-code", db=mock_db)
        finally:
            oidc_mod.logger = original_logger

        skipped = [c for c in captured if c[0] == "auth.oidc.role_overwrite_skipped"]
        assert len(skipped) == 1
        assert skipped[0][1]["current_role"] == "user"
        assert skipped[0][1]["idp_asserted_role"] == "admin"


# ===========================================================================
# Cross-cutting: sanitize_role is importable and exported
# ===========================================================================


class TestSanitizeRoleImportable:
    def test_importable_from_base(self):
        from engine.api.auth.base import sanitize_role

        assert callable(sanitize_role)

    def test_works_with_userinfo_role_field(self):
        """Demonstrate that ``sanitize_role`` is suitable for use on
        values from ``UserInfo.roles``."""
        info = UserInfo(roles=["bogus\x00role", "a" * 500])
        sanitized = [sanitize_role(r) for r in info.roles]
        assert sanitized == ["bogusrole", "a" * 128]
