"""Tests for expanded RBAC role hierarchy (SEV-233 / gh#86).

Validates the domain-specific roles quant_dev, retail_trader,
portfolio_manager alongside the pre-existing user/developer/admin
roles, and the require_auth convenience dependency.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import (
    ROLE_HIERARCHY,
    _resolve_token,
    require_auth,
    require_role,
)
from engine.db.models import User
from tests.conftest import FAKE_USER_ID


class TestExpandedRoleHierarchy:
    def test_viewer_is_lowest(self):
        assert ROLE_HIERARCHY["viewer"] == 0

    def test_user_above_viewer(self):
        assert ROLE_HIERARCHY["user"] > ROLE_HIERARCHY["viewer"]

    def test_retail_trader_above_user(self):
        assert ROLE_HIERARCHY["retail_trader"] > ROLE_HIERARCHY["user"]

    def test_quant_dev_above_retail_trader(self):
        assert ROLE_HIERARCHY["quant_dev"] > ROLE_HIERARCHY["retail_trader"]

    def test_developer_above_quant_dev(self):
        assert ROLE_HIERARCHY["developer"] > ROLE_HIERARCHY["quant_dev"]

    def test_portfolio_manager_above_developer(self):
        assert ROLE_HIERARCHY["portfolio_manager"] > ROLE_HIERARCHY["developer"]

    def test_admin_is_highest(self):
        assert ROLE_HIERARCHY["admin"] > ROLE_HIERARCHY["portfolio_manager"]

    def test_all_roles_present(self):
        expected = {"viewer", "user", "retail_trader", "quant_dev", "developer", "portfolio_manager", "admin"}
        assert set(ROLE_HIERARCHY.keys()) == expected

    def test_backward_compatible_user_developer_admin(self):
        assert ROLE_HIERARCHY["user"] < ROLE_HIERARCHY["developer"]
        assert ROLE_HIERARCHY["developer"] < ROLE_HIERARCHY["admin"]

    def test_total_role_count(self):
        assert len(ROLE_HIERARCHY) == 7


class TestRequireRoleExpanded:
    @pytest.mark.parametrize(
        ("role", "minimum", "allowed"),
        [
            ("viewer", "viewer", True),
            ("user", "viewer", True),
            ("retail_trader", "user", True),
            ("quant_dev", "retail_trader", True),
            ("developer", "quant_dev", True),
            ("portfolio_manager", "developer", True),
            ("admin", "portfolio_manager", True),
            ("admin", "admin", True),
            ("viewer", "user", False),
            ("user", "retail_trader", False),
            ("retail_trader", "quant_dev", False),
            ("quant_dev", "developer", False),
            ("developer", "portfolio_manager", False),
            ("portfolio_manager", "admin", False),
        ],
    )
    async def test_role_access_matrix(self, role, minimum, allowed):
        app = FastAPI()

        @app.get("/test")
        async def handler(user: User = Depends(require_role(minimum))):
            return {"role": user.role}

        fake_user = User(
            id=FAKE_USER_ID,
            email="test@example.com",
            display_name="Test",
            is_active=True,
            role=role,
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        from engine.api.auth.dependency import get_current_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/test")

        if allowed:
            assert resp.status_code == 200
        else:
            assert resp.status_code == 403


class TestRequireAuthDependency:
    async def test_require_auth_returns_user_on_valid_jwt(self):
        app = FastAPI()

        @app.get("/protected")
        async def handler(user: User = Depends(require_auth)):
            return {"id": str(user.id), "email": user.email}

        fake_user = User(
            id=FAKE_USER_ID,
            email="auth-test@example.com",
            display_name="Auth Test",
            is_active=True,
            role="user",
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[require_auth] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/protected")
            assert resp.status_code == 200
            assert resp.json()["email"] == "auth-test@example.com"


class TestResolveToken:
    def test_bearer_credentials(self):
        from fastapi.security import HTTPAuthorizationCredentials

        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {}

        req = _FakeRequest()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok123")
        assert _resolve_token(req, creds) == "tok123"

    def test_api_key_header(self):
        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {"x-api-key": "nxs_live_abc123"}

        req = _FakeRequest()
        assert _resolve_token(req, None) == "nxs_live_abc123"

    def test_no_token_returns_none(self):
        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {}

        req = _FakeRequest()
        assert _resolve_token(req, None) is None

    def test_bearer_takes_precedence_over_api_key(self):
        from fastapi.security import HTTPAuthorizationCredentials

        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {"x-api-key": "nxs_live_abc123"}

        req = _FakeRequest()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="jwt-token")
        assert _resolve_token(req, creds) == "jwt-token"

    def test_empty_credentials_returns_none(self):
        from fastapi.security import HTTPAuthorizationCredentials

        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {}

        req = _FakeRequest()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="")
        assert _resolve_token(req, creds) is None


class TestBaseProviderMapRoles:
    def _make_provider(self):
        from engine.api.auth.base import AuthResult, IAuthProvider

        class _Concrete(IAuthProvider):
            @property
            def name(self):
                return "test"

            async def authenticate(self, **kwargs):
                return AuthResult()

        return _Concrete()

    def test_map_roles_admin_wins(self):
        p = self._make_provider()
        assert p.map_roles(["user", "admin", "developer"]) == "admin"

    def test_map_roles_unknown_roles_fall_back_to_viewer(self):
        """SEV-741 follow-up: the least-privilege fallback for a
        completely unrecognized role set is ``viewer`` (read-only),
        not ``user`` (write). A misconfigured IdP must not be able to
        grant write access by asserting zero recognized groups."""
        p = self._make_provider()
        assert p.map_roles(["superuser", "god"]) == "viewer"

    def test_map_roles_new_domain_roles(self):
        p = self._make_provider()
        # SEV-741: map_roles no longer silently promotes domain roles.
        # ``quant_dev`` must remain ``quant_dev`` (not ``developer``) and
        # ``viewer`` must remain ``viewer`` (not ``user``). Only when an
        # external role is unrecognized do we fall back to ``viewer``.
        assert p.map_roles(["retail_trader", "quant_dev"]) == "quant_dev"
        assert p.map_roles(["portfolio_manager", "quant_dev"]) == "portfolio_manager"
        assert p.map_roles(["viewer"]) == "viewer"
        assert p.map_roles(["retail_trader"]) == "retail_trader"
        assert p.map_roles(["portfolio_manager"]) == "portfolio_manager"

    def test_map_roles_empty_list_returns_viewer(self):
        """SEV-741 follow-up: an empty external role claim must map to
        the lowest-privilege internal role (``viewer``), never ``user``.
        A federated login with no groups is the canonical least-privilege
        case."""
        p = self._make_provider()
        assert p.map_roles([]) == "viewer"


class TestAuthExports:
    def test_require_auth_importable(self):
        from engine.api.auth import require_auth

        assert callable(require_auth)

    def test_all_exports_present(self):
        import engine.api.auth as auth_mod

        for name in auth_mod.__all__:
            assert hasattr(auth_mod, name), f"Missing export: {name}"


# ===========================================================================
# SEV-741 follow-up: viewer fallback, role-preservation guard, log
# sanitization. These tests live alongside the historical RBAC role
# hierarchy tests so that all role-mapping invariants are co-located.
# ===========================================================================


def _concrete_provider():
    """Build a minimal concrete IAuthProvider for unit-level tests."""
    from engine.api.auth.base import AuthResult, IAuthProvider

    class _Concrete(IAuthProvider):
        @property
        def name(self) -> str:
            return "test-concrete"

        async def authenticate(self, **kwargs):  # pragma: no cover - unused here
            return AuthResult()

    return _Concrete()


class TestMapRolesViewerFallback:
    """Belt-and-braces coverage for the ``viewer`` least-privilege
    fallback added in this change. The fallback fires whenever
    ``map_roles`` cannot find *any* recognized role in the upstream
    claim."""

    def test_empty_external_roles_returns_viewer(self):
        assert _concrete_provider().map_roles([]) == "viewer"

    def test_all_unrecognized_returns_viewer(self):
        assert (
            _concrete_provider().map_roles(["superuser", "root", "god"])
            == "viewer"
        )

    def test_whitespace_only_role_is_unrecognized_and_falls_back_to_viewer(
        self,
    ):
        """A whitespace-only string normalizes to the empty string,
        which is not a known role. Should fall through to ``viewer``
        without crashing — and *not* grant ``user`` write access."""
        assert _concrete_provider().map_roles(["   "]) == "viewer"

    def test_mixed_recognized_and_unrecognized_uses_recognized(self):
        """When at least one recognized role is present it wins;
        the fallback is only used when nothing was recognized."""
        assert (
            _concrete_provider().map_roles(["developer", "l33t_h4x0r"])
            == "developer"
        )

    def test_viewer_is_strictly_below_user(self):
        """Sanity-check that the viewer fallback really is least
        privilege: ``viewer`` must rank strictly below ``user`` in
        :data:`ROLE_HIERARCHY` so downstream ``require_role`` checks
        block write operations."""
        assert ROLE_HIERARCHY["viewer"] < ROLE_HIERARCHY["user"]

    def test_mapped_field_in_warning_reports_viewer_on_empty_input(
        self, monkeypatch
    ):
        """When the input is empty no warning fires (nothing
        unrecognized), so this is mostly a guard that we don't
        accidentally start emitting warnings for the empty case."""
        from engine.api.auth import base

        calls: list[dict] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, *_a, **_kw):  # pragma: no cover
                pass

            def error(self, *_a, **_kw):  # pragma: no cover
                pass

        monkeypatch.setattr(base, "logger", _Stub())
        assert _concrete_provider().map_roles([]) == "viewer"
        assert calls == [], "Empty input must not trigger a warning"


# ---------------------------------------------------------------------------
# Role-preservation guard: ``settings.auth_overwrite_role_on_login``
# ---------------------------------------------------------------------------


class TestRolePreservationGuard:
    """Verify ``IAuthProvider.should_overwrite_existing_role`` and its
    one current call site in ``LDAPAuthProvider``.

    These pin the SEV-741 follow-up contract: by default a federated
    login MUST NOT mutate the stored role of an existing user —
    regardless of what the IdP asserts. Operators must opt in via
    ``NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN=true``.
    """

    def test_guard_returns_false_when_flag_disabled(self, monkeypatch):
        """Default behavior: flag is False, never overwrite."""
        from engine.config import Settings

        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is False

        monkeypatch.setattr("engine.api.auth.base.settings", s)
        p = _concrete_provider()
        # Even if mapped > existing, preservation wins.
        assert p.should_overwrite_existing_role("user", "admin") is False
        # And even when roles match (no-op), still False.
        assert p.should_overwrite_existing_role("admin", "admin") is False

    def test_guard_returns_false_when_roles_match_even_if_flag_enabled(
        self, monkeypatch
    ):
        """When the flag is True but the role is already correct,
        skip the DB write to avoid churn."""
        from engine.config import Settings

        s = Settings(_env_file=None, auth_overwrite_role_on_login=True)
        monkeypatch.setattr("engine.api.auth.base.settings", s)
        p = _concrete_provider()
        assert p.should_overwrite_existing_role("admin", "admin") is False

    def test_guard_returns_true_only_when_flag_enabled_and_roles_differ(
        self, monkeypatch
    ):
        from engine.config import Settings

        s = Settings(_env_file=None, auth_overwrite_role_on_login=True)
        monkeypatch.setattr("engine.api.auth.base.settings", s)
        p = _concrete_provider()
        assert p.should_overwrite_existing_role("user", "admin") is True
        # Downgrades must also be honored when opted-in.
        assert p.should_overwrite_existing_role("admin", "viewer") is True

    def test_guard_logs_preservation_when_skipping(self, monkeypatch):
        """When the guard skips an overwrite it must emit an info
        log so operators can audit that the IdP assertion was
        intentionally ignored."""
        from engine.api.auth import base
        from engine.config import Settings

        s = Settings(_env_file=None)
        monkeypatch.setattr("engine.api.auth.base.settings", s)

        captured: list[dict] = []

        class _Stub:
            def info(self, _event, **kwargs):
                captured.append({"event": _event, **kwargs})

            def warning(self, *_a, **_kw):  # pragma: no cover
                pass

            def error(self, *_a, **_kw):  # pragma: no cover
                pass

        monkeypatch.setattr(base, "logger", _Stub())
        p = _concrete_provider()
        assert p.should_overwrite_existing_role("admin", "user") is False
        assert any(
            c["event"] == "auth.federated.preserve_role" for c in captured
        ), "Expected an info log when the overwrite guard skips"
        # Payload must include enough context to audit the decision.
        preservation = next(
            c for c in captured if c["event"] == "auth.federated.preserve_role"
        )
        assert preservation["existing_role"] == "admin"
        assert preservation["mapped_role"] == "user"
        assert preservation["provider"] == "test-concrete"

    async def test_ldap_authenticate_preserves_existing_role_when_flag_off(
        self, monkeypatch
    ):
        """End-to-end: a returning LDAP user with role ``admin`` who
        logs in again with a more privileged IdP claim must keep
        their stored role unchanged when the operator has not opted
        in to overwrite-on-login.

        Previously :mod:`engine.api.auth.ldap` unconditionally wrote
        the freshly-mapped role on every login.
        """
        import json
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.config import Settings
        from engine.db.models import User

        s = Settings(
            _env_file=None,
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping=json.dumps(
                {"cn=admins,ou=groups,dc=example,dc=com": "admin"}
            ),
            # Flag OFF: preservation must win.
            auth_overwrite_role_on_login=False,
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)
        # The overwrite guard lives in base.py and reads its own
        # ``settings`` import, so patch that too.
        monkeypatch.setattr("engine.api.auth.base.settings", s)

        attrs = {
            "uid": [b"keepadmin"],
            "mail": [b"keepadmin@example.com"],
            "cn": [b"Keep Admin"],
            "memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"],
        }

        mock_ldap = MagicMock()
        mock_ldap.OPT_NETWORK_TIMEOUT = 7
        mock_ldap.OPT_TIMEOUT = 8
        mock_ldap.SCOPE_SUBTREE = 2

        class _Conn:
            def set_option(self, *_a, **_kw): ...
            def simple_bind_s(self, *_a, **_kw): ...
            def search_s(self, *_a, **_kw):
                return [("uid=keepadmin,ou=users,dc=example,dc=com", attrs)]
            def unbind_s(self): ...

        mock_ldap.initialize = MagicMock(return_value=_Conn())
        mock_filter = MagicMock()
        mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)

        existing_user = User(
            email="keepadmin@example.com",
            display_name="Keep Admin",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="keepadmin",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        provider = LDAPAuthProvider()
        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}
        ):
            result = await provider.authenticate(
                username="keepadmin", password="irrelevant", db=mock_db
            )

        assert result.success is True
        # The whole point of the guard: the stored role is preserved.
        assert existing_user.role == "admin"
        # And we did NOT call flush (no DB write).
        mock_db.flush.assert_not_called()

    async def test_ldap_authenticate_overwrites_when_flag_on(self, monkeypatch):
        """Symmetric coverage: when the operator opts in, a returning
        user with a different IdP-mapped role DOES get updated."""
        import json
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.config import Settings
        from engine.db.models import User

        s = Settings(
            _env_file=None,
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping=json.dumps(
                {"cn=admins,ou=groups,dc=example,dc=com": "admin"}
            ),
            auth_overwrite_role_on_login=True,
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)
        # Guard lives in base.py — patch its settings import too.
        monkeypatch.setattr("engine.api.auth.base.settings", s)

        attrs = {
            "uid": [b"promoteme"],
            "mail": [b"promoteme@example.com"],
            "cn": [b"Promote Me"],
            "memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"],
        }

        mock_ldap = MagicMock()
        mock_ldap.OPT_NETWORK_TIMEOUT = 7
        mock_ldap.OPT_TIMEOUT = 8
        mock_ldap.SCOPE_SUBTREE = 2

        class _Conn:
            def set_option(self, *_a, **_kw): ...
            def simple_bind_s(self, *_a, **_kw): ...
            def search_s(self, *_a, **_kw):
                return [("uid=promoteme,ou=users,dc=example,dc=com", attrs)]
            def unbind_s(self): ...

        mock_ldap.initialize = MagicMock(return_value=_Conn())
        mock_filter = MagicMock()
        mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)

        existing_user = User(
            email="promoteme@example.com",
            display_name="Promote Me",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="promoteme",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        provider = LDAPAuthProvider()
        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}
        ):
            result = await provider.authenticate(
                username="promoteme", password="irrelevant", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_called_once()


# ---------------------------------------------------------------------------
# Log sanitization
# ---------------------------------------------------------------------------


class TestSanitizeRoleForLog:
    """Direct unit tests for :func:`sanitize_role_for_log` plus
    integration coverage showing that ``map_roles`` actually pipes
    unrecognized roles through it before emitting the warning."""

    def test_strip_newlines_and_carriage_returns(self):
        from engine.api.auth.base import sanitize_role_for_log

        # ``\n`` and ``\r`` are log-injection classics; they must be
        # gone from the sanitized output.
        assert sanitize_role_for_log("good\nbad") == "goodbad"
        assert sanitize_role_for_log("a\rb") == "ab"
        assert sanitize_role_for_log("a\r\nb") == "ab"

    def test_strip_tab_and_other_c0_controls(self):
        from engine.api.auth.base import sanitize_role_for_log

        for code in range(0x20):
            assert sanitize_role_for_log(f"a{chr(code)}b") == "ab"
        # DEL (0x7f) must also be stripped.
        assert sanitize_role_for_log("a\x7fb") == "ab"

    def test_strip_ansi_escape_sequences(self):
        """ANSI bombs (e.g. ``\\x1b[2J\\x1b[H``) must be neutralized
        so they cannot blank an operator's terminal when the warning
        is rendered. Stripping the ``\\x1b`` byte is what defangs them
        — the remaining ``[`` is a benign printable character that
        cannot start a real CSI sequence on its own."""
        from engine.api.auth.base import sanitize_role_for_log

        ansi_bomb = "\x1b[2J\x1b[H Boom "
        cleaned = sanitize_role_for_log(ansi_bomb)
        assert "\x1b" not in cleaned, "ESC byte must be stripped"
        # The remainder is harmless text — terminals only honor CSI
        # sequences when preceded by an ESC byte.
        assert "Boom" in cleaned
        # And the byte count dropped by exactly the number of ESCs.
        assert cleaned.count("\x1b") == 0

    def test_truncates_overlong_role_strings(self):
        """A role string longer than the cap must be truncated and
        terminated with an ellipsis so operators can tell it was
        shortened."""
        from engine.api.auth.base import _MAX_LOG_ROLE_LENGTH, sanitize_role_for_log

        # 10x the cap — defensive against a future cap change.
        huge = "A" * (_MAX_LOG_ROLE_LENGTH * 10)
        out = sanitize_role_for_log(huge)
        assert len(out) == _MAX_LOG_ROLE_LENGTH + 1  # +1 for the ellipsis
        assert out.endswith("…")

    def test_short_strings_pass_through_unchanged(self):
        from engine.api.auth.base import sanitize_role_for_log

        assert sanitize_role_for_log("admin") == "admin"
        assert sanitize_role_for_log("cn=admins,ou=groups") == (
            "cn=admins,ou=groups"
        )

    def test_unicode_letters_preserved(self):
        """Sanitization is *control-character* removal, not ASCII
        enforcement — non-ASCII letters in legitimate group DNs
        (e.g. Cyrillic, accented Latin) must survive."""
        from engine.api.auth.base import sanitize_role_for_log

        assert sanitize_role_for_log("Админы") == "Админы"
        assert sanitize_role_for_log("café-admin") == "café-admin"

    def test_non_string_input_is_coerced(self):
        """IdPs occasionally emit numeric or bytes-ish role claims;
        the sanitizer must never raise."""
        from engine.api.auth.base import sanitize_role_for_log

        assert sanitize_role_for_log(None) == ""
        assert sanitize_role_for_log(42) == "42"
        assert sanitize_role_for_log(b"admin") == "b'admin'"

    def test_empty_string_round_trips(self):
        from engine.api.auth.base import sanitize_role_for_log

        assert sanitize_role_for_log("") == ""

    def test_idempotent(self):
        """Running the sanitizer twice must equal running it once —
        important for code paths that defensively re-sanitize."""
        from engine.api.auth.base import sanitize_role_for_log

        sample = "weird\x00role\nname"
        once = sanitize_role_for_log(sample)
        twice = sanitize_role_for_log(once)
        assert once == twice


class TestMapRolesUsesSanitizerInWarning:
    """Integration: ``map_roles`` must run every unrecognized role
    through :func:`sanitize_role_for_log` before it lands in the
    warning payload."""

    def _patch_logger(self, monkeypatch):
        from engine.api.auth import base

        calls: list[dict] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, *_a, **_kw):  # pragma: no cover
                pass

            def error(self, *_a, **_kw):  # pragma: no cover
                pass

        monkeypatch.setattr(base, "logger", _Stub())
        return calls

    def test_control_chars_stripped_from_unrecognized_payload(self, monkeypatch):
        """If the IdP ships an unrecognized role containing a newline,
        that newline must NOT make it into the warning payload —
        otherwise structured-log consumers (ELK, Loki, Datadog) would
        see a forged extra line."""
        calls = self._patch_logger(monkeypatch)
        p = _concrete_provider()
        assert p.map_roles(["good", "bad\nname"]) == "viewer"
        assert calls, "Expected at least one warning"
        unrecognized = calls[0]["unrecognized"]
        assert "bad\nname" not in unrecognized
        assert "badname" in unrecognized
        # And the legitimate control-char-free role is untouched.
        assert "good" in unrecognized

    def test_truncated_overlong_role_in_payload(self, monkeypatch):
        """An unrecognized role longer than the sanitizer cap must
        appear truncated (with trailing ellipsis) in the warning."""
        from engine.api.auth.base import _MAX_LOG_ROLE_LENGTH

        calls = self._patch_logger(monkeypatch)
        p = _concrete_provider()
        huge = "X" * (_MAX_LOG_ROLE_LENGTH * 5)
        p.map_roles([huge])
        assert calls
        unrecognized = calls[0]["unrecognized"]
        assert len(unrecognized) == 1
        assert unrecognized[0].endswith("…")
        assert len(unrecognized[0]) == _MAX_LOG_ROLE_LENGTH + 1

    def test_recognized_roles_not_sanitized_in_payload(self, monkeypatch):
        """Recognized roles are already validated against an internal
        allow-list and need no sanitization. The warning's
        ``recognized`` field should contain them verbatim (lowercased
        and stripped, as the matcher saw them)."""
        calls = self._patch_logger(monkeypatch)
        p = _concrete_provider()
        p.map_roles(["admin", "weird\none"])
        assert calls
        # ``recognized`` carries the normalized admin only.
        assert calls[0]["recognized"] == ["admin"]

    def test_mapped_role_in_warning_uses_viewer_fallback(self, monkeypatch):
        """When nothing is recognized, the warning's ``mapped=`` field
        must reflect the new ``viewer`` fallback, not the legacy
        ``user`` value — operators key alerts on this payload."""
        calls = self._patch_logger(monkeypatch)
        p = _concrete_provider()
        p.map_roles(["bogus"])
        assert calls
        assert calls[0]["mapped"] == "viewer"


# ---------------------------------------------------------------------------
# RBAC integration: viewer fallback really blocks write endpoints
# ---------------------------------------------------------------------------


class TestViewerFallbackBlocksWrites:
    """End-to-end sanity: when ``map_roles`` falls back to ``viewer``,
    the resulting user must NOT be able to hit a ``require_role("user")``
    endpoint. This is the security guarantee the fallback change
    exists to provide."""

    async def test_viewer_cannot_reach_user_endpoint(self):
        from fastapi import Depends, FastAPI
        from httpx import ASGITransport, AsyncClient

        from engine.api.auth.dependency import get_current_user, require_role
        from engine.db.models import User
        from tests.conftest import FAKE_USER_ID

        mapped = _concrete_provider().map_roles([])  # -> "viewer"
        assert mapped == "viewer"

        app = FastAPI()

        @app.get("/write")
        async def handler(user: User = Depends(require_role("user"))):
            return {"role": user.role}

        fake_user = User(
            id=FAKE_USER_ID,
            email="leastpriv@example.com",
            display_name="Least Priv",
            is_active=True,
            role=mapped,
            auth_provider="ldap",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/write")
        assert resp.status_code == 403, (
            "viewer-fallback user must not be able to hit a write endpoint"
        )

    async def test_viewer_can_reach_viewer_endpoint(self):
        from fastapi import Depends, FastAPI
        from httpx import ASGITransport, AsyncClient

        from engine.api.auth.dependency import get_current_user, require_role
        from engine.db.models import User
        from tests.conftest import FAKE_USER_ID

        app = FastAPI()

        @app.get("/read")
        async def handler(user: User = Depends(require_role("viewer"))):
            return {"role": user.role}

        fake_user = User(
            id=FAKE_USER_ID,
            email="leastpriv@example.com",
            display_name="Least Priv",
            is_active=True,
            role="viewer",
            auth_provider="ldap",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/read")
        assert resp.status_code == 200
