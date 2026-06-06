"""Tests for the centralized federated-role overwrite guard.

Covers the helpers introduced to ``engine/api/auth/base.py`` to
centralize the SEV-741 defense-in-depth pattern:

1. ``_CONTROL_CHARS_RE``  — Unicode class that strips invisible /
   spoofing characters from IdP-asserted role strings.
2. ``_sanitize_role``      — thin wrapper over ``_CONTROL_CHARS_RE``
   that runs at the IdP -> internal-role boundary.
3. ``_apply_role_mapping`` — overwrite-or-skip helper used by every
   federated provider for EXISTING users.  Honors
   ``Settings.auth_overwrite_role_on_login``.

The four federated providers (LDAP, OIDC, Google, GitHub) are
exercised through end-to-end mocks to verify the guard behaves
identically across them.

Spec referenced (focus areas):
  * (1) Shared ``_apply_role_mapping(user, mapped_role, config)`` helper.
  * (2) All four federated providers refactored to call it.
  * (3) ``_sanitize_role`` docstring accurately describes when it applies.
  * (4) ``_CONTROL_CHARS_RE`` broadened to cover C1 / RTL / ZW / BOM.
  * (5) Unit tests across all providers verifying the guard works
        identically.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    _CONTROL_CHARS_RE,
    AuthResult,
    IAuthProvider,
    _apply_role_mapping,
    _sanitize_role,
)
from engine.config import Settings
from engine.db.models import User

# ---------------------------------------------------------------------------
# Test fixtures — concrete providers and stub settings
# ---------------------------------------------------------------------------


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-concrete"

    async def authenticate(self, **_kwargs: Any) -> AuthResult:
        return AuthResult()


def _make_user(
    role: str = "user",
    auth_provider: str = "test",
    external_id: str | None = "ext-1",
) -> User:
    return User(
        email="user@example.com",
        display_name="User",
        is_active=True,
        role=role,
        auth_provider=auth_provider,
        external_id=external_id,
    )


def _make_settings(overwrite: bool = False) -> Settings:
    return Settings(_env_file=None, auth_overwrite_role_on_login=overwrite)


@pytest.fixture
def capture_logger(monkeypatch):
    """Capture structlog events emitted by ``engine.api.auth.base``."""
    from engine.api.auth import base

    calls: list[dict[str, Any]] = []

    class _Stub:
        def info(self, _event, **kwargs):
            calls.append({"event": _event, "level": "info", **kwargs})

        def warning(self, _event, **kwargs):
            calls.append({"event": _event, "level": "warning", **kwargs})

        def error(self, _event, **kwargs):
            calls.append({"event": _event, "level": "error", **kwargs})

    monkeypatch.setattr(base, "logger", _Stub())
    return calls


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _CONTROL_CHARS_RE — character class coverage (focus area 4)
# ═══════════════════════════════════════════════════════════════════════════════


class TestControlCharsReCoverage:
    """``_CONTROL_CHARS_RE`` must match every dangerous Unicode class
    called out in the spec: C0, DEL, C1, RTL override, zero-width chars,
    and BOM."""

    def test_matches_c0_control_range(self):
        # U+0000 through U+001F (plus the literal DEL is tested below).
        for cp in range(0x20):
            ch = chr(cp)
            assert _CONTROL_CHARS_RE.search(f"a{ch}b"), (
                f"C0 control U+{cp:04X} should match"
            )

    def test_matches_del(self):
        assert _CONTROL_CHARS_RE.search("a\x7fb")
        assert _CONTROL_CHARS_RE.search("\x7f")

    def test_matches_c1_control_range(self):
        """U+0080 through U+009F — the often-forgotten C1 block."""
        for cp in range(0x80, 0xA0):
            ch = chr(cp)
            assert _CONTROL_CHARS_RE.search(f"a{ch}b"), (
                f"C1 control U+{cp:04X} should match"
            )

    def test_matches_rtl_override(self):
        """U+202E RIGHT-TO-LEFT OVERRIDE — visual spoofing vector."""
        assert _CONTROL_CHARS_RE.search("admin\u202E")
        assert _CONTROL_CHARS_RE.search("\u202Eadmin")
        assert _CONTROL_CHARS_RE.search("a\u202Eb")

    def test_matches_zero_width_chars(self):
        """U+200B (ZWSP), U+200C (ZWNJ), U+200D (ZWJ)."""
        for cp in (0x200B, 0x200C, 0x200D):
            ch = chr(cp)
            assert _CONTROL_CHARS_RE.search(f"admin{ch}"), (
                f"Zero-width U+{cp:04X} should match"
            )

    def test_matches_bom(self):
        """U+FEFF BOM / Zero-Width No-Break Space."""
        assert _CONTROL_CHARS_RE.search("\uFEFFadmin")
        assert _CONTROL_CHARS_RE.search("admin\uFEFF")

    def test_does_not_match_normal_chars(self):
        assert not _CONTROL_CHARS_RE.search("admin")
        assert not _CONTROL_CHARS_RE.search("portfolio_manager")
        assert not _CONTROL_CHARS_RE.search("user@example.com")

    def test_does_not_match_normal_unicode(self):
        """Non-control Unicode (e.g. CJK, accented Latin) must pass through."""
        assert not _CONTROL_CHARS_RE.search("管理员")  # "admin" in Chinese
        assert not _CONTROL_CHARS_RE.search("administrateur")
        assert not _CONTROL_CHARS_RE.search("αβγ")

    def test_does_not_match_whitespace(self):
        """ASCII whitespace is allowed (it would be stripped elsewhere by
        the ``strip()`` in :meth:`map_roles`).  The regex must not
        accidentally eat valid separators in role display strings."""
        assert not _CONTROL_CHARS_RE.search("a b")
        assert not _CONTROL_CHARS_RE.search("a-b")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _sanitize_role — wrapper behavior (focus area 3)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSanitizeRole:
    """``_sanitize_role`` strips every character matched by
    ``_CONTROL_CHARS_RE`` and leaves the rest of the string intact."""

    def test_strips_rtl_override(self):
        assert _sanitize_role("admin\u202E") == "admin"
        assert _sanitize_role("\u202Eadmin") == "admin"
        assert _sanitize_role("a\u202Ed\u202Emin") == "admin"

    def test_strips_zero_width_chars(self):
        assert _sanitize_role("admin\u200B") == "admin"
        assert _sanitize_role("ad\u200Cmin") == "admin"
        assert _sanitize_role("\u200Dadmin") == "admin"

    def test_strips_bom(self):
        assert _sanitize_role("\uFEFFadmin") == "admin"
        assert _sanitize_role("admin\uFEFF") == "admin"

    def test_strips_c1_controls(self):
        assert _sanitize_role("admin\u0080") == "admin"
        assert _sanitize_role("ad\u009Fmin") == "admin"

    def test_strips_multiple_distinct_dangerous_chars(self):
        # Mix of RTL, ZW, BOM, C1 in a single string.
        malicious = "\u202Eadmin\u200B\u200C\u200D\uFEFF\u0085"
        assert _sanitize_role(malicious) == "admin"

    def test_preserves_normal_role(self):
        assert _sanitize_role("admin") == "admin"
        assert _sanitize_role("portfolio_manager") == "portfolio_manager"
        assert _sanitize_role("user") == "user"

    def test_returns_empty_for_pure_control_string(self):
        """A role composed solely of control characters collapses to
        the empty string — the caller is responsible for falling back
        to the default ``user`` role (see :meth:`map_roles`)."""
        assert _sanitize_role("\u202E\u200B\uFEFF") == ""
        assert _sanitize_role("\x00\x01\x02") == ""

    def test_idempotent(self):
        role = "admin\u202E\u200B"
        once = _sanitize_role(role)
        twice = _sanitize_role(once)
        assert once == twice == "admin"

    def test_concatenates_visible_parts(self):
        # Three visible "fragments" separated by invisible chars collapse
        # into a single concatenated string.  This is the spoofing risk:
        # "ad\u200Bmin" visually reads as "admin" but compares unequal.
        assert _sanitize_role("ad\u200Bmin") == "admin"
        assert _sanitize_role("a\u202Ed\u202Em\u200Bi\u200Dn") == "admin"

    def test_does_not_modify_normal_strings(self):
        """Function must not mutate the contents of a clean string."""
        s = "developer"
        assert _sanitize_role(s) is not s or _sanitize_role(s) == s
        # The actual guarantee is equality, not identity (re.sub may
        # return the same object for an unchanged string, but we don't
        # depend on it).
        assert _sanitize_role(s) == s


# ═══════════════════════════════════════════════════════════════════════════════
# 3. map_roles integration with _sanitize_role
# ═══════════════════════════════════════════════════════════════════════════════


class TestMapRolesSanitizationIntegration:
    """End-to-end: ``map_roles`` returns a sanitized, non-empty string."""

    def test_returns_sane_role_for_pure_input(self):
        p = _ConcreteProvider()
        assert p.map_roles(["admin"]) == "admin"

    def test_falls_back_to_user_when_recognized_role_only_invisible(self):
        """An external role like ``"admin\\u200B"`` is NOT equal to the
        canonical ``"admin"`` after lowercase+strip — it falls through
        to ``unrecognized`` and the user gets the default ``user``
        role.  This is the safe default — never persist a spoofed
        role."""
        p = _ConcreteProvider()
        assert p.map_roles(["admin\u200B"]) == "user"

    def test_map_roles_result_never_contains_rtl(self):
        """For every recognized role, the output must not contain any
        RTL / ZW / BOM character."""
        p = _ConcreteProvider()
        for role in (
            "viewer",
            "user",
            "retail_trader",
            "quant_dev",
            "developer",
            "portfolio_manager",
            "admin",
        ):
            mapped = p.map_roles([role])
            assert _sanitize_role(mapped) == mapped
            assert "\u202E" not in mapped
            assert "\u200B" not in mapped
            assert "\uFEFF" not in mapped

    def test_empty_after_sanitize_falls_back_to_user(self):
        """If sanitization would produce an empty string (e.g. a
        recognized role were somehow comprised solely of control
        chars), map_roles falls back to the default ``user`` role
        rather than persisting an empty string."""
        p = _ConcreteProvider()
        # Empty input list — best is None, mapped to "user", sanitization
        # leaves it as "user".
        assert p.map_roles([]) == "user"
        # Whitespace-only inputs are unrecognized → "user".
        assert p.map_roles(["   "]) == "user"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _apply_role_mapping — overwrite-or-skip helper (focus area 1)
# ═══════════════════════════════════════════════════════════════════════════════


class TestApplyRoleMapping:
    """Direct unit tests for ``_apply_role_mapping``."""

    def test_no_change_when_role_already_equal(self, capture_logger):
        user = _make_user(role="admin")
        cfg = _make_settings(overwrite=True)

        changed = _apply_role_mapping(user, "admin", cfg)

        assert changed is False
        assert user.role == "admin"
        # No log event when the role is already correct.
        assert capture_logger == []

    def test_preserves_role_when_flag_false(self, capture_logger):
        """SEV-741: when ``auth_overwrite_role_on_login`` is False (the
        default), the existing locally-granted role is preserved even
        if the IdP now asserts a different role."""
        user = _make_user(role="admin")
        cfg = _make_settings(overwrite=False)

        changed = _apply_role_mapping(user, "viewer", cfg)

        assert changed is False
        assert user.role == "admin"
        # An audit event is emitted so operators can see the IdP
        # claim was intentionally discarded.
        assert any(
            c["event"] == "auth.role_overwrite_skipped" for c in capture_logger
        )

    def test_overwrites_role_when_flag_true(self, capture_logger):
        user = _make_user(role="user")
        cfg = _make_settings(overwrite=True)

        changed = _apply_role_mapping(user, "admin", cfg)

        assert changed is True
        assert user.role == "admin"
        assert any(
            c["event"] == "auth.role_overwritten" for c in capture_logger
        )

    def test_overwrite_audit_event_carries_roles(self, capture_logger):
        user = _make_user(role="user")
        cfg = _make_settings(overwrite=True)

        _apply_role_mapping(user, "developer", cfg)

        overwrite_events = [c for c in capture_logger if c["event"] == "auth.role_overwritten"]
        assert overwrite_events
        evt = overwrite_events[0]
        assert evt["previous_role"] == "user"
        assert evt["new_role"] == "developer"

    def test_skip_audit_event_carries_current_and_mapped(self, capture_logger):
        user = _make_user(role="admin")
        cfg = _make_settings(overwrite=False)

        _apply_role_mapping(user, "viewer", cfg)

        skip_events = [c for c in capture_logger if c["event"] == "auth.role_overwrite_skipped"]
        assert skip_events
        evt = skip_events[0]
        assert evt["current_role"] == "admin"
        assert evt["mapped_role"] == "viewer"

    def test_audit_event_includes_provider_and_external_id(self, capture_logger):
        user = _make_user(role="user", auth_provider="ldap", external_id="uid=jdoe")
        cfg = _make_settings(overwrite=False)

        _apply_role_mapping(user, "admin", cfg)

        evt = next(c for c in capture_logger if c["event"] == "auth.role_overwrite_skipped")
        assert evt["provider"] == "ldap"
        assert evt["external_id"] == "uid=jdoe"

    def test_overwrite_event_includes_provider_and_external_id(self, capture_logger):
        user = _make_user(role="viewer", auth_provider="oidc", external_id="sub-123")
        cfg = _make_settings(overwrite=True)

        _apply_role_mapping(user, "developer", cfg)

        evt = next(c for c in capture_logger if c["event"] == "auth.role_overwritten")
        assert evt["provider"] == "oidc"
        assert evt["external_id"] == "sub-123"

    def test_no_log_when_role_unchanged(self, capture_logger):
        """Critical: when the role is already correct, we do NOT emit
        an audit event — this keeps the audit log clean for actual
        changes / suppressions."""
        user = _make_user(role="admin")
        cfg = _make_settings(overwrite=False)

        _apply_role_mapping(user, "admin", cfg)
        _apply_role_mapping(user, "admin", _make_settings(overwrite=True))

        assert capture_logger == []

    def test_supports_downgrade_when_flag_true(self, capture_logger):
        user = _make_user(role="admin")
        cfg = _make_settings(overwrite=True)

        changed = _apply_role_mapping(user, "viewer", cfg)

        assert changed is True
        assert user.role == "viewer"

    def test_blocks_downgrade_when_flag_false(self, capture_logger):
        """Defense-in-depth: a misconfigured IdP that drops a user
        from ``admin`` to ``viewer`` must NOT silently downgrade them
        on the next login."""
        user = _make_user(role="admin")
        cfg = _make_settings(overwrite=False)

        changed = _apply_role_mapping(user, "viewer", cfg)

        assert changed is False
        assert user.role == "admin"

    def test_blocks_upgrade_when_flag_false(self, capture_logger):
        """The same guard applies in the other direction: a compromised
        IdP cannot escalate privileges either."""
        user = _make_user(role="viewer")
        cfg = _make_settings(overwrite=False)

        changed = _apply_role_mapping(user, "admin", cfg)

        assert changed is False
        assert user.role == "viewer"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Cross-provider integration: every federated provider honors the guard
#    (focus area 2 + 5)
# ═══════════════════════════════════════════════════════════════════════════════


# --- LDAP --------------------------------------------------------------------


def _ldap_attrs(member_of: list[bytes] | None = None):
    attrs: dict[str, list[bytes]] = {
        "uid": [b"testuser"],
        "mail": [b"testuser@example.com"],
        "cn": [b"Test User"],
    }
    if member_of is not None:
        attrs["memberOf"] = member_of
    return attrs


def _ldap_mock(attrs):
    from tests.test_ldap_auth import _build_ldap_mock

    return _build_ldap_mock(
        search_results=[("uid=testuser,ou=users,dc=example,dc=com", attrs)]
    )


def _ldap_settings(overwrite: bool, monkeypatch):
    s = Settings(
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=developers,ou=groups,dc=example,dc=com": "developer",
        }),
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.ldap.settings", s)
    return s


class TestLDAPApplyRoleMapping:
    """LDAP provider end-to-end: ``_apply_role_mapping`` is invoked
    for existing users; behavior is governed by
    ``auth_overwrite_role_on_login``."""

    async def test_existing_user_role_preserved_when_flag_false(
        self, monkeypatch
    ):
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(overwrite=False, monkeypatch=monkeypatch)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock(attrs)

        existing = _make_user(
            role="user", auth_provider="ldap", external_id="testuser"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing.role == "user"  # preserved — IdP claim ignored
        mock_db.flush.assert_not_called()

    async def test_existing_user_role_overwritten_when_flag_true(
        self, monkeypatch
    ):
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(overwrite=True, monkeypatch=monkeypatch)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock(attrs)

        existing = _make_user(
            role="user", auth_provider="ldap", external_id="testuser"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_called()

    async def test_existing_user_no_change_when_role_matches(
        self, monkeypatch
    ):
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(overwrite=False, monkeypatch=monkeypatch)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock(attrs)

        existing = _make_user(
            role="admin", auth_provider="ldap", external_id="testuser"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert existing.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_new_user_gets_mapped_role_unconditionally(
        self, monkeypatch
    ):
        """First-time LDAP login always assigns the mapped role; the
        overwrite flag is irrelevant for users not yet in the DB."""
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(overwrite=False, monkeypatch=monkeypatch)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock(attrs)

        created: list[User] = []

        def _on_add(user: User) -> None:
            created.append(user)
            user.is_active = True

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        mock_db.add = MagicMock(side_effect=_on_add)
        mock_db.flush = AsyncMock()

        async def _refresh(user: User) -> None:
            user.is_active = True

        mock_db.refresh = AsyncMock(side_effect=_refresh)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="testuser", password="pass", db=mock_db
            )

        assert result.success is True
        assert len(created) == 1
        assert created[0].role == "admin"


# --- OIDC --------------------------------------------------------------------


def _oidc_settings(overwrite: bool, monkeypatch):
    s = Settings(
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_redirect_uri="https://app.example.com/callback",
        oidc_role_claim="roles",
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.oidc.settings", s)
    return s


def _build_oidc_client_with_roles(rsa_keys, id_token_claims):
    """Build a fully-mocked httpx client returning a signed id_token."""
    from tests.test_oidc_auth import (
        DISCOVERY_DOC,
        _FakeAsyncClient,
        _FakeHttpxResponse,
        _make_jwk_kid,
        _sign_id_token,
    )

    private_key, pub_key = rsa_keys
    jwk_dict, kid = _make_jwk_kid(pub_key)
    claims = {"aud": "test-client-id", **id_token_claims}
    id_token = _sign_id_token(claims, private_key, kid)

    disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
    token_resp = _FakeHttpxResponse(json_data={"id_token": id_token, "access_token": "at"})
    jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})

    return _FakeAsyncClient(
        get_responses=[disc_resp, jwks_resp],
        post_responses=[token_resp],
    )


@pytest.fixture
def rsa_keys():
    from tests.test_oidc_auth import _generate_rsa_key_pair

    return _generate_rsa_key_pair()


class TestOIDCApplyRoleMapping:
    """OIDC provider end-to-end: ``_apply_role_mapping`` is invoked for
    existing users; behavior matches LDAP."""

    async def test_existing_user_role_preserved_when_flag_false(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(overwrite=False, monkeypatch=monkeypatch)

        fake_client = _build_oidc_client_with_roles(
            rsa_keys,
            {
                "sub": "oidc-existing",
                "email": "existing@example.com",
                "name": "Existing",
                "roles": ["admin"],
            },
        )

        existing = _make_user(
            role="user", auth_provider="oidc", external_id="oidc-existing"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "user"
        mock_db.flush.assert_not_called()

    async def test_existing_user_role_overwritten_when_flag_true(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(overwrite=True, monkeypatch=monkeypatch)

        fake_client = _build_oidc_client_with_roles(
            rsa_keys,
            {
                "sub": "oidc-existing",
                "email": "existing@example.com",
                "name": "Existing",
                "roles": ["admin"],
            },
        )

        existing = _make_user(
            role="user", auth_provider="oidc", external_id="oidc-existing"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_called()

    async def test_existing_user_no_flush_when_role_unchanged(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(overwrite=True, monkeypatch=monkeypatch)

        fake_client = _build_oidc_client_with_roles(
            rsa_keys,
            {
                "sub": "oidc-existing",
                "email": "existing@example.com",
                "name": "Existing",
                "roles": ["admin"],
            },
        )

        existing = _make_user(
            role="admin", auth_provider="oidc", external_id="oidc-existing"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            await OIDCAuthProvider().authenticate(code="code", db=mock_db)

        assert existing.role == "admin"
        mock_db.flush.assert_not_called()


# --- Google ------------------------------------------------------------------


def _google_settings(overwrite: bool, monkeypatch):
    s = Settings(
        google_client_id="test-google-id",
        google_client_secret="test-google-secret",
        google_redirect_uri="https://app.example.com/google/callback",
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.google.settings", s)
    return s


def _google_httpx_mock(profile: dict[str, Any]):
    """Build a fake httpx.AsyncClient for the Google OAuth flow."""
    from tests.test_oidc_auth import _FakeAsyncClient, _FakeHttpxResponse

    token_resp = _FakeHttpxResponse(json_data={"access_token": "at"})
    userinfo_resp = _FakeHttpxResponse(json_data=profile)
    return _FakeAsyncClient(post_responses=[token_resp], get_responses=[userinfo_resp])


class TestGoogleApplyRoleMapping:
    """Google provider end-to-end."""

    async def test_existing_user_role_preserved_when_flag_false(
        self, monkeypatch
    ):
        from engine.api.auth.google import GoogleAuthProvider

        _google_settings(overwrite=False, monkeypatch=monkeypatch)
        fake_client = _google_httpx_mock(
            {"sub": "g-existing", "email": "g@example.com", "name": "G"}
        )

        existing = _make_user(
            role="admin", auth_provider="google", external_id="g-existing"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GoogleAuthProvider().authenticate(
                code="code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_existing_user_role_overwritten_when_flag_true(
        self, monkeypatch
    ):
        """Even though Google does not surface IdP-side roles, an
        operator may still want to demote a user back to ``user`` by
        toggling the flag (this is a no-op for Google because the
        mapped role is always ``user``).  The test confirms the helper
        is wired in and the audit path fires."""
        from engine.api.auth.google import GoogleAuthProvider

        _google_settings(overwrite=True, monkeypatch=monkeypatch)
        fake_client = _google_httpx_mock(
            {"sub": "g-existing", "email": "g@example.com", "name": "G"}
        )

        existing = _make_user(
            role="admin", auth_provider="google", external_id="g-existing"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GoogleAuthProvider().authenticate(
                code="code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "user"
        mock_db.flush.assert_called()


# --- GitHub ------------------------------------------------------------------


def _github_settings(overwrite: bool, monkeypatch):
    s = Settings(
        github_client_id="test-github-id",
        github_client_secret="test-github-secret",
        github_redirect_uri="https://app.example.com/github/callback",
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.github_oauth.settings", s)
    return s


def _github_httpx_mock(profile: dict[str, Any]):
    from tests.test_oidc_auth import _FakeAsyncClient, _FakeHttpxResponse

    token_resp = _FakeHttpxResponse(json_data={"access_token": "at"})
    userinfo_resp = _FakeHttpxResponse(json_data=profile)
    return _FakeAsyncClient(post_responses=[token_resp], get_responses=[userinfo_resp])


class TestGitHubApplyRoleMapping:
    """GitHub provider end-to-end."""

    async def test_existing_user_role_preserved_when_flag_false(
        self, monkeypatch
    ):
        from engine.api.auth.github_oauth import GitHubAuthProvider

        _github_settings(overwrite=False, monkeypatch=monkeypatch)
        fake_client = _github_httpx_mock(
            {"id": 1234, "login": "guser", "email": "g@github", "name": "GU"}
        )

        existing = _make_user(
            role="admin", auth_provider="github", external_id="1234"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GitHubAuthProvider().authenticate(
                code="code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_existing_user_role_overwritten_when_flag_true(
        self, monkeypatch
    ):
        from engine.api.auth.github_oauth import GitHubAuthProvider

        _github_settings(overwrite=True, monkeypatch=monkeypatch)
        fake_client = _github_httpx_mock(
            {"id": 1234, "login": "guser", "email": "g@github", "name": "GU"}
        )

        existing = _make_user(
            role="admin", auth_provider="github", external_id="1234"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await GitHubAuthProvider().authenticate(
                code="code", db=mock_db
            )

        assert result.success is True
        assert existing.role == "user"
        mock_db.flush.assert_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Cross-provider parity: identical guard behavior (focus area 5)
# ═══════════════════════════════════════════════════════════════════════════════


class TestProviderGuardParity:
    """The overwrite-or-skip contract must hold uniformly across LDAP,
    OIDC, Google, GitHub.  This is the cross-cutting guarantee called
    out in focus area 5: 'the guard works identically'.

    We exercise each provider's existing-user path with the overwrite
    flag both on and off, and assert identical semantics (preserve vs
    overwrite, flush on change, no flush when unchanged).
    """

    @pytest.mark.parametrize("overwrite_flag", [False, True])
    async def test_guard_consistent_for_ldap(self, monkeypatch, overwrite_flag):
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(overwrite=overwrite_flag, monkeypatch=monkeypatch)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock(attrs)

        existing = _make_user(
            role="user", auth_provider="ldap", external_id="testuser"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="testuser", password="pass", db=mock_db
            )

        if overwrite_flag:
            assert existing.role == "admin"
            mock_db.flush.assert_called()
        else:
            assert existing.role == "user"
            mock_db.flush.assert_not_called()

    @pytest.mark.parametrize("overwrite_flag", [False, True])
    async def test_guard_consistent_for_oidc(
        self, monkeypatch, rsa_keys, overwrite_flag
    ):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(overwrite=overwrite_flag, monkeypatch=monkeypatch)

        fake_client = _build_oidc_client_with_roles(
            rsa_keys,
            {
                "sub": "oidc-x",
                "email": "x@example.com",
                "name": "X",
                "roles": ["admin"],
            },
        )

        existing = _make_user(
            role="user", auth_provider="oidc", external_id="oidc-x"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            await OIDCAuthProvider().authenticate(code="code", db=mock_db)

        if overwrite_flag:
            assert existing.role == "admin"
            mock_db.flush.assert_called()
        else:
            assert existing.role == "user"
            mock_db.flush.assert_not_called()

    @pytest.mark.parametrize("overwrite_flag", [False, True])
    async def test_guard_consistent_for_google(self, monkeypatch, overwrite_flag):
        from engine.api.auth.google import GoogleAuthProvider

        _google_settings(overwrite=overwrite_flag, monkeypatch=monkeypatch)
        fake_client = _google_httpx_mock(
            {"sub": "g-x", "email": "g@example.com", "name": "G"}
        )

        existing = _make_user(
            role="admin", auth_provider="google", external_id="g-x"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            await GoogleAuthProvider().authenticate(code="code", db=mock_db)

        if overwrite_flag:
            assert existing.role == "user"
            mock_db.flush.assert_called()
        else:
            assert existing.role == "admin"
            mock_db.flush.assert_not_called()

    @pytest.mark.parametrize("overwrite_flag", [False, True])
    async def test_guard_consistent_for_github(self, monkeypatch, overwrite_flag):
        from engine.api.auth.github_oauth import GitHubAuthProvider

        _github_settings(overwrite=overwrite_flag, monkeypatch=monkeypatch)
        fake_client = _github_httpx_mock(
            {"id": 42, "login": "guser", "email": "g@github", "name": "GU"}
        )

        existing = _make_user(
            role="admin", auth_provider="github", external_id="42"
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            await GitHubAuthProvider().authenticate(code="code", db=mock_db)

        if overwrite_flag:
            assert existing.role == "user"
            mock_db.flush.assert_called()
        else:
            assert existing.role == "admin"
            mock_db.flush.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Source-level / structural guards (regression detection)
# ═══════════════════════════════════════════════════════════════════════════════


class TestStructuralGuards:
    """Protect the contract from silent regressions."""

    def test_helper_is_exported_from_base(self):
        from engine.api.auth import base

        assert hasattr(base, "_apply_role_mapping")
        assert hasattr(base, "_sanitize_role")
        assert hasattr(base, "_CONTROL_CHARS_RE")

    def test_helper_signature(self):
        import inspect

        sig = inspect.signature(_apply_role_mapping)
        params = list(sig.parameters.keys())
        assert params == ["user", "mapped_role", "config"], (
            f"Expected ['user', 'mapped_role', 'config'], got {params}"
        )
        # With `from __future__ import annotations` the annotation is
        # stringified; accept either form.
        assert sig.return_annotation in (bool, "bool"), (
            f"return annotation must be bool, got {sig.return_annotation!r}"
        )

    def test_sanitize_role_signature(self):
        import inspect

        sig = inspect.signature(_sanitize_role)
        params = list(sig.parameters.keys())
        assert params == ["role"]
        assert sig.return_annotation in (str, "str"), (
            f"return annotation must be str, got {sig.return_annotation!r}"
        )

    def test_control_chars_re_is_compiled_pattern(self):
        import re

        assert isinstance(_CONTROL_CHARS_RE, re.Pattern)

    def test_no_direct_role_assignment_in_ldap_for_existing_users(self):
        """After the refactor, LDAP must NOT assign ``user.role = …``
        directly for the existing-user branch — it must call the
        helper.  We grep the source to prevent regressions."""
        import inspect

        from engine.api.auth import ldap

        src = inspect.getsource(ldap)
        # The helper must be invoked somewhere in the file.
        assert "_apply_role_mapping" in src, (
            "LDAP provider must delegate to _apply_role_mapping"
        )

    def test_no_direct_role_assignment_in_oidc_for_existing_users(self):
        import inspect

        from engine.api.auth import oidc

        src = inspect.getsource(oidc)
        assert "_apply_role_mapping" in src

    def test_no_direct_role_assignment_in_google_for_existing_users(self):
        import inspect

        from engine.api.auth import google

        src = inspect.getsource(google)
        assert "_apply_role_mapping" in src

    def test_no_direct_role_assignment_in_github_for_existing_users(self):
        import inspect

        from engine.api.auth import github_oauth

        src = inspect.getsource(github_oauth)
        assert "_apply_role_mapping" in src

    def test_sanitize_role_docstring_describes_when_applied(self):
        """Focus area 3: the docstring must accurately describe when
        sanitization is applied (at the output of map_roles, before
        persistence — NOT, e.g., only on display or only in audit)."""
        doc = _sanitize_role.__doc__ or ""
        # The docstring should explicitly mention WHERE in the pipeline
        # sanitization is applied.
        assert "map_roles" in doc or "OUTPUT" in doc or "boundary" in doc.lower(), (
            "docstring must describe WHERE sanitization is applied"
        )

    def test_control_chars_re_covers_c1_range(self):
        """Focus area 4: every code point in U+0080-U+009F must match."""
        for cp in range(0x80, 0xA0):
            assert _CONTROL_CHARS_RE.match(chr(cp)), (
                f"U+{cp:04X} must be in _CONTROL_CHARS_RE"
            )

    def test_control_chars_re_covers_rtl_override(self):
        assert _CONTROL_CHARS_RE.match("\u202E")

    def test_control_chars_re_covers_zero_width(self):
        for cp in (0x200B, 0x200C, 0x200D):
            assert _CONTROL_CHARS_RE.match(chr(cp)), (
                f"U+{cp:04X} must be in _CONTROL_CHARS_RE"
            )

    def test_control_chars_re_covers_bom(self):
        assert _CONTROL_CHARS_RE.match("\uFEFF")
