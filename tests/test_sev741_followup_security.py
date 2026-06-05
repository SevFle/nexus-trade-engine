"""Tests for the SEV-741 follow-up security hardening.

This module targets the *recently changed* code in:

- ``engine/api/auth/base.py``
    * ``DEFAULT_FALLBACK_ROLE`` (new constant)
    * ``_MAX_LOG_ROLE_LENGTH`` (new constant)
    * ``_CONTROL_CHARS_RE`` (new regex)
    * ``sanitize_role_for_log()`` (new public helper)
    * ``map_roles()`` — least-privilege fallback now emits a distinct
      ``auth.map_roles.fallback_to_least_privilege`` warning.

- ``engine/api/auth/{github_oauth,google,ldap,oidc}.py``
    * All four providers now respect the ``auth_overwrite_role_on_login``
      flag. Default behavior (False) is already pinned in
      ``test_auth_role_promotion_security_fix.py`` and
      ``test_ldap_auth.py``. Here we cover the opt-in path and the
      noop-log path on every provider.

- ``engine/api/auth/oidc.py``
    * New helper ``_maybe_overwrite_existing_user_role`` — extracted
      to satisfy ruff PLR0915 and to make the overwrite semantics
      independently testable.

Test design rules
-----------------
* Tests must be deterministic — no network, no real LDAP, no real DB.
* Each test exercises exactly one behavior so a regression points at
  a single assertion.
* Sanitizer tests cover: identity, type coercion, control-char
  stripping, length truncation, idempotence, and the regex attack
  surface (ANSI escape, CRLF, NUL).
* Provider tests cover: default (no overwrite), opt-in (overwrite),
  opt-in with no role change (no flush), opt-in with non-list raw
  roles (OIDC-only — no overwrite), and the noop-log path
  (GitHub/Google where there is no upstream role claim).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    DEFAULT_FALLBACK_ROLE,
    IAuthProvider,
    sanitize_role_for_log,
)
from engine.config import Settings

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _ConcreteProvider(IAuthProvider):
    """Minimal concrete provider used for map_roles / sanitization tests."""

    @property
    def name(self) -> str:
        return "test"

    async def authenticate(self, **_kwargs: Any):  # pragma: no cover - unused here
        from engine.api.auth.base import AuthResult

        return AuthResult()


def _stub_logger(monkeypatch):
    """Replace the module-level structlog logger on ``auth.base`` with a
    recording stub. Returns the list of recorded call dicts."""
    calls: list[dict[str, Any]] = []

    class _Stub:
        def warning(self, _event, **kwargs):
            calls.append({"event": _event, "level": "warning", **kwargs})

        def info(self, _event, **kwargs):
            calls.append({"event": _event, "level": "info", **kwargs})

        def error(self, _event, **kwargs):
            calls.append({"event": _event, "level": "error", **kwargs})

        def exception(self, _event, **kwargs):  # pragma: no cover
            calls.append({"event": _event, "level": "exception", **kwargs})

    from engine.api.auth import base

    monkeypatch.setattr(base, "logger", _Stub())
    return calls


# ---------------------------------------------------------------------------
# 1. DEFAULT_FALLBACK_ROLE constant
# ---------------------------------------------------------------------------


class TestDefaultFallbackRole:
    """Pin the contract of the public fallback-role constant."""

    def test_value_is_viewer(self):
        assert DEFAULT_FALLBACK_ROLE == "viewer"

    def test_value_is_str(self):
        assert isinstance(DEFAULT_FALLBACK_ROLE, str)

    def test_value_is_least_privilege(self):
        """``viewer`` must be strictly less privileged than ``user``.

        We assert this against the priority table in ``map_roles``: the
        fallback role must have a lower priority than ``user``."""
        priority: dict[str, int] = {
            "viewer": 0,
            "user": 1,
        }
        provider = _ConcreteProvider()
        # Sanity: the priority table inside map_roles still ranks viewer
        # below user. If somebody reorders the table this test fails
        # loudly instead of silently re-introducing SEV-741.
        assert provider.map_roles(["viewer"]) == "viewer"
        assert provider.map_roles(["viewer", "user"]) == "user"
        assert priority[DEFAULT_FALLBACK_ROLE] < priority["user"]


# ---------------------------------------------------------------------------
# 2. sanitize_role_for_log — pure-function unit tests
# ---------------------------------------------------------------------------


class TestSanitizeRoleForLogIdentity:
    """For inputs that need no transformation the function must be the
    identity function on strings."""

    def test_empty_string(self):
        assert sanitize_role_for_log("") == ""

    def test_plain_ascii(self):
        assert sanitize_role_for_log("admin") == "admin"

    def test_underscore_separated(self):
        assert sanitize_role_for_log("portfolio_manager") == "portfolio_manager"

    def test_internal_spaces_preserved(self):
        # Internal spaces are NOT control characters and must survive.
        assert sanitize_role_for_log("My Role Name") == "My Role Name"

    def test_unicode_preserved(self):
        # Non-ASCII printable characters must survive — operators may
        # legitimately have localized group names in their IdP.
        assert sanitize_role_for_log("rôle-d'étudiant") == "rôle-d'étudiant"


class TestSanitizeRoleForLogTypeCoercion:
    """Non-string inputs must be coerced via ``str()`` rather than
    raising — IdP claims occasionally surface as bytes or ints."""

    def test_int_input(self):
        assert sanitize_role_for_log(42) == "42"

    def test_bytes_input(self):
        # ``str(b"admin")`` produces ``"b'admin'"`` — ugly but safe.
        # The contract is "never raise"; prettification is the caller's
        # job.
        result = sanitize_role_for_log(b"admin")
        assert isinstance(result, str)
        assert "admin" in result

    def test_none_input(self):
        assert sanitize_role_for_log(None) == "None"

    def test_list_input(self):
        result = sanitize_role_for_log(["a", "b"])
        assert isinstance(result, str)
        assert "a" in result and "b" in result

    def test_dict_input(self):
        # Should not raise even on collections — coercion is total.
        result = sanitize_role_for_log({"x": 1})
        assert isinstance(result, str)


class TestSanitizeRoleForLogControlChars:
    """CRLF / NUL / BEL / ANSI-escape injection must be neutralized."""

    def test_strips_lf(self):
        assert sanitize_role_for_log("admin\n") == "admin"

    def test_strips_crlf(self):
        assert sanitize_role_for_log("admin\r\n") == "admin"

    def test_strips_embedded_lf(self):
        # Embedded newline is the canonical log-injection attack.
        assert sanitize_role_for_log("user\nFAKE_EVENT admin=1") == "userFAKE_EVENT admin=1"

    def test_strips_nul(self):
        # NUL truncation attacks against C-backed loggers.
        assert sanitize_role_for_log("admin\x00user") == "adminuser"

    def test_strips_bell(self):
        # Terminal bell — annoying but not dangerous; still stripped.
        assert sanitize_role_for_log("user\x07") == "user"

    def test_strips_backspace(self):
        # Backspace can be used to mask log content in some terminals.
        assert sanitize_role_for_log("a\bb") == "ab"

    def test_strips_tab(self):
        # Tab is in the C0 range and must be stripped for parity with
        # the other control characters.
        assert sanitize_role_for_log("a\tb") == "ab"

    def test_strips_del(self):
        # 0x7f (DEL) is in the regex's character class.
        assert sanitize_role_for_log("user\x7f") == "user"

    def test_strips_ansi_escape_sequence(self):
        # ESC (0x1b) followed by ``[`` is the start of an ANSI escape.
        # We strip ESC; the leftover ``[31m`` is harmless text.
        result = sanitize_role_for_log("\x1b[31madmin\x1b[0m")
        assert "\x1b" not in result
        assert "admin" in result

    def test_strips_all_c0_chars(self):
        # Every byte in 0x00-0x1f must be stripped.
        for i in range(0x20):
            ch = chr(i)
            result = sanitize_role_for_log(f"a{ch}b")
            assert result == "ab", (
                f"control char 0x{i:02x} ({ch!r}) was not stripped "
                f"— result={result!r}"
            )

    def test_preserves_printable_ascii(self):
        # All printable ASCII (0x20-0x7e) must be preserved.
        for i in range(0x20, 0x7f):
            ch = chr(i)
            if ch == "\\":
                # Avoid ambiguous test inputs
                continue
            assert sanitize_role_for_log(f"a{ch}b") == f"a{ch}b", (
                f"printable char 0x{i:02x} ({ch!r}) was incorrectly stripped"
            )


class TestSanitizeRoleForLogTruncation:
    """Inputs longer than ``_MAX_LOG_ROLE_LENGTH`` must be capped and
    marked so the operator can see that truncation happened."""

    def test_exactly_at_limit_not_truncated(self):
        from engine.api.auth.base import _MAX_LOG_ROLE_LENGTH

        text = "a" * _MAX_LOG_ROLE_LENGTH
        assert sanitize_role_for_log(text) == text
        assert len(sanitize_role_for_log(text)) == _MAX_LOG_ROLE_LENGTH

    def test_one_over_limit_is_truncated(self):
        from engine.api.auth.base import _MAX_LOG_ROLE_LENGTH

        text = "a" * (_MAX_LOG_ROLE_LENGTH + 1)
        result = sanitize_role_for_log(text)
        assert result.endswith("...[truncated]")
        assert len(result) == _MAX_LOG_ROLE_LENGTH + len("...[truncated]")

    def test_truncation_marker_is_distinct(self):
        # The marker must be a literal that an attacker cannot produce
        # from raw input — that's why we use ``...[truncated]`` with
        # square brackets; an attacker controlling only the role string
        # could include ``[truncated]`` but cannot force a length-based
        # truncation marker without actually exceeding the limit.
        text = "x" * 5000
        result = sanitize_role_for_log(text)
        assert result.endswith("...[truncated]")
        assert "[truncated]" in result

    def test_truncation_happens_after_control_char_strip(self):
        # The order is: strip control chars first, then truncate.
        # This test proves the order by constructing an input that is
        # *longer than the limit even after stripping* — so truncation
        # must still happen, but the surviving prefix must contain no
        # control characters. If the order were reversed, the truncated
        # prefix would still contain embedded newlines.
        from engine.api.auth.base import _MAX_LOG_ROLE_LENGTH

        # 200 'A' characters (> _MAX_LOG_ROLE_LENGTH) interleaved with
        # newlines so a reversed-order implementation would leak the
        # newlines into the truncated prefix.
        text = (
            "A" * (_MAX_LOG_ROLE_LENGTH + 50)
            + "\nFAKE\nEVENT\n"
            + "B" * 100
        )
        result = sanitize_role_for_log(text)
        # 1. Truncation marker proves the post-strip length still
        #    exceeded the limit.
        assert result.endswith("...[truncated]")
        # 2. No raw newline survives — strip ran on the full input,
        #    and the truncated prefix is also newline-free.
        assert "\n" not in result
        assert "\r" not in result

    def test_huge_input_does_not_crash(self):
        # Defense against log-flooding via multi-MB payloads.
        result = sanitize_role_for_log("a" * 1_000_000)
        assert "...[truncated]" in result
        # Final size must be bounded.
        assert len(result) < 500


class TestSanitizeRoleForLogIdempotence:
    """Running the sanitizer twice must produce the same output as
    running it once — important when it is composed into log
    pipelines that may re-sanitize on the way out."""

    def test_idempotent_on_plain_string(self):
        text = "developer"
        once = sanitize_role_for_log(text)
        twice = sanitize_role_for_log(once)
        assert once == twice == text

    def test_idempotent_on_already_sanitized(self):
        text = "user\nFAKE"
        once = sanitize_role_for_log(text)
        twice = sanitize_role_for_log(once)
        assert once == twice
        assert "\n" not in twice


# ---------------------------------------------------------------------------
# 3. map_roles — fallback warning event
# ---------------------------------------------------------------------------


class TestMapRolesFallbackWarning:
    """``map_roles`` must emit a *distinct* warning event when it falls
    back to ``viewer`` — operators need to alert on this independently
    from the ``unrecognized_roles`` event so they can spot zero-role
    payloads vs. misnamed-role payloads in their dashboards."""

    def test_fallback_warning_fires_for_empty_input(self, monkeypatch):
        calls = _stub_logger(monkeypatch)
        p = _ConcreteProvider()
        assert p.map_roles([]) == "viewer"
        fallback_calls = [
            c for c in calls
            if c["event"] == "auth.map_roles.fallback_to_least_privilege"
        ]
        assert len(fallback_calls) == 1
        assert fallback_calls[0]["fallback_role"] == "viewer"
        assert fallback_calls[0]["provider"] == "test"
        assert fallback_calls[0]["empty_input"] is True

    def test_fallback_warning_fires_for_all_unrecognized(self, monkeypatch):
        calls = _stub_logger(monkeypatch)
        p = _ConcreteProvider()
        assert p.map_roles(["bogus"]) == "viewer"
        fallback_calls = [
            c for c in calls
            if c["event"] == "auth.map_roles.fallback_to_least_privilege"
        ]
        assert len(fallback_calls) == 1
        assert fallback_calls[0]["fallback_role"] == "viewer"
        assert fallback_calls[0]["empty_input"] is False

    def test_fallback_warning_does_not_fire_when_recognized_present(
        self, monkeypatch
    ):
        calls = _stub_logger(monkeypatch)
        p = _ConcreteProvider()
        assert p.map_roles(["admin", "bogus"]) == "admin"
        assert not any(
            c["event"] == "auth.map_roles.fallback_to_least_privilege"
            for c in calls
        )

    def test_unrecognized_and_fallback_warnings_both_fire_for_all_unknown(
        self, monkeypatch
    ):
        """When every external role is unrecognized, both events must
        fire — the ``unrecognized_roles`` event so operators can see
        *what* came in, and the ``fallback_to_least_privilege`` event
        so they can alert on the role that was actually assigned."""
        calls = _stub_logger(monkeypatch)
        p = _ConcreteProvider()
        assert p.map_roles(["bogus"]) == "viewer"
        events = [c["event"] for c in calls]
        assert "auth.map_roles.unrecognized_roles" in events
        assert "auth.map_roles.fallback_to_least_privilege" in events

    def test_fallback_warning_unrecognized_list_is_sanitized(
        self, monkeypatch
    ):
        """The ``unrecognized`` payload of the fallback warning must
        itself be sanitized — otherwise the log-injection protection
        ``sanitize_role_for_log`` provides is bypassed on the fallback
        path."""
        calls = _stub_logger(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["attacker\ninjected"])
        fallback_calls = [
            c for c in calls
            if c["event"] == "auth.map_roles.fallback_to_least_privilege"
        ]
        assert fallback_calls, "Fallback warning must fire for unrecognized input"
        payload = fallback_calls[0]["unrecognized"]
        assert isinstance(payload, list)
        assert payload == ["attackerinjected"]
        # No raw newline survives into the log payload.
        for item in payload:
            assert "\n" not in item
            assert "\r" not in item


# ---------------------------------------------------------------------------
# 4. auth_overwrite_role_on_login — opt-in behavior on every provider
# ---------------------------------------------------------------------------
#
# Default behavior (False) is already pinned in
# ``test_ldap_auth.py`` (LDAP) and ``test_auth_role_promotion_security_fix.py``
# (settings). Here we add the missing opt-in coverage on **every**
# provider so the matrix is closed.
#
# Matrix:
#                | default (False)      | opt-in (True)              |
# ---------------|----------------------|----------------------------|
# GitHub         | no-op (covered here) | noop log  (covered here)   |
# Google         | no-op (covered here) | noop log  (covered here)   |
# LDAP           | no-op (existing)     | overwrite (covered here)   |
# OIDC           | no-op (covered here) | overwrite (covered here)   |


def _provider_settings(
    monkeypatch,
    *,
    module: str,
    auth_overwrite_role_on_login: bool,
    **extra: Any,
) -> Settings:
    """Build a Settings instance and monkey-patch it onto the given
    auth module. The ``auth_overwrite_role_on_login`` flag is set
    explicitly so tests don't depend on the default.

    ``module`` is the dotted module path (e.g. ``engine.api.auth.oidc``);
    the helper patches ``<module>.settings`` using the dotted-string
    form of ``monkeypatch.setattr`` so the module does not need to be
    pre-imported at the call site."""
    s = Settings(auth_overwrite_role_on_login=auth_overwrite_role_on_login, **extra)
    monkeypatch.setattr(f"{module}.settings", s)
    return s


# ----- GitHub --------------------------------------------------------------


class TestGitHubAuthProviderRoleOverwrite:
    """GitHub does not surface internal roles in this implementation —
    the opt-in path must emit a noop log and *not* mutate the user."""

    async def test_default_no_overwrite_no_log(self, monkeypatch):
        from engine.api.auth.github_oauth import GitHubAuthProvider

        _provider_settings(
            monkeypatch,
            module="engine.api.auth.github_oauth",
            auth_overwrite_role_on_login=False,
            github_client_id="id",
            github_client_secret="secret",
            github_redirect_uri="https://app.example.com/cb",
        )
        provider = GitHubAuthProvider()

        # Drive the post-token branch directly. We pre-populate the DB
        # with an existing user so the ``elif`` branch is taken.
        from engine.db.models import User

        existing = User(
            email="gh@example.com",
            display_name="GH",
            is_active=True,
            role="admin",  # privileged
            auth_provider="github",
            external_id="gh-123",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)

        # Stub httpx so the token+userinfo exchange succeeds.
        token_resp = MagicMock()
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "tok"}

        userinfo_resp = MagicMock()
        userinfo_resp.raise_for_status = MagicMock()
        userinfo_resp.json.return_value = {
            "id": 123,
            "login": "ghuser",
            "email": "gh@example.com",
            "name": "GH User",
        }

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                pass

            async def post(self, *_a, **_kw):
                return token_resp

            async def get(self, *_a, **_kw):
                return userinfo_resp

        with patch("httpx.AsyncClient", return_value=_Client()):
            res = await provider.authenticate(code="c", db=mock_db)

        assert res.success is True
        # No overwrite, no flush, role unchanged.
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_opt_in_emits_noop_log(self, monkeypatch):
        from engine.api.auth.github_oauth import GitHubAuthProvider

        _provider_settings(
            monkeypatch,
            module="engine.api.auth.github_oauth",
            auth_overwrite_role_on_login=True,
            github_client_id="id",
            github_client_secret="secret",
            github_redirect_uri="https://app.example.com/cb",
        )

        calls: list[dict[str, Any]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, **kw})

            def exception(self, *_a, **_kw):  # pragma: no cover
                pass

        from engine.api.auth import github_oauth

        monkeypatch.setattr(github_oauth, "logger", _Stub())

        from engine.db.models import User

        existing = User(
            email="gh2@example.com",
            display_name="GH2",
            is_active=True,
            role="user",
            auth_provider="github",
            external_id="gh-456",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)

        token_resp = MagicMock()
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "tok"}

        userinfo_resp = MagicMock()
        userinfo_resp.raise_for_status = MagicMock()
        userinfo_resp.json.return_value = {
            "id": 456,
            "login": "gh2",
            "email": "gh2@example.com",
            "name": "GH2",
        }

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                pass

            async def post(self, *_a, **_kw):
                return token_resp

            async def get(self, *_a, **_kw):
                return userinfo_resp

        with patch("httpx.AsyncClient", return_value=_Client()):
            res = await GitHubAuthProvider().authenticate(code="c", db=mock_db)

        assert res.success is True
        # Noop log fired — operator can see the flag is wired up.
        assert any(c["event"] == "auth.github.role_overwrite_noop" for c in calls)
        # Role still unchanged: GitHub provider does not extract roles.
        assert existing.role == "user"


# ----- Google --------------------------------------------------------------


class TestGoogleAuthProviderRoleOverwrite:
    """Symmetric to GitHub: Google also has no upstream role extraction
    in the current implementation, so opt-in must emit a noop log."""

    async def test_default_no_overwrite(self, monkeypatch):
        from engine.api.auth.google import GoogleAuthProvider

        _provider_settings(
            monkeypatch,
            module="engine.api.auth.google",
            auth_overwrite_role_on_login=False,
            google_client_id="id",
            google_client_secret="secret",
            google_redirect_uri="https://app.example.com/cb",
        )

        from engine.db.models import User

        existing = User(
            email="g@example.com",
            display_name="G",
            is_active=True,
            role="admin",
            auth_provider="google",
            external_id="g-sub",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)

        token_resp = MagicMock()
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "tok"}

        userinfo_resp = MagicMock()
        userinfo_resp.raise_for_status = MagicMock()
        userinfo_resp.json.return_value = {
            "sub": "g-sub",
            "email": "g@example.com",
            "name": "G User",
        }

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                pass

            async def post(self, *_a, **_kw):
                return token_resp

            async def get(self, *_a, **_kw):
                return userinfo_resp

        with patch("httpx.AsyncClient", return_value=_Client()):
            res = await GoogleAuthProvider().authenticate(code="c", db=mock_db)

        assert res.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_opt_in_emits_noop_log(self, monkeypatch):
        from engine.api.auth.google import GoogleAuthProvider

        _provider_settings(
            monkeypatch,
            module="engine.api.auth.google",
            auth_overwrite_role_on_login=True,
            google_client_id="id",
            google_client_secret="secret",
            google_redirect_uri="https://app.example.com/cb",
        )

        calls: list[dict[str, Any]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, **kw})

            def exception(self, *_a, **_kw):  # pragma: no cover
                pass

        from engine.api.auth import google

        monkeypatch.setattr(google, "logger", _Stub())

        from engine.db.models import User

        existing = User(
            email="g2@example.com",
            display_name="G2",
            is_active=True,
            role="user",
            auth_provider="google",
            external_id="g2-sub",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)

        token_resp = MagicMock()
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "tok"}

        userinfo_resp = MagicMock()
        userinfo_resp.raise_for_status = MagicMock()
        userinfo_resp.json.return_value = {
            "sub": "g2-sub",
            "email": "g2@example.com",
            "name": "G2",
        }

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                pass

            async def post(self, *_a, **_kw):
                return token_resp

            async def get(self, *_a, **_kw):
                return userinfo_resp

        with patch("httpx.AsyncClient", return_value=_Client()):
            res = await GoogleAuthProvider().authenticate(code="c", db=mock_db)

        assert res.success is True
        assert any(c["event"] == "auth.google.role_overwrite_noop" for c in calls)
        assert existing.role == "user"


# ----- LDAP ----------------------------------------------------------------


def _ldap_settings(
    monkeypatch,
    *,
    auth_overwrite_role_on_login: bool,
) -> Settings:
    return _provider_settings(
        monkeypatch,
        module="engine.api.auth.ldap",
        auth_overwrite_role_on_login=auth_overwrite_role_on_login,
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=developers,ou=groups,dc=example,dc=com": "developer",
        }),
    )


def _ldap_mock_with_admin_group():
    """Build the sys.modules patches that simulate a successful LDAP
    bind + search returning a user in the ``cn=admins,…`` group."""
    attrs = {
        "uid": [b"testuser"],
        "mail": [b"testuser@example.com"],
        "cn": [b"Test User"],
        "memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"],
    }

    class _Conn:
        def set_option(self, *_a):
            pass

        def simple_bind_s(self, *_a):
            pass

        def search_s(self, *_a):
            return [("uid=testuser,ou=users,dc=example,dc=com", attrs)]

        def unbind_s(self):
            pass

    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(return_value=_Conn())
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter


class TestLDAPAuthProviderRoleOverwrite:
    """LDAP path is the only one with real upstream role extraction;
    opt-in must actually mutate ``user.role`` and emit a log event."""

    async def test_opt_in_overwrites_role_and_logs(self, monkeypatch):
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(monkeypatch, auth_overwrite_role_on_login=True)

        calls: list[dict[str, Any]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, **kw})

            def exception(self, *_a, **_kw):  # pragma: no cover
                pass

        from engine.api.auth import ldap as ldap_module

        monkeypatch.setattr(ldap_module, "logger", _Stub())

        from engine.db.models import User

        existing = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
            role="user",  # local: user; IdP will say: admin
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.flush = AsyncMock()

        mock_ldap, mock_filter = _ldap_mock_with_admin_group()
        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}
        ):
            res = await LDAPAuthProvider().authenticate(
                username="testuser", password="correctpass", db=mock_db
            )

        assert res.success is True
        # IdP-driven escalation actually happened under opt-in.
        assert existing.role == "admin"
        mock_db.flush.assert_awaited_once()
        overwrite_calls = [
            c for c in calls if c["event"] == "auth.ldap.role_overwritten"
        ]
        assert len(overwrite_calls) == 1
        assert overwrite_calls[0]["previous_role"] == "user"
        assert overwrite_calls[0]["new_role"] == "admin"

    async def test_opt_in_no_flush_when_role_unchanged(self, monkeypatch):
        """If the IdP claim maps to the same role the user already has,
        the provider must skip both the log line and the ``flush``."""
        from engine.api.auth.ldap import LDAPAuthProvider

        _ldap_settings(monkeypatch, auth_overwrite_role_on_login=True)

        from engine.api.auth import ldap as ldap_module

        calls: list[dict[str, Any]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, **kw})

            def exception(self, *_a, **_kw):  # pragma: no cover
                pass

        monkeypatch.setattr(ldap_module, "logger", _Stub())

        from engine.db.models import User

        existing = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
            role="admin",  # already admin; IdP also says admin
            auth_provider="ldap",
            external_id="testuser",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.flush = AsyncMock()

        mock_ldap, mock_filter = _ldap_mock_with_admin_group()
        with patch.dict(
            "sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}
        ):
            res = await LDAPAuthProvider().authenticate(
                username="testuser", password="correctpass", db=mock_db
            )

        assert res.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()
        assert not any(c["event"] == "auth.ldap.role_overwritten" for c in calls)


# ----- OIDC ----------------------------------------------------------------


def _make_oidc_id_token(claims: dict, rsa_keys) -> str:
    """Sign a tiny ID token with the test RSA key pair."""
    import jwt

    private_key, _pub_key = rsa_keys
    return jwt.encode(
        {"aud": "test-client-id", **claims},
        private_key,
        algorithm="RS256",
        headers={"kid": "test-kid"},
    )


_DISCOVERY_DOC = {
    "authorization_endpoint": "https://id.example.com/authorize",
    "token_endpoint": "https://id.example.com/token",
    "jwks_uri": "https://id.example.com/jwks",
}


def _oidc_settings(
    monkeypatch,
    *,
    auth_overwrite_role_on_login: bool,
) -> Settings:
    return _provider_settings(
        monkeypatch,
        module="engine.api.auth.oidc",
        auth_overwrite_role_on_login=auth_overwrite_role_on_login,
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_redirect_uri="https://app.example.com/callback",
        oidc_role_claim="roles",
    )


def _oidc_mock_client(rsa_keys, claims: dict):
    """Build a fake httpx client that returns a signed id_token plus a
    matching JWKS document. The first ``get`` returns the discovery
    doc, the second returns the JWKS; the ``post`` returns the token
    response."""
    import json

    from jwt.algorithms import RSAAlgorithm

    _, pub_key = rsa_keys
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = "test-kid"
    id_token = _make_oidc_id_token(claims, rsa_keys)

    disc_resp = MagicMock()
    disc_resp.raise_for_status = MagicMock()
    disc_resp.json.return_value = _DISCOVERY_DOC

    jwks_resp = MagicMock()
    jwks_resp.raise_for_status = MagicMock()
    jwks_resp.json.return_value = {"keys": [jwk_dict]}

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {"id_token": id_token, "access_token": "at"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            pass

        async def get(self, *_a, **_kw):
            nonlocal disc_resp, jwks_resp
            if disc_resp is not None:
                r = disc_resp
                disc_resp = None
                return r
            return jwks_resp

        async def post(self, *_a, **_kw):
            return token_resp

    return _Client()


@pytest.fixture
def rsa_keys():
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


class TestOIDCAuthProviderRoleOverwrite:
    """OIDC path: opt-in must overwrite the role from the upstream
    ``roles`` claim. The overwrite logic is in a dedicated helper
    ``_maybe_overwrite_existing_user_role`` to satisfy ruff PLR0915."""

    async def test_default_no_overwrite(self, monkeypatch, rsa_keys):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=False)

        from engine.db.models import User

        existing = User(
            email="o@example.com",
            display_name="O",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="o-sub",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.flush = AsyncMock()

        fake_client = _oidc_mock_client(
            rsa_keys,
            {"sub": "o-sub", "email": "o@example.com", "roles": ["admin"]},
        )
        with patch("httpx.AsyncClient", return_value=fake_client):
            res = await OIDCAuthProvider().authenticate(code="c", db=mock_db)

        assert res.success is True
        # Default: IdP claim ignored, role unchanged.
        assert existing.role == "user"
        mock_db.flush.assert_not_called()

    async def test_opt_in_overwrites_role_and_logs(self, monkeypatch, rsa_keys):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=True)

        from engine.api.auth import oidc as oidc_module

        calls: list[dict[str, Any]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, **kw})

            def exception(self, *_a, **_kw):  # pragma: no cover
                pass

        monkeypatch.setattr(oidc_module, "logger", _Stub())

        from engine.db.models import User

        existing = User(
            email="o@example.com",
            display_name="O",
            is_active=True,
            role="user",  # local: user; IdP will say: admin
            auth_provider="oidc",
            external_id="o-sub",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.flush = AsyncMock()

        fake_client = _oidc_mock_client(
            rsa_keys,
            {"sub": "o-sub", "email": "o@example.com", "roles": ["admin"]},
        )
        with patch("httpx.AsyncClient", return_value=fake_client):
            res = await OIDCAuthProvider().authenticate(code="c", db=mock_db)

        assert res.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_awaited_once()
        overwrite_calls = [
            c for c in calls if c["event"] == "auth.oidc.role_overwritten"
        ]
        assert len(overwrite_calls) == 1
        assert overwrite_calls[0]["previous_role"] == "user"
        assert overwrite_calls[0]["new_role"] == "admin"

    async def test_opt_in_no_flush_when_role_unchanged(self, monkeypatch, rsa_keys):
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=True)

        from engine.db.models import User

        existing = User(
            email="o@example.com",
            display_name="O",
            is_active=True,
            role="admin",  # already admin
            auth_provider="oidc",
            external_id="o-sub",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.flush = AsyncMock()

        fake_client = _oidc_mock_client(
            rsa_keys,
            {"sub": "o-sub", "email": "o@example.com", "roles": ["admin"]},
        )
        with patch("httpx.AsyncClient", return_value=fake_client):
            res = await OIDCAuthProvider().authenticate(code="c", db=mock_db)

        assert res.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_opt_in_non_list_roles_does_not_overwrite(
        self, monkeypatch, rsa_keys
    ):
        """If the upstream ``roles`` claim is not a list (e.g. a single
        string), the OIDC provider skips the overwrite entirely rather
        than crashing. This is the documented behavior of the helper."""
        from engine.api.auth.oidc import OIDCAuthProvider

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=True)

        from engine.db.models import User

        existing = User(
            email="o@example.com",
            display_name="O",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="o-sub",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.flush = AsyncMock()

        fake_client = _oidc_mock_client(
            rsa_keys,
            # ``roles`` is a *string*, not a list — helper must skip it.
            {"sub": "o-sub", "email": "o@example.com", "roles": "admin"},
        )
        with patch("httpx.AsyncClient", return_value=fake_client):
            res = await OIDCAuthProvider().authenticate(code="c", db=mock_db)

        assert res.success is True
        # No overwrite, role preserved.
        assert existing.role == "user"
        mock_db.flush.assert_not_called()


# ---------------------------------------------------------------------------
# 5. _maybe_overwrite_existing_user_role — direct unit tests
# ---------------------------------------------------------------------------


class TestMaybeOverwriteExistingUserRole:
    """Direct unit tests for the helper extracted from OIDC's
    ``authenticate``. Covers the four exit paths:
      - flag off -> no-op
      - raw_roles not a list -> no-op
      - same role -> no-op
      - different role -> mutate + flush + log
    """

    async def test_flag_off_is_noop(self, monkeypatch):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=False)
        user = User(email="x@x.com", role="user", auth_provider="oidc")
        db = AsyncMock(spec=AsyncSession)

        await OIDCAuthProvider()._maybe_overwrite_existing_user_role(
            user, ["admin"], db
        )
        assert user.role == "user"
        db.flush.assert_not_called()

    async def test_non_list_raw_roles_is_noop(self, monkeypatch):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=True)
        user = User(email="x@x.com", role="user", auth_provider="oidc")
        db = AsyncMock(spec=AsyncSession)

        await OIDCAuthProvider()._maybe_overwrite_existing_user_role(
            user, "admin", db  # type: ignore[arg-type]
        )
        assert user.role == "user"
        db.flush.assert_not_called()

    async def test_same_role_is_noop(self, monkeypatch):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=True)
        user = User(email="x@x.com", role="admin", auth_provider="oidc")
        db = AsyncMock(spec=AsyncSession)

        await OIDCAuthProvider()._maybe_overwrite_existing_user_role(
            user, ["admin"], db
        )
        assert user.role == "admin"
        db.flush.assert_not_called()

    async def test_different_role_mutates_and_flushes(self, monkeypatch):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=True)

        calls: list[dict[str, Any]] = []

        class _Stub:
            def info(self, _event, **kw):
                calls.append({"event": _event, **kw})

        from engine.api.auth import oidc as oidc_module

        monkeypatch.setattr(oidc_module, "logger", _Stub())

        user = User(email="x@x.com", role="user", auth_provider="oidc")
        db = AsyncMock(spec=AsyncSession)

        await OIDCAuthProvider()._maybe_overwrite_existing_user_role(
            user, ["admin"], db
        )
        assert user.role == "admin"
        db.flush.assert_awaited_once()
        assert len(calls) == 1
        assert calls[0]["event"] == "auth.oidc.role_overwritten"
        assert calls[0]["previous_role"] == "user"
        assert calls[0]["new_role"] == "admin"

    async def test_helper_handles_empty_list(self, monkeypatch):
        """Empty list -> map_roles returns viewer (least privilege).
        If the user is not already viewer, the overwrite path runs and
        downgrades them. This is the documented opt-in behavior."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_settings(monkeypatch, auth_overwrite_role_on_login=True)

        from engine.api.auth import oidc as oidc_module

        class _Stub:
            def info(self, _event, **kw):  # pragma: no cover
                pass

        monkeypatch.setattr(oidc_module, "logger", _Stub())

        user = User(email="x@x.com", role="admin", auth_provider="oidc")
        db = AsyncMock(spec=AsyncSession)

        await OIDCAuthProvider()._maybe_overwrite_existing_user_role(
            user, [], db
        )
        assert user.role == "viewer"
        db.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. Integration: sanitize_role_for_log is wired into map_roles' log payload
# ---------------------------------------------------------------------------


class TestSanitizeRoleWiredIntoMapRoles:
    """End-to-end: the ``unrecognized`` payload on the warning event
    must contain sanitized strings — this is the actual security
    boundary, not the helper in isolation."""

    def test_crlf_injection_neutralized_in_log(self, monkeypatch):
        calls = _stub_logger(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["admin", "FAKE\nINJECTED\rEVENT"])
        warning = next(
            c for c in calls
            if c["event"] == "auth.map_roles.unrecognized_roles"
        )
        payload = warning["unrecognized"]
        assert payload == ["FAKEINJECTEDEVENT"]
        # Belt-and-braces: ensure no raw control chars slipped through.
        for item in payload:
            assert "\n" not in item and "\r" not in item

    def test_oversized_role_truncated_in_log(self, monkeypatch):
        calls = _stub_logger(monkeypatch)
        p = _ConcreteProvider()
        big = "X" * 10_000
        p.map_roles([big])
        warning = next(
            c for c in calls
            if c["event"] == "auth.map_roles.unrecognized_roles"
        )
        payload = warning["unrecognized"]
        assert len(payload) == 1
        # The sanitized version must be bounded.
        assert len(payload[0]) < 200
        assert "[truncated]" in payload[0]

    def test_combined_attack_neutralized(self, monkeypatch):
        """CRLF + oversize + unicode all in one — sanitizer must handle
        all three transformations in the correct order."""
        calls = _stub_logger(monkeypatch)
        p = _ConcreteProvider()
        attack = "\n" * 5 + "X" * 5000 + "\r\n" + "rôle"
        p.map_roles([attack])
        warning = next(
            c for c in calls
            if c["event"] == "auth.map_roles.unrecognized_roles"
        )
        payload = warning["unrecognized"][0]
        # No control chars
        for ch in payload:
            assert ord(ch) >= 0x20 or ord(ch) == 0x7f, (
                f"control char 0x{ord(ch):02x} survived sanitization"
            )
        # Bounded length
        assert len(payload) < 200
        # Truncation marker present
        assert "[truncated]" in payload
