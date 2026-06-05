"""Tests for the SEV-741 follow-up: explicit role-deny semantics.

Background
----------
Three changes shipped together to close the silent-escalation gap that
remained after SEV-741:

1. ``IAuthProvider.map_roles_detailed`` returns ``None`` (instead of
   silently falling back to ``"user"``) when no external role is
   recognized.  This forces callers to make an explicit policy decision
   about how to handle the case — "deny", "keep previous role", or
   "assign default" — rather than burying it in a magic value.

2. ``Settings.auth_overwrite_role_on_login`` is now wired into the
   actual login/role-mapping codepath of both federated providers
   (OIDC and LDAP).  Previously the OIDC provider *never* updated an
   existing user's role on re-login (silent no-op) and the LDAP
   provider *always* did (silent overwrite).  Both now respect the
   flag, which defaults to ``False`` (defense-in-depth).

3. ``engine.api.auth.base._validate_role_table()`` validates the
   ``ROLE_PRIORITY`` table at import time and raises
   :class:`RuntimeError` (not bare ``assert``) so the check survives
   ``python -O``.

This test module pins all three behaviours and adds re-login
integration coverage that proves toggling ``auth_overwrite_role_on_login``
changes persisted behaviour in both providers.
"""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import (
    ROLE_FLOOR,
    ROLE_PRIORITY,
    AuthResult,
    IAuthProvider,
    _validate_role_table,
)
from engine.config import Settings

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


class _ConcreteProvider(IAuthProvider):
    """Minimal concrete provider for unit tests."""

    @property
    def name(self) -> str:
        return "test-concrete"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult()


class _AnotherConcrete(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-other"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult()


def _patch_logger(monkeypatch):
    """Replace the module-level structlog logger in engine.api.auth.base
    with a stub that records every call.  Returns the calls list so
    tests can assert on event names and bound kwargs."""
    calls: list[dict[str, object]] = []

    class _Stub:
        def warning(self, _event, **kwargs):
            calls.append({"event": _event, **kwargs})

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
# 1. map_roles_detailed — explicit-deny semantics
# ===========================================================================


class TestMapRolesDetailedContract:
    """``map_roles_detailed`` is the new explicit-deny variant of
    ``map_roles``.  It returns ``None`` when no recognized role is
    found, so callers cannot accidentally accept a "default user" as
    if it were an authenticated identity assertion."""

    def test_method_exists_on_iprovider(self):
        """The new method is part of the public provider contract."""
        assert hasattr(IAuthProvider, "map_roles_detailed")
        assert callable(IAuthProvider.map_roles_detailed)

    @pytest.mark.parametrize(
        ("external", "expected"),
        [
            (["viewer"], "viewer"),
            (["user"], "user"),
            (["retail_trader"], "retail_trader"),
            (["quant_dev"], "quant_dev"),
            (["developer"], "developer"),
            (["portfolio_manager"], "portfolio_manager"),
            (["admin"], "admin"),
        ],
    )
    def test_single_recognized_role_returned_verbatim(self, external, expected):
        """Recognized roles come back exactly as canonicalized — no
        translation, no promotion."""
        assert _ConcreteProvider().map_roles_detailed(external) == expected

    def test_returns_none_for_empty_input(self):
        """Empty input must surface as ``None`` so the caller can
        distinguish "no role asserted" from "explicit user"."""
        assert _ConcreteProvider().map_roles_detailed([]) is None

    def test_returns_none_when_all_roles_unrecognized(self):
        """This is the central new guarantee: previously ``map_roles``
        silently returned ``"user"`` for this input."""
        assert (
            _ConcreteProvider().map_roles_detailed(["superuser", "root", "god"])
            is None
        )

    def test_returns_none_for_whitespace_only_role(self):
        """Whitespace normalizes to ``""``, which is unrecognized.
        Must surface as ``None`` (not silent fallback)."""
        assert _ConcreteProvider().map_roles_detailed(["   "]) is None

    def test_returns_recognized_role_when_partial_unrecognized(self):
        """Recognized + unrecognized mix → recognized wins, no fallback."""
        assert (
            _ConcreteProvider().map_roles_detailed(["developer", "l33t_h4x0r"])
            == "developer"
        )

    def test_returns_highest_priority_when_multiple_recognized(self):
        """The highest-priority *recognized* role wins (no promotion layer)."""
        assert (
            _ConcreteProvider().map_roles_detailed(
                ["viewer", "user", "quant_dev", "developer", "admin"]
            )
            == "admin"
        )

    def test_quant_dev_not_promoted_to_developer(self):
        """SEV-741 regression: previously promoted. ``map_roles_detailed``
        must faithfully reflect the input."""
        assert _ConcreteProvider().map_roles_detailed(["quant_dev"]) == "quant_dev"

    def test_viewer_not_promoted_to_user(self):
        """SEV-741 regression: previously promoted."""
        assert _ConcreteProvider().map_roles_detailed(["viewer"]) == "viewer"

    def test_case_insensitive_normalization(self):
        """Casing / whitespace are normalized before lookup."""
        assert _ConcreteProvider().map_roles_detailed(["ADMIN"]) == "admin"
        assert _ConcreteProvider().map_roles_detailed(["  Admin  "]) == "admin"
        assert _ConcreteProvider().map_roles_detailed(["QuAnT_dEv"]) == "quant_dev"

    def test_does_not_return_role_floor_on_deny(self):
        """Pin the contract: the *whole point* of map_roles_detailed is
        that ``ROLE_FLOOR`` is NOT returned when no recognized role is
        found.  If this test ever fails, somebody wired the convenience
        wrapper logic back into the explicit variant."""
        result = _ConcreteProvider().map_roles_detailed(["totally_bogus"])
        assert result is None
        assert result != ROLE_FLOOR

    def test_detailed_vs_convenience_disagree_on_deny(self):
        """The two methods must differ on the all-unrecognized case:
        ``map_roles`` returns ``ROLE_FLOOR``, ``map_roles_detailed``
        returns ``None``."""
        external = ["not_a_real_role"]
        assert _ConcreteProvider().map_roles(external) == ROLE_FLOOR
        assert _ConcreteProvider().map_roles_detailed(external) is None

    def test_detailed_and_convenience_agree_on_recognized(self):
        """For inputs that contain a recognized role, the two methods
        must agree — the wrapper must not introduce any extra
        translation."""
        external = ["viewer", "admin", "bogus"]
        assert (
            _ConcreteProvider().map_roles(external)
            == _ConcreteProvider().map_roles_detailed(external)
            == "admin"
        )

    def test_provider_name_appears_in_warning(self, monkeypatch):
        """The provider's ``name`` property is bound to the warning so
        operators can attribute misconfigurations."""
        calls = _patch_logger(monkeypatch)
        _AnotherConcrete().map_roles_detailed(["weird"])
        assert calls
        assert calls[0]["provider"] == "test-other"

    def test_warning_fires_for_pure_unrecognized_set(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        result = _ConcreteProvider().map_roles_detailed(["bogus"])
        assert result is None
        assert any(c["event"] == "auth.map_roles.unrecognized_roles" for c in calls)

    def test_warning_fires_for_partial_unrecognized_set(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        assert _ConcreteProvider().map_roles_detailed(["admin", "bogus"]) == "admin"
        assert any(c["event"] == "auth.map_roles.unrecognized_roles" for c in calls), (
            "Expected warning when ANY external role is unrecognized."
        )

    def test_warning_does_not_fire_when_all_recognized(self, monkeypatch):
        calls = _patch_logger(monkeypatch)
        assert _ConcreteProvider().map_roles_detailed(["user", "developer"]) == "developer"
        assert calls == []

    def test_warning_payload_includes_mapped_role(self, monkeypatch):
        """``mapped=`` reports what was returned (or 'user' for the
        all-unrecognized case so existing dashboards keep working)."""
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles_detailed(["viewer", "bogus"])
        assert calls[0]["mapped"] == "viewer"

    def test_warning_payload_reports_user_when_all_unrecognized(self, monkeypatch):
        """For backward-compat with operator dashboards, the warning's
        ``mapped=`` field reports 'user' even though the function
        itself returns ``None``."""
        calls = _patch_logger(monkeypatch)
        _ConcreteProvider().map_roles_detailed(["bogus"])
        assert calls[0]["mapped"] == "user"


# ===========================================================================
# 2. _validate_role_table — runtime validation surviving -O
# ===========================================================================


class TestValidateRoleTable:
    """``_validate_role_table`` replaces what would otherwise be bare
    module-level ``assert`` statements, so the invariants survive
    ``python -O``.  These tests exercise the function directly."""

    def test_default_table_validates_clean(self):
        """The shipping ROLE_PRIORITY table must validate without
        raising — this is the baseline assertion."""
        _validate_role_table()  # no exception

    def test_function_is_a_regular_function_not_an_assert(self):
        """Pin the implementation: must be a def'd function, not a
        bare ``assert`` (which would be stripped by -O)."""
        assert inspect.isfunction(_validate_role_table)

    def test_no_bare_module_level_assert_in_base(self):
        """Source-level guard: the base module must not contain bare
        module-level ``assert`` statements (those are stripped by -O
        and would defeat the point of the validator)."""
        from engine.api.auth import base

        src = inspect.getsource(base)
        for line in src.splitlines():
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if stripped.startswith("assert ") and indent == 0:
                pytest.fail(
                    f"Found bare module-level assert in engine/api/auth/base.py "
                    f"(stripped by -O): {line!r}"
                )

    def test_runtime_error_on_non_dict(self, monkeypatch):
        from engine.api.auth import base

        monkeypatch.setattr(base, "ROLE_PRIORITY", ["viewer", "user"])
        with pytest.raises(RuntimeError, match="must be a dict"):
            base._validate_role_table()

    def test_runtime_error_on_empty_dict(self, monkeypatch):
        from engine.api.auth import base

        monkeypatch.setattr(base, "ROLE_PRIORITY", {})
        with pytest.raises(RuntimeError, match="must not be empty"):
            base._validate_role_table()

    def test_runtime_error_on_missing_required_role(self, monkeypatch):
        from engine.api.auth import base

        broken = {
            "viewer": 0,
            "user": 1,
            "retail_trader": 2,
            "quant_dev": 3,
            # "developer" intentionally missing
            "portfolio_manager": 4,
            "admin": 5,
        }
        monkeypatch.setattr(base, "ROLE_PRIORITY", broken)
        with pytest.raises(RuntimeError, match="missing required roles"):
            base._validate_role_table()

    def test_runtime_error_on_non_contiguous_priorities(self, monkeypatch):
        from engine.api.auth import base

        broken = {
            "viewer": 0,
            "user": 1,
            "retail_trader": 2,
            "quant_dev": 3,
            "developer": 4,
            "portfolio_manager": 5,
            "admin": 7,  # gap: skips 6
        }
        monkeypatch.setattr(base, "ROLE_PRIORITY", broken)
        with pytest.raises(RuntimeError, match="contiguous integers"):
            base._validate_role_table()

    def test_runtime_error_on_duplicate_priorities(self, monkeypatch):
        """Duplicate priorities also fail the contiguity check (since
        ``[0, 0, 1, ...]`` cannot equal ``[0, 1, 2, ...]``). The error
        message must mention contiguity — operators need a single,
        unambiguous diagnostic for any malformed priority set."""
        from engine.api.auth import base

        broken = {
            "viewer": 0,
            "user": 0,  # duplicate of viewer
            "retail_trader": 1,
            "quant_dev": 2,
            "developer": 3,
            "portfolio_manager": 4,
            "admin": 5,
        }
        monkeypatch.setattr(base, "ROLE_PRIORITY", broken)
        with pytest.raises(RuntimeError, match="contiguous integers"):
            base._validate_role_table()

    def test_runtime_error_does_not_use_assert(self):
        """Pin that the function raises RuntimeError — not AssertionError.
        AssertionError is what bare ``assert`` raises, and would still
        be stripped by -O."""
        from engine.api.auth import base

        # We can't actually run with -O in-process, but we can verify
        # that the function explicitly raises RuntimeError (which -O
        # does NOT strip).
        src = inspect.getsource(base._validate_role_table)
        assert "raise RuntimeError" in src
        # And that the function does NOT use bare ``assert`` for the
        # validation logic.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("assert "):
                pytest.fail(
                    f"_validate_role_table must not use bare assert "
                    f"(stripped by -O): {line!r}"
                )

    def test_validator_runs_at_import_time(self):
        """Importing the module must trigger validation.  We can't
        re-import in-process reliably, but we can confirm the call
        appears at module scope (not inside an ``if`` or function)."""
        from engine.api.auth import base

        src = inspect.getsource(base)
        # The call must appear at column 0 (module-level).
        assert any(
            line == "_validate_role_table()" for line in src.splitlines()
        ), "Expected module-level call to _validate_role_table()"


# ===========================================================================
# 3. ROLE_PRIORITY module-level constant
# ===========================================================================


class TestRolePriorityConstant:
    """The role priority table is now a module-level constant (single
    source of truth).  Tests pin the public surface so refactors
    don't accidentally drop roles or change priorities."""

    def test_role_priority_is_dict(self):
        assert isinstance(ROLE_PRIORITY, dict)

    def test_role_priority_contains_all_roles(self):
        expected = {
            "viewer", "user", "retail_trader", "quant_dev",
            "developer", "portfolio_manager", "admin",
        }
        assert set(ROLE_PRIORITY.keys()) == expected

    def test_role_priority_starts_at_zero(self):
        assert min(ROLE_PRIORITY.values()) == 0

    def test_role_priority_is_contiguous(self):
        priorities = sorted(ROLE_PRIORITY.values())
        assert priorities == list(range(len(ROLE_PRIORITY)))

    def test_role_floor_is_user(self):
        """The convenience fallback for ``map_roles`` is "user"."""
        assert ROLE_FLOOR == "user"

    def test_role_floor_is_in_role_priority(self):
        """The floor must be a valid role — otherwise the fallback
        would produce a role that ``require_role`` cannot rank."""
        assert ROLE_FLOOR in ROLE_PRIORITY

    def test_priority_ordering_matches_role_hierarchy(self):
        """Cross-check: the auth dependency module's ROLE_HIERARCHY
        must agree with the base module's ROLE_PRIORITY — same roles,
        same ordering.  Drift between the two would silently break
        require_role checks."""
        from engine.api.auth.dependency import ROLE_HIERARCHY

        assert set(ROLE_PRIORITY.keys()) == set(ROLE_HIERARCHY.keys())
        for role in ROLE_PRIORITY:
            assert ROLE_PRIORITY[role] == ROLE_HIERARCHY[role], (
                f"Priority mismatch for {role!r}: "
                f"base={ROLE_PRIORITY[role]}, dependency={ROLE_HIERARCHY[role]}"
            )


# ===========================================================================
# 4. auth_overwrite_role_on_login — wired into login codepath
# ===========================================================================


class TestAuthOverwriteRoleOnLoginWired:
    """Verify that the ``auth_overwrite_role_on_login`` setting
    actually gates role overwrites in both OIDC and LDAP providers.
    Without these tests the setting could exist in config yet be
    ignored by the codepath — a silent security regression."""

    def test_oidc_source_references_setting(self):
        """Source-level guard: OIDC provider must consult the flag.
        Without this reference the flag is dead config."""
        from engine.api.auth import oidc

        src = inspect.getsource(oidc)
        assert "auth_overwrite_role_on_login" in src

    def test_ldap_source_references_setting(self):
        from engine.api.auth import ldap

        src = inspect.getsource(ldap)
        assert "auth_overwrite_role_on_login" in src

    def test_oidc_source_uses_map_roles_detailed(self):
        """Source-level guard: OIDC must use the explicit-deny variant
        (``map_roles_detailed``) when re-evaluating an existing user's
        role, so a bogus claim doesn't silently downgrade to "user"."""
        from engine.api.auth import oidc

        src = inspect.getsource(oidc)
        assert "map_roles_detailed" in src


# ===========================================================================
# 5. Integration: LDAP — flag toggling changes persisted behaviour on
#    re-login
# ===========================================================================


# ---- LDAP test scaffolding (mirrors test_ldap_auth.py) ---------------------


def _ldap_build_mock(search_results):
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(
        return_value=_make_fake_ldap_conn(search_results=search_results)
    )
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter


def _make_fake_ldap_conn(search_results):
    class _FakeLDAPConn:
        def set_option(self, opt, value):
            pass

        def simple_bind_s(self, dn, password):
            pass

        def search_s(self, base, scope, filterstr, attrlist):
            return search_results

        def unbind_s(self):
            pass

    return _FakeLDAPConn()


def _ldap_make_attrs(*, member_of=None, uid=b"testuser", mail=b"test@example.com",
                     cn=b"Test User"):
    attrs = {
        "uid": [uid],
        "mail": [mail],
        "cn": [cn],
    }
    if member_of is not None:
        attrs["memberOf"] = member_of
    return attrs


def _ldap_make_settings(*, overwrite_role_on_login, monkeypatch):
    s = Settings(
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=developers,ou=groups,dc=example,dc=com": "developer",
            "cn=viewers,ou=groups,dc=example,dc=com": "viewer",
        }),
        auth_overwrite_role_on_login=overwrite_role_on_login,
    )
    monkeypatch.setattr("engine.api.auth.ldap.settings", s)
    return s


def _ldap_make_db_for_existing_user(existing_user):
    """Mock DB that returns ``existing_user`` from the first execute()
    (the (provider, external_id) lookup)."""
    mock_db = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_user
    mock_db.execute.return_value = mock_result
    mock_db.flush = AsyncMock()
    return mock_db


# ---- LDAP re-login integration tests --------------------------------------


class TestLDAPOverwriteFlagIntegration:
    """End-to-end: toggling ``auth_overwrite_role_on_login`` must
    change persisted user role on re-login through LDAP."""

    async def test_flag_false_preserves_higher_role_on_relogin(
        self, monkeypatch
    ):
        """Default-False defense-in-depth: an existing local admin
        logging in through LDAP-with-only-viewer-group must NOT be
        downgraded to viewer."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_make_settings(overwrite_role_on_login=False, monkeypatch=monkeypatch)

        attrs = _ldap_make_attrs(
            member_of=[b"cn=viewers,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _ldap_build_mock(
            search_results=[("uid=adminuser,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="adminuser@example.com",
            display_name="Admin User",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="adminuser",
        )
        mock_db = _ldap_make_db_for_existing_user(existing_user)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="adminuser", password="pass", db=mock_db
            )

        assert result.success is True
        # CRITICAL: existing 'admin' role must be preserved.
        assert existing_user.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_flag_false_preserves_lower_role_against_asserted_higher(
        self, monkeypatch
    ):
        """Reverse direction: an existing viewer that the IdP now
        asserts as admin must NOT be silently escalated when the
        flag is False."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_make_settings(overwrite_role_on_login=False, monkeypatch=monkeypatch)

        attrs = _ldap_make_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _ldap_build_mock(
            search_results=[("uid=viewer,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="viewer@example.com",
            display_name="Viewer User",
            is_active=True,
            role="viewer",
            auth_provider="ldap",
            external_id="viewer",
        )
        mock_db = _ldap_make_db_for_existing_user(existing_user)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="viewer", password="pass", db=mock_db
            )

        assert result.success is True
        # CRITICAL: persisted viewer role preserved; no escalation.
        assert existing_user.role == "viewer"
        mock_db.flush.assert_not_called()

    async def test_flag_true_overwrites_with_higher_role_on_relogin(
        self, monkeypatch
    ):
        """Flag True: IdP asserts admin → existing user is escalated."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)

        attrs = _ldap_make_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _ldap_build_mock(
            search_results=[("uid=promote,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="promote@example.com",
            display_name="Promote Me",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="promote",
        )
        mock_db = _ldap_make_db_for_existing_user(existing_user)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="promote", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_called()

    async def test_flag_true_overwrites_with_lower_role_on_relogin(
        self, monkeypatch
    ):
        """Flag True: IdP asserts viewer → existing admin is downgraded.
        This is the documented opt-in semantic: with overwrite enabled,
        the IdP is the source of truth and can both escalate AND
        downgrade — operators accept that risk explicitly."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)

        attrs = _ldap_make_attrs(
            member_of=[b"cn=viewers,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _ldap_build_mock(
            search_results=[("uid=demote,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="demote@example.com",
            display_name="Demote Me",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="demote",
        )
        mock_db = _ldap_make_db_for_existing_user(existing_user)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="demote", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "viewer"
        mock_db.flush.assert_called()

    async def test_flag_true_no_flush_when_role_unchanged(self, monkeypatch):
        """Idempotency: flag True + same role → no DB write."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)

        attrs = _ldap_make_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _ldap_build_mock(
            search_results=[("uid=same,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="same@example.com",
            display_name="Same",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="same",
        )
        mock_db = _ldap_make_db_for_existing_user(existing_user)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="same", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_flag_false_no_flush_even_when_role_differs(self, monkeypatch):
        """Flag False: never overwrite, never flush — even when the
        IdP claims something different.  This is the persistence
        guarantee operators rely on for the default config."""
        from engine.api.auth.ldap import LDAPAuthProvider
        from engine.db.models import User

        _ldap_make_settings(overwrite_role_on_login=False, monkeypatch=monkeypatch)

        attrs = _ldap_make_attrs(
            member_of=[b"cn=developers,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _ldap_build_mock(
            search_results=[("uid=dev,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="dev@example.com",
            display_name="Dev",
            is_active=True,
            role="viewer",
            auth_provider="ldap",
            external_id="dev",
        )
        mock_db = _ldap_make_db_for_existing_user(existing_user)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await LDAPAuthProvider().authenticate(
                username="dev", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "viewer"  # unchanged
        mock_db.flush.assert_not_called()

    async def test_flag_false_default_in_settings(self):
        """The setting must default to False, regardless of how
        Settings is constructed."""
        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is False


# ===========================================================================
# 6. Integration: OIDC — flag toggling changes persisted behaviour on
#    re-login
# ===========================================================================


# ---- OIDC test scaffolding (mirrors test_oidc_auth.py) ---------------------


def _oidc_build_mock_client(rsa_keys, id_token_claims):
    """Build the httpx mock that returns discovery → token → jwks
    in order.  ``rsa_keys`` is a (private, public) pair."""
    import jwt
    from jwt.algorithms import RSAAlgorithm

    private_key, pub_key = rsa_keys
    kid = "test-kid-123"
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid

    claims = {"aud": "test-client-id", **id_token_claims}
    id_token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})

    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            if "jwks" in url:
                return _Resp({"keys": [jwk_dict]})
            return _Resp({
                "authorization_endpoint": "https://id.example.com/authorize",
                "token_endpoint": "https://id.example.com/token",
                "jwks_uri": "https://id.example.com/jwks",
            })

        async def post(self, url, **kwargs):
            return _Resp({"id_token": id_token, "access_token": "at"})

    return _Client()


def _oidc_make_settings(*, overwrite_role_on_login, monkeypatch):
    s = Settings(
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_redirect_uri="https://app.example.com/callback",
        oidc_role_claim="roles",
        auth_overwrite_role_on_login=overwrite_role_on_login,
    )
    monkeypatch.setattr("engine.api.auth.oidc.settings", s)
    return s


def _oidc_make_db_for_existing_user(existing_user):
    mock_db = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_user
    mock_db.execute.return_value = mock_result
    mock_db.flush = AsyncMock()
    return mock_db


# ---- OIDC re-login integration tests --------------------------------------


class TestOIDCOverwriteFlagIntegration:
    """End-to-end: toggling ``auth_overwrite_role_on_login`` must
    change persisted user role on re-login through OIDC.  These are
    the OIDC counterpart of the LDAP integration tests above."""

    @pytest.fixture
    def rsa_keys(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return private_key, private_key.public_key()

    async def test_flag_false_preserves_higher_role_on_relogin(
        self, monkeypatch, rsa_keys
    ):
        """Default-False: an admin re-logging in with a developer-only
        claim must NOT be downgraded."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_make_settings(overwrite_role_on_login=False, monkeypatch=monkeypatch)

        fake_client = _oidc_build_mock_client(
            rsa_keys,
            {
                "sub": "oidc-admin",
                "email": "admin@example.com",
                "name": "Admin",
                "roles": ["developer"],
            },
        )

        existing_user = User(
            email="admin@example.com",
            display_name="Admin",
            is_active=True,
            role="admin",
            auth_provider="oidc",
            external_id="oidc-admin",
        )
        mock_db = _oidc_make_db_for_existing_user(existing_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"  # preserved
        mock_db.flush.assert_not_called()

    async def test_flag_false_preserves_lower_role_against_asserted_higher(
        self, monkeypatch, rsa_keys
    ):
        """Default-False: a viewer that the IdP now asserts as admin
        must NOT be silently escalated."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_make_settings(overwrite_role_on_login=False, monkeypatch=monkeypatch)

        fake_client = _oidc_build_mock_client(
            rsa_keys,
            {
                "sub": "oidc-escalate",
                "email": "viewer@example.com",
                "name": "Viewer",
                "roles": ["admin"],
            },
        )

        existing_user = User(
            email="viewer@example.com",
            display_name="Viewer",
            is_active=True,
            role="viewer",
            auth_provider="oidc",
            external_id="oidc-escalate",
        )
        mock_db = _oidc_make_db_for_existing_user(existing_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "viewer"  # preserved, no escalation
        mock_db.flush.assert_not_called()

    async def test_flag_true_overwrites_with_higher_role_on_relogin(
        self, monkeypatch, rsa_keys
    ):
        """Flag True: user role is escalated to match the IdP claim."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)

        fake_client = _oidc_build_mock_client(
            rsa_keys,
            {
                "sub": "oidc-promote",
                "email": "promote@example.com",
                "name": "Promote",
                "roles": ["admin"],
            },
        )

        existing_user = User(
            email="promote@example.com",
            display_name="Promote",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-promote",
        )
        mock_db = _oidc_make_db_for_existing_user(existing_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_called()

    async def test_flag_true_overwrites_with_lower_role_on_relogin(
        self, monkeypatch, rsa_keys
    ):
        """Flag True: existing admin is downgraded when the IdP
        asserts only developer.  Opt-in semantic."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)

        fake_client = _oidc_build_mock_client(
            rsa_keys,
            {
                "sub": "oidc-demote",
                "email": "demote@example.com",
                "name": "Demote",
                "roles": ["developer"],
            },
        )

        existing_user = User(
            email="demote@example.com",
            display_name="Demote",
            is_active=True,
            role="admin",
            auth_provider="oidc",
            external_id="oidc-demote",
        )
        mock_db = _oidc_make_db_for_existing_user(existing_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "developer"
        mock_db.flush.assert_called()

    async def test_flag_true_no_flush_when_role_unchanged(
        self, monkeypatch, rsa_keys
    ):
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)

        fake_client = _oidc_build_mock_client(
            rsa_keys,
            {
                "sub": "oidc-same",
                "email": "same@example.com",
                "name": "Same",
                "roles": ["admin"],
            },
        )

        existing_user = User(
            email="same@example.com",
            display_name="Same",
            is_active=True,
            role="admin",
            auth_provider="oidc",
            external_id="oidc-same",
        )
        mock_db = _oidc_make_db_for_existing_user(existing_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_not_called()

    async def test_flag_true_no_overwrite_when_idp_asserts_no_recognized_role(
        self, monkeypatch, rsa_keys
    ):
        """Flag True + IdP asserts ONLY unrecognized roles → existing
        user's role is preserved (NOT downgraded to ``user``).  This
        is the key behavioural difference made by wiring
        ``map_roles_detailed`` (which returns ``None``) into the
        overwrite path: ``None`` means "the IdP made no usable
        assertion, keep what we have"."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)

        fake_client = _oidc_build_mock_client(
            rsa_keys,
            {
                "sub": "oidc-bogus",
                "email": "bogus@example.com",
                "name": "Bogus",
                "roles": ["totally_bogus_role", "not_a_real_role"],
            },
        )

        existing_user = User(
            email="bogus@example.com",
            display_name="Bogus",
            is_active=True,
            role="developer",
            auth_provider="oidc",
            external_id="oidc-bogus",
        )
        mock_db = _oidc_make_db_for_existing_user(existing_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        # CRITICAL: existing developer role preserved; bogus IdP claim
        # did not downgrade to "user".
        assert existing_user.role == "developer"
        mock_db.flush.assert_not_called()

    async def test_flag_true_no_overwrite_when_idp_asserts_non_list_roles(
        self, monkeypatch, rsa_keys
    ):
        """Flag True + non-list ``roles`` claim (string / dict) → no
        overwrite.  Defensive: only well-formed list claims trigger
        the overwrite codepath."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        _oidc_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)

        fake_client = _oidc_build_mock_client(
            rsa_keys,
            {
                "sub": "oidc-str-roles",
                "email": "str@example.com",
                "name": "Str",
                "roles": "admin",  # string, not list
            },
        )

        existing_user = User(
            email="str@example.com",
            display_name="Str",
            is_active=True,
            role="developer",
            auth_provider="oidc",
            external_id="oidc-str-roles",
        )
        mock_db = _oidc_make_db_for_existing_user(existing_user)

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await OIDCAuthProvider().authenticate(
                code="auth-code", db=mock_db
            )

        assert result.success is True
        # Non-list roles → no overwrite path was entered.
        assert existing_user.role == "developer"
        mock_db.flush.assert_not_called()


# ===========================================================================
# 7. Cross-flag integration: prove the SAME persisted user behaves
#    differently depending on the flag value.
# ===========================================================================


class TestFlagToggleChangesPersistedBehavior:
    """The headline integration guarantee: for the SAME user and the
    SAME IdP assertion, toggling the flag must change the persisted
    role after re-login.  These tests are the contract."""

    @staticmethod
    def _build_ldap_user(role_before: str):
        from engine.db.models import User

        return User(
            email="toggle@example.com",
            display_name="Toggle",
            is_active=True,
            role=role_before,
            auth_provider="ldap",
            external_id="toggle",
        )

    async def test_ldap_same_assertion_different_flag_different_outcome(
        self, monkeypatch
    ):
        """IdP asserts ``admin``.  Existing user has role ``user``.
        Flag False → user remains user.  Flag True → user becomes
        admin.  Both branches run on the SAME input."""
        from engine.api.auth.ldap import LDAPAuthProvider

        attrs = _ldap_make_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _ldap_build_mock(
            search_results=[("uid=toggle,ou=users,dc=example,dc=com", attrs)]
        )

        # Branch 1: flag False → no overwrite
        _ldap_make_settings(overwrite_role_on_login=False, monkeypatch=monkeypatch)
        user_a = self._build_ldap_user("user")
        db_a = _ldap_make_db_for_existing_user(user_a)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="toggle", password="pass", db=db_a
            )
        assert user_a.role == "user"  # preserved

        # Branch 2: flag True → overwrite to admin
        _ldap_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)
        user_b = self._build_ldap_user("user")
        db_b = _ldap_make_db_for_existing_user(user_b)

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await LDAPAuthProvider().authenticate(
                username="toggle", password="pass", db=db_b
            )
        assert user_b.role == "admin"  # escalated

        # Headline assertion: same input, different flag, different outcome.
        assert user_a.role != user_b.role

    @pytest.fixture
    def rsa_keys(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return private_key, private_key.public_key()

    async def test_oidc_same_assertion_different_flag_different_outcome(
        self, monkeypatch, rsa_keys
    ):
        """IdP asserts ``admin``.  Existing user has role ``user``.
        Flag False → user remains user.  Flag True → user becomes
        admin.  Both branches run on the SAME input."""
        from engine.api.auth.oidc import OIDCAuthProvider
        from engine.db.models import User

        def _build_user():
            return User(
                email="toggle@example.com",
                display_name="Toggle",
                is_active=True,
                role="user",
                auth_provider="oidc",
                external_id="oidc-toggle",
            )

        claims = {
            "sub": "oidc-toggle",
            "email": "toggle@example.com",
            "name": "Toggle",
            "roles": ["admin"],
        }

        # Branch 1: flag False → no overwrite
        _oidc_make_settings(overwrite_role_on_login=False, monkeypatch=monkeypatch)
        client = _oidc_build_mock_client(rsa_keys, claims)
        user_a = _build_user()
        db_a = _oidc_make_db_for_existing_user(user_a)
        with patch("httpx.AsyncClient", return_value=client):
            await OIDCAuthProvider().authenticate(code="x", db=db_a)
        assert user_a.role == "user"  # preserved

        # Branch 2: flag True → overwrite to admin
        _oidc_make_settings(overwrite_role_on_login=True, monkeypatch=monkeypatch)
        client = _oidc_build_mock_client(rsa_keys, claims)
        user_b = _build_user()
        db_b = _oidc_make_db_for_existing_user(user_b)
        with patch("httpx.AsyncClient", return_value=client):
            await OIDCAuthProvider().authenticate(code="x", db=db_b)
        assert user_b.role == "admin"  # escalated

        assert user_a.role != user_b.role


# ===========================================================================
# 8. End-to-end: mapped role from map_roles_detailed is the role used
#    for downstream authorization decisions.
# ===========================================================================


class TestMapRolesDetailedFlowsToRequireRole:
    """End-to-end: the value returned by ``map_roles_detailed`` must
    flow into ``require_role`` without any silent fallback or
    translation."""

    @pytest.mark.parametrize(
        ("external_roles", "minimum_required", "expected_status"),
        [
            (["viewer"], "viewer", 200),
            (["viewer"], "user", 403),
            (["quant_dev"], "quant_dev", 200),
            (["quant_dev"], "developer", 403),
            (["developer"], "developer", 200),
            (["developer"], "portfolio_manager", 403),
            (["portfolio_manager"], "portfolio_manager", 200),
            (["portfolio_manager"], "admin", 403),
            (["admin"], "admin", 200),
            (["viewer", "quant_dev"], "quant_dev", 200),
            (["viewer", "quant_dev"], "developer", 403),
        ],
    )
    async def test_no_promotion_end_to_end(
        self, external_roles, minimum_required, expected_status
    ):
        from fastapi import Depends, FastAPI
        from httpx import ASGITransport, AsyncClient

        from engine.api.auth.dependency import get_current_user, require_role
        from engine.db.models import User
        from tests.conftest import FAKE_USER_ID

        app = FastAPI()

        @app.get("/guarded")
        async def handler(user: User = Depends(require_role(minimum_required))):
            return {"role": user.role}

        provider = _ConcreteProvider()
        mapped = provider.map_roles_detailed(external_roles)
        # All parametrized inputs contain at least one recognized role,
        # so mapped should never be None here.
        assert mapped is not None

        fake_user = User(
            id=FAKE_USER_ID,
            email="e2e@example.com",
            display_name="E2E",
            is_active=True,
            role=mapped,
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/guarded")
            assert resp.status_code == expected_status, (
                f"external_roles={external_roles} -> mapped={mapped}; "
                f"minimum={minimum_required}; expected {expected_status}, "
                f"got {resp.status_code}"
            )
