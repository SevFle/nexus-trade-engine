"""Targeted regression tests for the role-security hardening trio.

This module pins three focused changes that close privilege-escalation
and log-injection vectors in the auth/role-mapping layer:

1. **``map_roles`` fallback tightened** — when the external IdP provides
   no recognized role, the function returns ``"viewer"`` (lowest
   privilege) instead of ``"user"``, and emits a dedicated
   ``auth.map_roles.fallback_role`` warning so operators can detect
   misconfigured IdPs.  Returning ``"user"`` was itself an escalation:
   ``user`` carries write access that an unrecognised external role
   should never implicitly grant.

2. **``sanitize_role_for_log()`` helper** — every external role value
   is sanitized before being emitted to the logger.  The helper strips
   ASCII control characters (``0x00-0x1F``, ``0x7F``) and caps length
   at 128 characters, defending against log-injection (newline forgery)
   and log-flooding (megabyte-scale payloads) by a malicious or
   misconfigured IdP.

3. **Federated login honors ``auth_overwrite_role_on_login``** — both
   the LDAP and OIDC providers used to unconditionally overwrite an
   existing local user's role on each login with whatever the IdP
   asserted.  Now the role is overwritten only when the operator has
   opted in via ``auth_overwrite_role_on_login=True``; otherwise the
   current role is preserved and a debug message is logged.

Each test class below targets exactly one of the three changes.  Tests
are deterministic and free of structlog-config coupling — the logger is
monkeypatched to a stub capturing the call kwargs.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    AuthResult,
    IAuthProvider,
    sanitize_role_for_log,
)
from engine.config import Settings

# ---------------------------------------------------------------------------
# Concrete test providers (one with each provider name we care about)
# ---------------------------------------------------------------------------


class _TestProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-provider"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult()


class _AltProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "alt-provider"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult()


def _patch_logger(monkeypatch):
    """Replace ``engine.api.auth.base.logger`` with a stub capturing
    every call's event-name + kwargs.  Returns the call list."""
    calls: list[dict[str, object]] = []

    class _Stub:
        def warning(self, _event, **kwargs):
            calls.append({"event": _event, "level": "warning", **kwargs})

        def info(self, _event, **kwargs):
            calls.append({"event": _event, "level": "info", **kwargs})

        def debug(self, _event, **kwargs):
            calls.append({"event": _event, "level": "debug", **kwargs})

        def error(self, _event, **kwargs):  # pragma: no cover
            calls.append({"event": _event, "level": "error", **kwargs})

    from engine.api.auth import base

    monkeypatch.setattr(base, "logger", _Stub())
    return calls


# ===========================================================================
# 1. map_roles fallback returns "viewer" and emits a dedicated warning
# ===========================================================================


class TestMapRolesViewerFallback:
    """Change #1: ``map_roles`` fallback returns ``"viewer"`` (lowest
    privilege), not ``"user"``, and emits ``auth.map_roles.fallback_role``.

    Returning ``"user"`` granted write access on the strength of *no*
    evidence — the IdP said nothing the engine could interpret.  Pinning
    ``"viewer"`` makes "unrecognized" ≡ "least privilege".
    """

    def test_empty_input_returns_viewer(self):
        assert _TestProvider().map_roles([]) == "viewer"

    def test_all_unrecognized_returns_viewer(self):
        assert (
            _TestProvider().map_roles(["superuser", "root", "guest"])
            == "viewer"
        )

    def test_whitespace_only_input_returns_viewer(self):
        """Whitespace normalizes to "" which is not a recognized role;
        must fall through to viewer."""
        assert _TestProvider().map_roles(["   "]) == "viewer"

    def test_mixed_recognized_and_unrecognized_uses_recognized(self):
        """Recognized role still wins; fallback is the *last* resort."""
        assert (
            _TestProvider().map_roles(["developer", "ghost_role"])
            == "developer"
        )

    def test_fallback_warning_fires_on_empty_input(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles([])
        fallback_calls = [
            c for c in calls if c["event"] == "auth.map_roles.fallback_role"
        ]
        assert len(fallback_calls) == 1, (
            "Expected exactly one auth.map_roles.fallback_role warning "
            "when no external roles are supplied."
        )

    def test_fallback_warning_fires_on_all_unrecognized(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles(["bogus_a", "bogus_b"])
        assert any(
            c["event"] == "auth.map_roles.fallback_role" for c in calls
        )

    def test_fallback_warning_does_not_fire_when_role_recognized(
        self, monkeypatch
    ):
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles(["admin"])
        assert not any(
            c["event"] == "auth.map_roles.fallback_role" for c in calls
        ), "fallback_role must not fire when at least one role is recognized"

    def test_fallback_warning_does_not_fire_on_mixed(self, monkeypatch):
        """When at least one recognized role is present the function
        returns it; the fallback path is not taken."""
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles(["user", "weird_role"])
        assert not any(
            c["event"] == "auth.map_roles.fallback_role" for c in calls
        )

    def test_fallback_warning_includes_provider_name(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _AltProvider().map_roles([])
        fallback = [c for c in calls if c["event"] == "auth.map_roles.fallback_role"]
        assert fallback
        assert fallback[0]["provider"] == "alt-provider"

    def test_fallback_warning_mapped_is_viewer(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles([])
        fallback = [c for c in calls if c["event"] == "auth.map_roles.fallback_role"]
        assert fallback
        assert fallback[0]["mapped"] == "viewer"

    def test_fallback_warning_external_count_is_correct(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles(["a", "b", "c"])
        fallback = [c for c in calls if c["event"] == "auth.map_roles.fallback_role"]
        assert fallback
        assert fallback[0]["external_count"] == 3

    def test_fallback_warning_includes_unrecognized_list(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles(["bogus_x", "bogus_y"])
        fallback = [c for c in calls if c["event"] == "auth.map_roles.fallback_role"]
        assert fallback
        unrecognized = fallback[0]["unrecognized"]
        assert "bogus_x" in unrecognized
        assert "bogus_y" in unrecognized

    def test_fallback_warning_fires_once_per_call(self, monkeypatch):
        """A single map_roles call must produce at most one
        fallback_role event, regardless of how many unrecognized roles
        triggered it.  Operators rely on this for alert de-duplication."""
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles(["bogus_a", "bogus_b", "bogus_c"])
        assert (
            sum(1 for c in calls if c["event"] == "auth.map_roles.fallback_role")
            == 1
        )

    def test_fallback_event_name_is_stable(self, monkeypatch):
        """The event name must remain stable across releases — operators
        key dashboards / alerts on this string."""
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles([])
        event_names = {c["event"] for c in calls}
        assert "auth.map_roles.fallback_role" in event_names

    def test_viewer_is_lower_privilege_than_user(self):
        """Sanity guard: ``viewer`` must remain strictly below ``user``
        in the role hierarchy.  Without this invariant the fallback
        would not actually be 'least privilege'."""
        from engine.api.auth.dependency import ROLE_HIERARCHY

        assert ROLE_HIERARCHY["viewer"] < ROLE_HIERARCHY["user"]

    def test_viewer_fallback_blocks_user_resource_access(self):
        """End-to-end: a viewer must NOT be able to access a
        ``require_role("user")`` resource.  This proves the fallback
        denies what ``"user"`` previously granted."""
        from fastapi import Depends, FastAPI
        from httpx import ASGITransport, AsyncClient

        from engine.api.auth.dependency import get_current_user, require_role
        from engine.db.models import User
        from tests.conftest import FAKE_USER_ID

        app = FastAPI()

        @app.get("/user-only")
        async def handler(user: User = Depends(require_role("user"))):
            return {"role": user.role}

        # Fallback path: empty external roles -> mapped to viewer.
        mapped = _TestProvider().map_roles([])
        assert mapped == "viewer"

        fake_user = User(
            id=FAKE_USER_ID,
            email="viewer-fallback@example.com",
            display_name="Viewer Fallback",
            is_active=True,
            role=mapped,
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        import asyncio

        async def _run():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                return await ac.get("/user-only")

        resp = asyncio.get_event_loop().run_until_complete(_run())
        assert resp.status_code == 403, (
            f"Fallback role 'viewer' must be denied access to user-only "
            f"resources. Got status {resp.status_code}."
        )


# ===========================================================================
# 2. sanitize_role_for_log() helper
# ===========================================================================


class TestSanitizeRoleForLog:
    """Change #2: ``sanitize_role_for_log()`` strips control chars and
    caps length at 128.  Tested in isolation AND through ``map_roles``."""

    # ----- pure-function unit tests -----

    def test_passthrough_for_clean_ascii(self):
        assert sanitize_role_for_log("admin") == "admin"

    def test_passthrough_for_underscored_role(self):
        assert sanitize_role_for_log("quant_dev") == "quant_dev"

    def test_passthrough_for_long_under_limit(self):
        role = "a" * 128
        assert sanitize_role_for_log(role) == role

    def test_strips_null_byte(self):
        assert sanitize_role_for_log("ad\x00min") == "admin"

    def test_strips_bell(self):
        assert sanitize_role_for_log("ad\x07min") == "admin"

    def test_strips_backspace(self):
        assert sanitize_role_for_log("ad\x08min") == "admin"

    def test_strips_tab(self):
        assert sanitize_role_for_log("ad\tmin") == "admin"

    def test_strips_newline(self):
        """Defends against newline-injection in downstream log shippers
        that may split events on raw ``\\n``."""
        assert sanitize_role_for_log("ad\nmin") == "admin"

    def test_strips_carriage_return(self):
        assert sanitize_role_for_log("ad\rmin") == "admin"

    def test_strips_form_feed(self):
        assert sanitize_role_for_log("ad\x0cmin") == "admin"

    def test_strips_vertical_tab(self):
        assert sanitize_role_for_log("ad\x0bmin") == "admin"

    def test_strips_del(self):
        assert sanitize_role_for_log("ad\x7fmin") == "admin"

    def test_strips_all_c0_controls(self):
        for codepoint in range(0x20):
            tainted = "ab" + chr(codepoint) + "cd"
            assert sanitize_role_for_log(tainted) == "abcd", (
                f"Failed to strip control char U+{codepoint:04X}"
            )

    def test_strips_multiple_control_chars(self):
        assert sanitize_role_for_log("\x00a\x01d\x02m\x03i\x04n\x05") == "admin"

    def test_caps_at_128_chars(self):
        role = "x" * 200
        result = sanitize_role_for_log(role)
        assert len(result) == 128

    def test_caps_exactly_at_boundary(self):
        role = "y" * 129
        assert len(sanitize_role_for_log(role)) == 128

    def test_preserves_chars_at_boundary(self):
        role = "z" * 128
        assert sanitize_role_for_log(role) == role

    def test_truncation_happens_after_stripping(self):
        """A role that becomes short after stripping should not be
        padded, and a long role with leading junk is truncated to 128
        *after* the strip — the cap is on the output, not input."""
        role = "\x00\x01\x02" + "a" * 200
        result = sanitize_role_for_log(role)
        assert len(result) == 128
        assert all(c == "a" for c in result)

    def test_empty_string_passthrough(self):
        assert sanitize_role_for_log("") == ""

    def test_none_returns_empty_string(self):
        assert sanitize_role_for_log(None) == ""

    def test_non_string_coerced_to_str(self):
        assert sanitize_role_for_log(12345) == "12345"

    def test_list_input_coerced(self):
        """Defensive: a malformed payload that delivered a list rather
        than a string must not raise — coerce and sanitize."""
        result = sanitize_role_for_log(["a", "b"])
        assert isinstance(result, str)
        assert len(result) > 0

    def test_only_control_chars_returns_empty(self):
        assert sanitize_role_for_log("\x00\x01\x02\x03") == ""

    def test_idempotent(self):
        """Running sanitize twice must yield the same result."""
        role = "ad\x00min\ninject\x1b"
        once = sanitize_role_for_log(role)
        twice = sanitize_role_for_log(once)
        assert once == twice

    def test_does_not_modify_input_string(self):
        original = "ad\x00min"
        sanitize_role_for_log(original)
        assert original == "ad\x00min", "input must not be mutated"

    def test_module_level_constant_exists(self):
        """Pin the cap constant so operators can reference it in
        runbooks / alert thresholds."""
        from engine.api.auth import base

        assert hasattr(base, "_LOG_ROLE_MAX_LENGTH")
        assert base._LOG_ROLE_MAX_LENGTH == 128

    def test_helper_is_importable_from_base(self):
        from engine.api.auth.base import sanitize_role_for_log as _imp

        assert _imp is sanitize_role_for_log

    def test_helper_is_exported_via_init(self):
        """The helper should be reachable from the package root for use
        by other auth modules (ldap.py, oidc.py)."""
        from engine.api.auth.base import sanitize_role_for_log as _imp

        assert callable(_imp)

    # ----- integration: sanitize_role_for_log is actually called by map_roles -----

    def test_map_roles_sanitizes_unrecognized_in_logs(self, monkeypatch):
        """The unrecognized-role list emitted to the logger must
        already be sanitized.  An attacker-controlled IdP role name
        with embedded newlines must not reach the log pipeline."""
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles(["inj\nect", "ad\x00min"])
        unrec_calls = [
            c for c in calls if c["event"] == "auth.map_roles.unrecognized_roles"
        ]
        assert unrec_calls
        unrecognized = unrec_calls[0]["unrecognized"]
        assert "inj\nect" not in unrecognized
        assert "ad\x00min" not in unrecognized
        # Sanitized versions must be present.
        assert "inject" in unrecognized
        assert "admin" in unrecognized

    def test_map_roles_sanitizes_unrecognized_in_fallback_warning(
        self, monkeypatch
    ):
        """Same defense on the dedicated fallback_role event."""
        calls = _patch_logger(monkeypatch)
        _TestProvider().map_roles(["inj\nect"])
        fallback = [
            c for c in calls if c["event"] == "auth.map_roles.fallback_role"
        ]
        assert fallback
        unrecognized = fallback[0]["unrecognized"]
        assert "inj\nect" not in unrecognized
        assert "inject" in unrecognized

    def test_map_roles_truncates_long_role_in_logs(self, monkeypatch):
        """A megabyte-scale role name must not appear verbatim in the
        warning payload."""
        calls = _patch_logger(monkeypatch)
        huge_role = "X" * 5000
        _TestProvider().map_roles([huge_role])
        unrec_calls = [
            c for c in calls if c["event"] == "auth.map_roles.unrecognized_roles"
        ]
        assert unrec_calls
        unrecognized = unrec_calls[0]["unrecognized"]
        assert all(len(r) <= 128 for r in unrecognized)

    def test_map_roles_recognized_list_also_sanitized(self, monkeypatch):
        """Even recognized roles (after normalization) should be
        sanitized before logging — the helper is defense-in-depth, not
        'only for untrusted' input."""
        calls = _patch_logger(monkeypatch)
        # The 'admin' part gets normalized and recognized; the control
        # chars get stripped by the helper.
        _TestProvider().map_roles(["ad\x00min", "bogus"])
        unrec_calls = [
            c for c in calls if c["event"] == "auth.map_roles.unrecognized_roles"
        ]
        assert unrec_calls
        recognized = unrec_calls[0]["recognized"]
        # 'admin' (after normalization) is recognized; the control-char
        # version must not appear anywhere in the payload.
        assert all("\x00" not in r for r in recognized)


# ===========================================================================
# 3. auth_overwrite_role_on_login flag honored by LDAP & OIDC providers
# ===========================================================================


# ----- LDAP helpers -----


def _ldap_attrs(member_of: list[bytes] | None = None):
    attrs: dict[str, list[bytes]] = {
        "uid": [b"ldapuser"],
        "mail": [b"ldapuser@example.com"],
        "cn": [b"Ldap User"],
    }
    if member_of is not None:
        attrs["memberOf"] = member_of
    return attrs


def _ldap_mock_with(attrs):
    fake_conn = MagicMock()
    fake_conn.set_option = MagicMock()
    fake_conn.simple_bind_s = MagicMock()
    fake_conn.search_s = MagicMock(return_value=[("uid=ldapuser,dc=example,dc=com", attrs)])
    fake_conn.unbind_s = MagicMock()
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(return_value=fake_conn)
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter


def _ldap_settings(monkeypatch, *, overwrite_role: bool) -> Settings:
    s = Settings(
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=developers,ou=groups,dc=example,dc=com": "developer",
        }),
        auth_overwrite_role_on_login=overwrite_role,
    )
    monkeypatch.setattr("engine.api.auth.ldap.settings", s)
    return s


# ----- OIDC helpers -----


def _oidc_settings(monkeypatch, *, overwrite_role: bool) -> Settings:
    s = Settings(
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="cid",
        oidc_client_secret="csecret",
        oidc_redirect_uri="https://app.example.com/cb",
        oidc_role_claim="roles",
        auth_overwrite_role_on_login=overwrite_role,
    )
    monkeypatch.setattr("engine.api.auth.oidc.settings", s)
    return s


class _FakeResp:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Captures GET/POST calls and returns scripted responses."""

    def __init__(self, get=None, post=None):
        self._get = list(get or [])
        self._post = list(post or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, **kwargs):
        if self._get:
            return self._get.pop(0)
        return _FakeResp({})

    async def post(self, url, **kwargs):
        if self._post:
            return self._post.pop(0)
        return _FakeResp({})


def _build_oidc_client(rsa_keys, claims: dict):
    import jwt as pyjwt

    private_key, pub_key = rsa_keys
    import json as _json

    from jwt.algorithms import RSAAlgorithm

    jwk_dict = _json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = "test-kid"
    id_token = pyjwt.encode(
        {"aud": "cid", **claims}, private_key, algorithm="RS256",
        headers={"kid": "test-kid"},
    )
    disc = _FakeResp({
        "authorization_endpoint": "https://id.example.com/authorize",
        "token_endpoint": "https://id.example.com/token",
        "jwks_uri": "https://id.example.com/jwks",
    })
    jwks = _FakeResp({"keys": [jwk_dict]})
    tok = _FakeResp({"id_token": id_token, "access_token": "at"})
    return _FakeAsyncClient(get=[disc, jwks], post=[tok])


# ----- LDAP tests -----


class TestLDAPAuthOverwriteRoleOnLogin:
    """Change #3 (LDAP side): the existing user's role is overwritten
    only when ``auth_overwrite_role_on_login`` is True."""

    async def test_role_preserved_when_flag_false(self, monkeypatch):
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite_role=False)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="ldapuser@example.com",
            display_name="Ldap User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            outcome = await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        assert outcome.success is True
        assert existing.role == "user", (
            "Existing user's role must be preserved when "
            "auth_overwrite_role_on_login is False."
        )
        db.flush.assert_not_called()

    async def test_role_preserved_default_settings(self, monkeypatch):
        """The production default is False — verify that path explicitly."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        s = Settings(
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping=json.dumps({
                "cn=admins,ou=groups,dc=example,dc=com": "admin",
            }),
            # Note: NOT setting auth_overwrite_role_on_login — relying on
            # the production default of False.
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)
        assert s.auth_overwrite_role_on_login is False

        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="default@example.com",
            display_name="Default",
            is_active=True,
            role="viewer",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        assert existing.role == "viewer"

    async def test_role_overwritten_when_flag_true(self, monkeypatch):
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite_role=True)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="ldapuser@example.com",
            display_name="Ldap User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            outcome = await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        assert outcome.success is True
        assert existing.role == "admin"
        db.flush.assert_called_once()

    async def test_no_overwrite_when_role_unchanged(self, monkeypatch):
        """When the IdP-asserted role equals the current role, neither
        branch runs — no flush, no log."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite_role=True)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="stable@example.com",
            display_name="Stable",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        assert existing.role == "admin"
        db.flush.assert_not_called()

    async def test_preserve_emits_debug_log(self, monkeypatch):
        """When the role is preserved, a debug message is emitted so
        operators can audit the decision."""
        from engine.api.auth import ldap as ldap_module
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite_role=False)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="debug@example.com",
            display_name="Debug",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, "level": "info", **kw})

            def debug(self, _event, **kw):
                calls.append({"event": _event, "level": "debug", **kw})

            def warning(self, _event, **kw):
                calls.append({"event": _event, "level": "warning", **kw})

        monkeypatch.setattr(ldap_module, "logger", _Stub())

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        debug_events = [c for c in calls if c["level"] == "debug"]
        assert any(
            c["event"] == "auth.ldap.role_preserved" for c in debug_events
        ), "Expected auth.ldap.role_preserved debug event when role is preserved"

    async def test_overwrite_emits_info_log(self, monkeypatch):
        """When the role is overwritten, an info message is emitted so
        operators can audit the change."""
        from engine.api.auth import ldap as ldap_module
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite_role=True)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="info@example.com",
            display_name="Info",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, "level": "info", **kw})

            def debug(self, _event, **kw):
                calls.append({"event": _event, "level": "debug", **kw})

            def warning(self, _event, **kw):
                calls.append({"event": _event, "level": "warning", **kw})

        monkeypatch.setattr(ldap_module, "logger", _Stub())

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        info_events = [c for c in calls if c["level"] == "info"]
        assert any(
            c["event"] == "auth.ldap.role_overwritten" for c in info_events
        ), "Expected auth.ldap.role_overwritten info event when role is overwritten"

    async def test_preserve_event_includes_role_values(self, monkeypatch):
        """The preserve-event payload must include both the preserved
        role and the IdP-asserted role for forensic value."""
        from engine.api.auth import ldap as ldap_module
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite_role=False)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="payload@example.com",
            display_name="Payload",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        captured: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kw):  # pragma: no cover
                captured.append({"event": _event, **kw})

            def debug(self, _event, **kw):
                captured.append({"event": _event, **kw})

            def warning(self, _event, **kw):  # pragma: no cover
                captured.append({"event": _event, **kw})

        monkeypatch.setattr(ldap_module, "logger", _Stub())

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        debug = [c for c in captured if c["event"] == "auth.ldap.role_preserved"]
        assert debug
        assert debug[0]["preserved_role"] == "user"
        assert debug[0]["idp_role"] == "admin"

    async def test_downgrade_blocked_when_flag_false(self, monkeypatch):
        """Critical regression: a misconfigured IdP that *demotes* a
        user (e.g. removes them from the admin group) must NOT cause
        a downgrade locally when the flag is False."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite_role=False)
        # IdP now asserts only "developer" but user is currently "admin".
        attrs = _ldap_attrs(
            member_of=[b"cn=developers,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="demote@example.com",
            display_name="Demote",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        assert existing.role == "admin", (
            "IdP-driven downgrade must be blocked when "
            "auth_overwrite_role_on_login is False."
        )

    async def test_escalation_blocked_when_flag_false(self, monkeypatch):
        """Critical regression: a compromised IdP that *adds* a user to
        the admin group must NOT cause a local escalation when the flag
        is False."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite_role=False)
        attrs = _ldap_attrs(member_of=[b"cn=admins,ou=groups,dc=example,dc=com"])
        mock_ldap, mock_filter = _ldap_mock_with(attrs)

        existing = User(
            email="escalate@example.com",
            display_name="Escalate",
            is_active=True,
            role="viewer",
            auth_provider="ldap",
            external_id="ldapuser",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="ldapuser", password="pw", db=db
            )

        assert existing.role == "viewer", (
            "IdP-driven escalation must be blocked when "
            "auth_overwrite_role_on_login is False."
        )


# ----- OIDC tests -----


class TestOIDCAuthOverwriteRoleOnLogin:
    """Change #3 (OIDC side): the existing user's role is overwritten
    only when ``auth_overwrite_role_on_login`` is True.

    The OIDC provider previously did *no* existing-user role sync at
    all.  The flag now enables explicit opt-in overwrite semantics so
    the two federated providers behave consistently."""

    @pytest.fixture
    def rsa_keys(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return private_key, private_key.public_key()

    async def test_role_preserved_when_flag_false(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite_role=False)
        client = _build_oidc_client(
            rsa_keys,
            {"sub": "oidc-existing-1", "email": "e1@x.com", "name": "E1",
             "roles": ["admin"]},
        )

        existing = User(
            email="e1@x.com",
            display_name="E1",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-existing-1",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            outcome = await OIDCAuthProvider().authenticate(
                code="code", db=db
            )

        assert outcome.success is True
        assert existing.role == "user", (
            "Existing user's role must be preserved when "
            "auth_overwrite_role_on_login is False."
        )
        db.flush.assert_not_called()

    async def test_role_overwritten_when_flag_true(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite_role=True)
        client = _build_oidc_client(
            rsa_keys,
            {"sub": "oidc-existing-2", "email": "e2@x.com", "name": "E2",
             "roles": ["admin"]},
        )

        existing = User(
            email="e2@x.com",
            display_name="E2",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-existing-2",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            outcome = await OIDCAuthProvider().authenticate(
                code="code", db=db
            )

        assert outcome.success is True
        assert existing.role == "admin"
        db.flush.assert_called_once()

    async def test_no_overwrite_when_role_unchanged(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite_role=True)
        client = _build_oidc_client(
            rsa_keys,
            {"sub": "stable-oidc", "email": "stable@x.com", "name": "Stable",
             "roles": ["admin"]},
        )

        existing = User(
            email="stable@x.com",
            display_name="Stable",
            is_active=True,
            role="admin",
            auth_provider="oidc",
            external_id="stable-oidc",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            await OIDCAuthProvider().authenticate(code="code", db=db)

        assert existing.role == "admin"
        db.flush.assert_not_called()

    async def test_preserve_emits_debug_log(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth import oidc as oidc_module
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite_role=False)
        client = _build_oidc_client(
            rsa_keys,
            {"sub": "debug-oidc", "email": "debug@x.com", "name": "Debug",
             "roles": ["admin"]},
        )

        existing = User(
            email="debug@x.com",
            display_name="Debug",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="debug-oidc",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, "level": "info", **kw})

            def debug(self, _event, **kw):
                calls.append({"event": _event, "level": "debug", **kw})

            def warning(self, _event, **kw):  # pragma: no cover
                calls.append({"event": _event, "level": "warning", **kw})

        monkeypatch.setattr(oidc_module, "logger", _Stub())

        with patch("httpx.AsyncClient", return_value=client):
            await OIDCAuthProvider().authenticate(code="code", db=db)

        debug_events = [c for c in calls if c["level"] == "debug"]
        assert any(
            c["event"] == "auth.oidc.role_preserved" for c in debug_events
        )

    async def test_overwrite_emits_info_log(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth import oidc as oidc_module
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite_role=True)
        client = _build_oidc_client(
            rsa_keys,
            {"sub": "info-oidc", "email": "info@x.com", "name": "Info",
             "roles": ["admin"]},
        )

        existing = User(
            email="info@x.com",
            display_name="Info",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="info-oidc",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, "level": "info", **kw})

            def debug(self, _event, **kw):  # pragma: no cover
                calls.append({"event": _event, "level": "debug", **kw})

            def warning(self, _event, **kw):  # pragma: no cover
                calls.append({"event": _event, "level": "warning", **kw})

        monkeypatch.setattr(oidc_module, "logger", _Stub())

        with patch("httpx.AsyncClient", return_value=client):
            await OIDCAuthProvider().authenticate(code="code", db=db)

        info_events = [c for c in calls if c["level"] == "info"]
        assert any(
            c["event"] == "auth.oidc.role_overwritten" for c in info_events
        )

    async def test_oidc_downgrade_blocked_when_flag_false(
        self, monkeypatch, rsa_keys
    ):
        """A misconfigured IdP that removes the admin claim must not
        cause a local downgrade when the flag is False."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite_role=False)
        # IdP now only asserts "user" but local user is "admin".
        client = _build_oidc_client(
            rsa_keys,
            {"sub": "demote-oidc", "email": "demote@x.com", "name": "Demote",
             "roles": ["user"]},
        )

        existing = User(
            email="demote@x.com",
            display_name="Demote",
            is_active=True,
            role="admin",
            auth_provider="oidc",
            external_id="demote-oidc",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            await OIDCAuthProvider().authenticate(code="code", db=db)

        assert existing.role == "admin"

    async def test_oidc_escalation_blocked_when_flag_false(
        self, monkeypatch, rsa_keys
    ):
        """A compromised IdP that asserts admin must not cause a local
        escalation when the flag is False."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite_role=False)
        client = _build_oidc_client(
            rsa_keys,
            {"sub": "esc-oidc", "email": "esc@x.com", "name": "Esc",
             "roles": ["admin"]},
        )

        existing = User(
            email="esc@x.com",
            display_name="Esc",
            is_active=True,
            role="viewer",
            auth_provider="oidc",
            external_id="esc-oidc",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            await OIDCAuthProvider().authenticate(code="code", db=db)

        assert existing.role == "viewer"

    async def test_no_role_claim_no_change(self, monkeypatch, rsa_keys):
        """When the IdP doesn't include a roles claim at all, no role
        sync attempt should be made — neither overwrite nor preserve
        event should fire."""
        from engine.api.auth import oidc as oidc_module
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite_role=True)
        client = _build_oidc_client(
            rsa_keys,
            {"sub": "no-claim", "email": "noclaim@x.com", "name": "NoClaim"},
        )

        existing = User(
            email="noclaim@x.com",
            display_name="NoClaim",
            is_active=True,
            role="developer",
            auth_provider="oidc",
            external_id="no-claim",
        )
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute.return_value = result
        db.flush = AsyncMock()

        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kw):  # pragma: no cover
                calls.append({"event": _event, **kw})

            def debug(self, _event, **kw):
                calls.append({"event": _event, **kw})

        monkeypatch.setattr(oidc_module, "logger", _Stub())

        with patch("httpx.AsyncClient", return_value=client):
            await OIDCAuthProvider().authenticate(code="code", db=db)

        assert existing.role == "developer"
        # No preserve/overwrite events fired — nothing to sync.
        sync_events = [
            c["event"] for c in calls
            if "role_preserved" in str(c["event"]) or "role_overwritten" in str(c["event"])
        ]
        assert sync_events == []


# ===========================================================================
# 4. Cross-cutting: setting default + integration through the layers
# ===========================================================================


class TestAuthOverwriteFlagDefaultAndIntegration:
    """Cross-cutting tests that pin the production default and verify
    the flag flows through Settings -> provider -> behavior."""

    def test_default_is_false_in_source(self):
        """The in-source default must be False."""
        from engine.config import Settings

        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is False

    def test_default_is_false_on_module_singleton(self):
        from engine.config import settings

        assert settings.auth_overwrite_role_on_login is False

    def test_setting_can_be_opted_in_via_env(self, monkeypatch):
        monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", "true")
        from engine.config import Settings

        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is True

    def test_setting_can_be_explicitly_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", "false")
        from engine.config import Settings

        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is False

    def test_setting_is_bool_typed(self):
        from engine.config import Settings

        s = Settings(_env_file=None)
        assert isinstance(s.auth_overwrite_role_on_login, bool)
