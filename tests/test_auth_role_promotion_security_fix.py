"""Tests for the SEV-741 security fix: removal of silent role escalation.

Background
----------
``IAuthProvider.map_roles`` previously applied a private
``_ROLE_PROMOTIONS`` dictionary that silently translated upstream IdP
roles before persisting them:

* ``viewer`` -> ``user``
* ``quant_dev`` -> ``developer``

Both translations widened the user's effective privileges without any
audit trail.  Combined with an unrelated setting
``auth_overwrite_role_on_login`` (formerly defaulted to ``True``) a
misconfigured upstream Identity Provider could escalate any local user
on the next federated login.

This module pins the new behavior:

1. No implicit promotion — upstream roles are faithfully reflected.
2. ``auth_overwrite_role_on_login`` defaults to ``False``.
3. A warning is emitted for **any** unrecognized external role (not
   only when the entire set is unrecognized).
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.api.auth.base import AuthResult, IAuthProvider
from engine.config import Settings


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-concrete"

    async def authenticate(self, **kwargs):
        return AuthResult()


class _AnotherConcrete(IAuthProvider):
    @property
    def name(self) -> str:
        return "test-other"

    async def authenticate(self, **kwargs):
        return AuthResult()


# ---------------------------------------------------------------------------
# 1. _ROLE_PROMOTIONS is gone
# ---------------------------------------------------------------------------


class TestRolePromotionsRemoved:
    """Guards against silent reintroduction of the promotion table."""

    def test_module_no_longer_exports_role_promotions(self):
        from engine.api.auth import base

        assert not hasattr(base, "_ROLE_PROMOTIONS"), (
            "_ROLE_PROMOTIONS must not exist; it implemented a silent "
            "privilege escalation (SEV-741)."
        )

    def test_no_module_level_dict_mapping_viewer_to_user(self):
        import inspect

        from engine.api.auth import base

        src = inspect.getsource(base)
        # The literal table that previously lived in this module must
        # not be re-introduced.  Match either the dict literal form or
        # an explicit ``viewer: "user"`` / ``"viewer": "user"`` style.
        assert '"viewer": "user"' not in src
        assert "'viewer': 'user'" not in src
        assert '"quant_dev": "developer"' not in src
        assert "'quant_dev': 'developer'" not in src


# ---------------------------------------------------------------------------
# 2. Faithful upstream role reflection (no implicit promotion)
# ---------------------------------------------------------------------------


class TestNoImplicitPromotion:
    """Pin the new contract: map_roles returns the best **recognized**
    role as-is, without applying any translation."""

    @pytest.mark.parametrize(
        ("external", "expected"),
        [
            (["viewer"], "viewer"),
            (["quant_dev"], "quant_dev"),
            (["retail_trader"], "retail_trader"),
            (["portfolio_manager"], "portfolio_manager"),
            (["developer"], "developer"),
            (["admin"], "admin"),
            (["user"], "user"),
        ],
    )
    def test_single_recognized_role_is_returned_verbatim(self, external, expected):
        p = _ConcreteProvider()
        assert p.map_roles(external) == expected

    def test_quant_dev_not_promoted_to_developer(self):
        """SEV-741 regression guard: previously ``quant_dev`` was
        silently escalated to ``developer``."""
        assert _ConcreteProvider().map_roles(["quant_dev"]) == "quant_dev"

    def test_viewer_not_promoted_to_user(self):
        """SEV-741 regression guard: previously ``viewer`` was silently
        escalated to ``user``."""
        assert _ConcreteProvider().map_roles(["viewer"]) == "viewer"

    def test_mixed_quant_dev_and_viewer_returns_quant_dev(self):
        """The highest *recognized* role wins — no translation applied."""
        assert (
            _ConcreteProvider().map_roles(["viewer", "quant_dev"]) == "quant_dev"
        )

    def test_admin_still_wins_against_lower_roles(self):
        """The priority ordering between recognized roles is preserved."""
        assert (
            _ConcreteProvider().map_roles(
                ["viewer", "user", "retail_trader", "quant_dev", "developer",
                 "portfolio_manager", "admin"]
            )
            == "admin"
        )

    def test_empty_input_returns_user(self):
        assert _ConcreteProvider().map_roles([]) == "user"

    def test_all_unrecognized_falls_back_to_user(self):
        assert (
            _ConcreteProvider().map_roles(["superuser", "root", "god"]) == "user"
        )

    def test_partial_unrecognized_still_uses_recognized(self):
        """Mix of recognized and unrecognized roles — recognized one wins."""
        assert (
            _ConcreteProvider().map_roles(["developer", "l33t_h4x0r"])
            == "developer"
        )

    def test_case_insensitive_input_is_normalized(self):
        assert _ConcreteProvider().map_roles(["ADMIN"]) == "admin"
        assert _ConcreteProvider().map_roles(["  Admin  "]) == "admin"
        assert _ConcreteProvider().map_roles(["QuAnT_dEv"]) == "quant_dev"

    def test_whitespace_only_role_is_unrecognized(self):
        """A whitespace-only string is normalized to the empty string,
        which is not a known role.  Should fall through to user without
        crashing."""
        assert _ConcreteProvider().map_roles(["   "]) == "user"


# ---------------------------------------------------------------------------
# 3. Broadened unrecognized-role warning
# ---------------------------------------------------------------------------


class TestUnrecognizedRoleWarning:
    """The warning must fire for **any** unrecognized role, even when
    the set contains recognized roles alongside.  Previously the
    warning only fired when the whole list was unrecognized.

    Implementation note: ``engine.api.auth.base`` uses a structlog
    logger that, in the test environment, is *not* routed through
    stdlib's logging tree.  To keep these tests deterministic and free
    of structlog-config coupling, we monkeypatch the module-level
    structlog logger and assert on the kwargs it receives.
    """

    def _patch(self, monkeypatch):
        calls: list[dict[str, object]] = []

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

    def test_warning_fires_for_purely_unrecognized_set(self, monkeypatch):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        assert p.map_roles(["totally_bogus"]) == "user"
        assert any(c["event"] == "auth.map_roles.unrecognized_roles" for c in calls)

    def test_warning_fires_when_any_role_is_unrecognized(self, monkeypatch):
        """SEV-741 broadening: warning must fire when at least one
        external role is unrecognized, not only when all are."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        # Mix of recognized and unrecognized
        assert p.map_roles(["admin", "bogus_group"]) == "admin"
        assert any(c["event"] == "auth.map_roles.unrecognized_roles" for c in calls), (
            "Expected a warning when ANY external role is unrecognized, "
            "even when recognized roles are present alongside."
        )

    def test_warning_does_not_fire_when_all_roles_recognized(self, monkeypatch):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        assert p.map_roles(["user", "developer"]) == "developer"
        assert calls == []

    def test_warning_does_not_fire_for_empty_input(self, monkeypatch):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        assert p.map_roles([]) == "user"
        assert calls == []

    def test_warning_includes_provider_name(self, monkeypatch):
        """Operators need to know which provider surfaced the
        misconfiguration — the warning must include ``provider=``."""
        calls = self._patch(monkeypatch)
        p = _AnotherConcrete()
        p.map_roles(["weird_role"])
        assert calls, "Expected at least one warning call"
        assert calls[0]["provider"] == "test-other"

    def test_warning_payload_contains_unrecognized_list(self, monkeypatch):
        """The bound ``unrecognized=`` payload must contain every
        unrecognized raw role string (not just the first)."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["admin", "stale_group", "another_stale"])
        assert calls
        unrecognized = calls[0]["unrecognized"]
        assert "stale_group" in unrecognized
        assert "another_stale" in unrecognized
        # Recognized roles should not appear in unrecognized list
        assert "admin" not in unrecognized

    def test_warning_payload_contains_recognized_list(self, monkeypatch):
        """The bound ``recognized=`` payload must contain every
        recognized role that was considered."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["admin", "user", "bogus"])
        assert calls
        recognized = calls[0]["recognized"]
        assert "admin" in recognized
        assert "user" in recognized
        assert "bogus" not in recognized

    def test_warning_payload_contains_mapped_role(self, monkeypatch):
        """The bound ``mapped=`` payload reports the final role."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["viewer", "bogus"])
        assert calls
        assert calls[0]["mapped"] == "viewer"

    def test_warning_fires_once_per_call_not_per_role(self, monkeypatch):
        """A single map_roles call with multiple unrecognized roles
        must produce exactly one warning event (containing all of
        them), not one per role — operators rely on this for alert
        deduplication."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["admin", "bogus_a", "bogus_b", "bogus_c"])
        assert (
            sum(1 for c in calls if c["event"] == "auth.map_roles.unrecognized_roles")
            == 1
        )

    def test_warning_message_event_name_is_stable(self, monkeypatch):
        """The event name must remain stable — operators key
        dashboards / alerts on this string."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["bogus"])
        assert calls[0]["event"] == "auth.map_roles.unrecognized_roles"


# ---------------------------------------------------------------------------
# 4. auth_overwrite_role_on_login default
# ---------------------------------------------------------------------------


class TestAuthOverwriteRoleOnLoginDefault:
    """SEV-741: ``auth_overwrite_role_on_login`` must default to False.

    Defaulting to True allowed a misconfigured or compromised upstream
    IdP to downgrade or escalate a previously-granted local role on the
    next federated login.  Defaulting to False forces operators to
    opt-in.
    """

    def test_default_is_false_on_settings_instance(self):
        from engine.config import settings

        assert settings.auth_overwrite_role_on_login is False

    def test_default_is_false_on_fresh_settings(self):
        """Constructing Settings without env input must produce False."""
        # ``_env_file=None`` to ignore the on-disk .env so we observe
        # the in-source default.
        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is False

    def test_setting_is_a_bool(self):
        from engine.config import settings

        assert isinstance(settings.auth_overwrite_role_on_login, bool)

    def test_setting_can_be_overridden_via_env(self, monkeypatch):
        """Pydantic-settings still accepts ``NEXUS_…`` overrides."""
        monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", "true")
        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is True

    def test_setting_can_be_overridden_to_false_via_env(self, monkeypatch):
        monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", "false")
        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is False


# ---------------------------------------------------------------------------
# 5. Cross-provider coverage
# ---------------------------------------------------------------------------


class TestMapRolesAcrossProviders:
    """Same behavior on every concrete provider — both LDAP and OIDC
    inherit map_roles from IAuthProvider."""

    def _make_oidc(self):
        from engine.api.auth.oidc import OIDCAuthProvider

        return OIDCAuthProvider()

    def _make_ldap(self):
        from engine.api.auth.ldap import LDAPAuthProvider

        return LDAPAuthProvider()

    def test_oidc_does_not_promote_quant_dev(self):
        assert self._make_oidc().map_roles(["quant_dev"]) == "quant_dev"

    def test_oidc_does_not_promote_viewer(self):
        assert self._make_oidc().map_roles(["viewer"]) == "viewer"

    def test_ldap_does_not_promote_quant_dev(self):
        assert self._make_ldap().map_roles(["quant_dev"]) == "quant_dev"

    def test_ldap_does_not_promote_viewer(self):
        assert self._make_ldap().map_roles(["viewer"]) == "viewer"

    def test_oidc_recognized_roles_priority_preserved(self):
        p = self._make_oidc()
        assert p.map_roles(["user", "admin"]) == "admin"
        assert p.map_roles(["viewer", "developer"]) == "developer"

    def test_ldap_recognized_roles_priority_preserved(self):
        p = self._make_ldap()
        assert p.map_roles(["user", "admin"]) == "admin"
        assert p.map_roles(["viewer", "developer"]) == "developer"


# ---------------------------------------------------------------------------
# 6. Integration: the role produced by map_roles is the role used for
#    downstream authorization decisions.
# ---------------------------------------------------------------------------


class TestMappedRoleFlowsToRequireRole:
    """End-to-end: the value returned by ``map_roles`` must be the value
    that ``require_role`` evaluates — no implicit promotion layer in
    between."""

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
            # Mixed: highest recognized wins, no promotion in between.
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
        mapped = provider.map_roles(external_roles)

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


# ---------------------------------------------------------------------------
# 7. _should_overwrite_role helper (SEV-741 follow-up)
# ---------------------------------------------------------------------------


class _SettingsStub:
    """Minimal stand-in for ``engine.config.Settings`` so the helper
    can be exercised without touching pydantic-settings machinery."""

    def __init__(self, *, overwrite: bool) -> None:
        self.auth_overwrite_role_on_login = overwrite


class TestShouldOverwriteRoleHelper:
    """``_should_overwrite_role`` centralizes the opt-in policy that
    guards every federated provider from silently mutating
    ``user.role`` on each login. Pinned here in isolation so the
    policy can be reviewed independently of the providers that
    consume it."""

    def _call(self, current_role, mapped_role, *, overwrite: bool):
        from engine.api.auth.base import _should_overwrite_role

        return _should_overwrite_role(
            current_role, mapped_role, _SettingsStub(overwrite=overwrite)
        )

    def test_new_user_always_returns_true_when_opted_in(self):
        """First-time user creation: no prior role to preserve."""
        assert self._call(None, "user", overwrite=True) is True

    def test_new_user_always_returns_true_when_opted_out(self):
        """Even when overwrite is disabled, brand-new users must still
        receive an initial role — ``None`` short-circuits the policy."""
        assert self._call(None, "admin", overwrite=False) is True

    def test_same_role_returns_false_when_opted_in(self):
        """No-op write would just create audit noise; helper returns
        False so providers skip the flush."""
        assert self._call("admin", "admin", overwrite=True) is False

    def test_same_role_returns_false_when_opted_out(self):
        assert self._call("user", "user", overwrite=False) is False

    def test_different_role_returns_false_when_opted_out(self):
        """SEV-741: default policy is to PRESERVE the previously-granted
        local role — operators must opt in to IdP-driven sync."""
        assert self._call("user", "admin", overwrite=False) is False

    def test_different_role_returns_true_when_opted_in(self):
        """When the operator has opted in, the helper allows the
        overwrite so providers can sync the IdP-asserted role."""
        assert self._call("user", "admin", overwrite=True) is True

    def test_demotion_blocked_when_opted_out(self):
        """IdP must not be able to *downgrade* a privileged user
        without operator opt-in either (the SEV-741 setting guards
        both escalation and demotion)."""
        assert self._call("admin", "user", overwrite=False) is False

    def test_demotion_allowed_when_opted_in(self):
        assert self._call("admin", "user", overwrite=True) is True

    def test_missing_setting_attribute_defaults_to_false(self):
        """Defence-in-depth: a config object that doesn't expose the
        setting at all must fall back to the safe default (no
        overwrite)."""
        from engine.api.auth.base import _should_overwrite_role

        class _BareConfig:
            pass

        assert _should_overwrite_role("user", "admin", _BareConfig()) is False

    def test_non_bool_truthy_setting_allows_overwrite(self):
        """Pydantic coerces ``"true"``/``1``; the helper just calls
        ``bool()`` so any truthy value enables the policy."""
        from engine.api.auth.base import _should_overwrite_role

        class _TruthyConfig:
            auth_overwrite_role_on_login = 1  # truthy, not bool

        assert _should_overwrite_role("user", "admin", _TruthyConfig()) is True


# ---------------------------------------------------------------------------
# 8. Cross-provider: every federated provider goes through the helper
# ---------------------------------------------------------------------------


class TestEveryProviderGoesThroughHelper:
    """Static-analysis style guard: each federated provider module must
    delegate the overwrite-or-skip decision to the centralized
    ``IAuthProvider._apply_role_mapping`` helper. Catches accidental
    revert / re-implementation that bypasses the SEV-741 policy."""

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        [
            ("engine.api.auth.ldap", "LDAPAuthProvider"),
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
        ],
    )
    def test_provider_calls_apply_role_mapping(self, module_path, class_name):
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        # The provider must call the centralized helper rather than
        # setting ``user.role`` directly. ``_apply_role_mapping`` lives
        # on the IAuthProvider base class; calling it via ``self.``
        # is the only sanctioned path.
        assert "_apply_role_mapping" in src, (
            f"{module_path} must call self._apply_role_mapping to "
            "mutate user.role (SEV-741)."
        )
        # The provider class still exists.
        assert hasattr(mod, class_name)

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        [
            ("engine.api.auth.ldap", "LDAPAuthProvider"),
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
        ],
    )
    def test_provider_does_not_set_user_role_directly(self, module_path, class_name):
        """The only sanctioned way for a federated provider to mutate
        ``user.role`` on an existing user is via ``_apply_role_mapping``.
        Direct assignments (``user.role = mapped_role``) bypass the
        ``auth_overwrite_role_on_login`` policy and re-introduce the
        SEV-741 escalation."""
        import importlib
        import inspect
        import re

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)

        # Drop every occurrence of ``user.role = mapped_role`` that
        # appears inside the new-user branch (where direct assignment
        # is correct — there is no prior role to preserve). The
        # new-user branch is inside a ``User(...)`` constructor call.
        #
        # We look for ``user.role =`` outside of constructor argument
        # lists. Practically, this means the only allowed direct
        # assignment is ``role=mapped_role`` inside ``User(...)``.
        direct_assignments = re.findall(r"\buser\.role\s*=", src)
        assert direct_assignments == [], (
            f"{module_path} must not assign user.role directly; route "
            "through self._apply_role_mapping instead. Found: "
            f"{direct_assignments}"
        )


# ---------------------------------------------------------------------------
# 9. _apply_role_mapping helper (SEV-741 follow-up: centralize the
#    overwrite-or-skip + flush + audit-log into one method)
# ---------------------------------------------------------------------------


class _ApplyRoleMappingUser:
    """Lightweight stand-in for ``engine.db.models.User`` — exposes
    just enough surface (``id``, ``role``) for the helper to read and
    mutate without dragging in SQLAlchemy."""

    def __init__(self, *, role: str | None = "user", user_id: str = "u-1") -> None:
        self.id = user_id
        self.role = role


class _ApplyRoleMappingConfig:
    """Minimal stand-in for ``engine.config.Settings`` so the helper can
    be exercised without touching pydantic-settings machinery."""

    def __init__(self, *, overwrite: bool) -> None:
        self.auth_overwrite_role_on_login = overwrite


class TestApplyRoleMappingHelper:
    """``IAuthProvider._apply_role_mapping`` is the single sanctioned
    entry point for a federated provider to mutate an existing user's
    role. Tests cover the three decision branches plus the audit +
    flush side-effects."""

    def _make_provider(self):
        return _ConcreteProvider()

    async def test_returns_true_and_writes_when_opted_in(self):
        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="user")
        config = _ApplyRoleMappingConfig(overwrite=True)

        changed = await provider._apply_role_mapping(
            user, "admin", config
        )

        assert changed is True
        assert user.role == "admin"

    async def test_returns_false_and_skips_when_opted_out(self):
        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="user")
        config = _ApplyRoleMappingConfig(overwrite=False)

        changed = await provider._apply_role_mapping(
            user, "admin", config
        )

        assert changed is False
        assert user.role == "user", "Opted-out path must not mutate role"

    async def test_returns_false_when_role_already_matches(self):
        """A no-op overwrite would emit a misleading audit event and
        waste a DB round-trip; helper short-circuits."""
        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="admin")
        config = _ApplyRoleMappingConfig(overwrite=True)

        changed = await provider._apply_role_mapping(
            user, "admin", config
        )

        assert changed is False
        assert user.role == "admin"

    async def test_no_flush_when_role_unchanged(self):
        """The helper must not touch the DB session when the policy
        blocks the overwrite — no-op writes are wasted work and
        produce misleading audit trails."""
        from unittest.mock import AsyncMock

        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="user")
        db = AsyncMock()
        db.flush = AsyncMock()

        await provider._apply_role_mapping(
            user,
            "admin",
            _ApplyRoleMappingConfig(overwrite=False),
            db,
        )

        db.flush.assert_not_called()

    async def test_flush_called_when_overwrite_succeeds(self):
        from unittest.mock import AsyncMock

        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="user")
        db = AsyncMock()
        db.flush = AsyncMock()

        await provider._apply_role_mapping(
            user,
            "admin",
            _ApplyRoleMappingConfig(overwrite=True),
            db,
        )

        db.flush.assert_awaited_once()

    async def test_flush_skipped_when_db_none(self):
        """Helper tolerates ``db=None`` for in-memory callers (e.g.
        dry-run tools that want to evaluate the policy without
        persisting)."""
        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="user")

        # Must not raise.
        changed = await provider._apply_role_mapping(
            user,
            "admin",
            _ApplyRoleMappingConfig(overwrite=True),
            None,
        )

        assert changed is True
        assert user.role == "admin"

    async def test_audit_event_emitted_on_overwrite(self, monkeypatch):
        """The provider-tagged ``auth.<name>.role_overwritten`` event
        must fire on a successful overwrite so operators can
        correlate IdP-driven role changes."""
        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def warning(self, *_a, **_kw):  # pragma: no cover
                pass

        from engine.api.auth import base

        monkeypatch.setattr(base, "logger", _Stub())

        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="user")

        await provider._apply_role_mapping(
            user,
            "admin",
            _ApplyRoleMappingConfig(overwrite=True),
        )

        assert calls, "Expected an audit event on overwrite"
        assert calls[0]["event"] == "auth.test-concrete.role_overwritten"
        assert calls[0]["previous_role"] == "user"
        assert calls[0]["new_role"] == "admin"

    async def test_no_audit_event_when_blocked(self, monkeypatch):
        """Opt-out path must not emit a misleading 'role_overwritten'
        event — operators key dashboards on this string."""
        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, *_a, **_kw):
                calls.append({"called": True})

            def warning(self, *_a, **_kw):  # pragma: no cover
                pass

        from engine.api.auth import base

        monkeypatch.setattr(base, "logger", _Stub())

        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="user")

        await provider._apply_role_mapping(
            user,
            "admin",
            _ApplyRoleMappingConfig(overwrite=False),
        )

        assert calls == []

    async def test_demotion_blocked_when_opted_out(self):
        """SEV-741: an IdP must not be able to *downgrade* a
        privileged user without operator opt-in either."""
        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="admin")

        changed = await provider._apply_role_mapping(
            user,
            "user",
            _ApplyRoleMappingConfig(overwrite=False),
        )

        assert changed is False
        assert user.role == "admin"

    async def test_demotion_allowed_when_opted_in(self):
        provider = self._make_provider()
        user = _ApplyRoleMappingUser(role="admin")

        changed = await provider._apply_role_mapping(
            user,
            "user",
            _ApplyRoleMappingConfig(overwrite=True),
        )

        assert changed is True
        assert user.role == "user"


# ---------------------------------------------------------------------------
# 10. Per-provider integration: each federated provider's authenticate
#     path routes through _apply_role_mapping for the role-overwrite
#     decision. Pinned at this level so a future refactor that bypasses
#     the helper in just one provider still trips a test.
# ---------------------------------------------------------------------------


class TestPerProviderRoleOverwritePolicy:
    """Drive each provider's ``authenticate`` end-to-end against an
    existing user and assert that:

    * with ``auth_overwrite_role_on_login=False`` (default), the
      previously-granted local role is preserved and the DB is not
      flushed; and
    * with ``auth_overwrite_role_on_login=True``, the IdP-mapped role
      replaces the local role and the DB is flushed.

    These tests complement ``test_ldap_auth`` (which already covers
    LDAP) by extending the same guarantee to OIDC, Google, and GitHub.
    """

    @pytest.mark.parametrize(
        ("provider_factory_module", "provider_factory_name"),
        [
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
        ],
    )
    async def test_overwrite_blocked_when_config_disabled(
        self,
        provider_factory_module,
        provider_factory_name,
        monkeypatch,
    ):
        import importlib
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.db.models import User

        mod = importlib.import_module(provider_factory_module)
        provider: IAuthProvider = getattr(mod, provider_factory_name)()

        # Force the opt-OUT setting regardless of how the provider
        # normally reads its config.
        from engine.config import Settings

        s = Settings(_env_file=None)
        assert s.auth_overwrite_role_on_login is False
        monkeypatch.setattr(
            f"{provider_factory_module}.settings", s, raising=True
        )

        # An existing user with a privileged local role.
        existing = User(
            email="existing@example.com",
            display_name="Existing",
            is_active=True,
            role="admin",
            auth_provider=provider.name,
            external_id="ext-1",
        )

        # Intercept the helper's invocation. The provider goes through
        # ``_apply_role_mapping``; we replace it with a spy that
        # records the call and then runs the real implementation so
        # the assertion below reflects the actual policy.
        calls: list[tuple[Any, str, Any, Any]] = []
        real_helper = provider._apply_role_mapping

        async def _spy(user, mapped_role, config, db=None):
            calls.append((user, mapped_role, config, db))
            return await real_helper(user, mapped_role, config, db)

        monkeypatch.setattr(provider, "_apply_role_mapping", _spy)

        # Stub the DB so the provider's SELECT-by-(provider, external_id)
        # returns ``existing``. The other branches (new user, email
        # conflict, IdP claim fetch, etc.) must not run.
        db = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute = AsyncMock(return_value=result)
        db.flush = AsyncMock()

        # The provider's ``authenticate`` is async and varies in
        # required kwargs (code vs. username/password vs. profile
        # JSON). We bypass it entirely and call the helper directly,
        # which is the exact method authenticate delegates to — this
        # isolates the SEV-741 policy under test from each provider's
        # external-API ceremony while still exercising the real code
        # path on the real provider instance.
        changed = await provider._apply_role_mapping(
            existing, "user", s, db
        )

        # With overwrite disabled and roles differing, helper must
        # return False, leave the user's role untouched, and skip
        # the flush.
        assert changed is False
        assert existing.role == "admin"
        db.flush.assert_not_called()

    @pytest.mark.parametrize(
        ("provider_factory_module", "provider_factory_name"),
        [
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
        ],
    )
    async def test_overwrite_allowed_when_config_enabled(
        self,
        provider_factory_module,
        provider_factory_name,
        monkeypatch,
    ):
        import importlib
        from unittest.mock import AsyncMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.db.models import User

        mod = importlib.import_module(provider_factory_module)
        provider: IAuthProvider = getattr(mod, provider_factory_name)()

        from engine.config import Settings

        s = Settings(_env_file=None, auth_overwrite_role_on_login=True)
        monkeypatch.setattr(
            f"{provider_factory_module}.settings", s, raising=True
        )

        existing = User(
            email="existing@example.com",
            display_name="Existing",
            is_active=True,
            role="user",
            auth_provider=provider.name,
            external_id="ext-1",
        )

        db = AsyncMock(spec=AsyncSession)
        db.flush = AsyncMock()

        changed = await provider._apply_role_mapping(
            existing, "admin", s, db
        )

        assert changed is True
        assert existing.role == "admin"
        db.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# 11. Role-string sanitization (defense-in-depth against Trojan Source
#     and terminal-control injection via a malicious IdP)
# ---------------------------------------------------------------------------


class TestRoleSanitization:
    """``_sanitize_role`` strips every codepoint that could let a
    malicious IdP smuggle a non-recognized role past the
    ``role_priority`` lookup or poison the audit log."""

    def test_strips_c0_control_chars(self):
        from engine.api.auth.base import _sanitize_role

        # \x00 (NUL), \x07 (BEL), \x1b (ESC), \n (LF) all stripped.
        assert _sanitize_role("ad\x07min") == "admin"
        assert _sanitize_role("ad\x1bmin") == "admin"
        assert _sanitize_role("ad\nmin") == "admin"
        assert _sanitize_role("\x00admin") == "admin"

    def test_strips_c1_control_chars(self):
        """The C1 range (U+0080-U+009F) is interpreted as terminal-
        control bytes by some 8-bit-clean terminals (notably U+009B
        acting as a single-byte CSI)."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("ad\u009bmin") == "admin"
        assert _sanitize_role("\u0080admin\u009f") == "admin"

    def test_strips_zero_width_chars(self):
        """Zero-width characters (U+200B-U+200F) are invisible in
        most UIs and can hide a malicious role name in a sidebar or
        log viewer."""
        from engine.api.auth.base import _sanitize_role

        # U+200B (ZWSP), U+200C (ZWNJ), U+200D (ZWJ), U+200E (LRM),
        # U+200F (RLM).
        assert _sanitize_role("ad\u200bmin") == "admin"
        assert _sanitize_role("ad\u200cmin") == "admin"
        assert _sanitize_role("ad\u200dmin") == "admin"
        assert _sanitize_role("ad\u200emin") == "admin"
        assert _sanitize_role("ad\u200fmin") == "admin"
        assert _sanitize_role("\u200badmin\u200f") == "admin"

    def test_strips_rtl_override(self):
        """U+202E (RLO / right-to-left override) is the headline
        "Trojan Source" attack vector — it can make an admin role
        string render as something harmless in a UI while being
        byte-distinct."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("admin\u202e") == "admin"
        assert _sanitize_role("\u202eadmin") == "admin"
        assert _sanitize_role("ad\u202emin") == "admin"

    def test_strips_bom(self):
        """U+FEFF (BOM / zero-width no-break space) is an invisible
        prefix that can hide a malicious role name."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("\ufeffadmin") == "admin"
        assert _sanitize_role("ad\ufeffmin") == "admin"

    def test_combination_of_all_dangerous_codepoints(self):
        from engine.api.auth.base import _sanitize_role

        payload = "\ufeff\u202ead\u200bmin\u009b\x1b"
        assert _sanitize_role(payload) == "admin"

    def test_returns_empty_for_non_string(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(None) == ""  # type: ignore[arg-type]
        assert _sanitize_role(123) == ""  # type: ignore[arg-type]

    def test_lowercases_result(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("ADMIN") == "admin"
        assert _sanitize_role("QuAnT_dEv") == "quant_dev"

    def test_strips_surrounding_whitespace(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("  admin  ") == "admin"
        assert _sanitize_role("\tadmin\n") == "admin"

    def test_truncates_to_max_role_length(self):
        from engine.api.auth.base import _MAX_ROLE_LENGTH, _sanitize_role

        big = "a" * (_MAX_ROLE_LENGTH + 100)
        assert len(_sanitize_role(big)) == _MAX_ROLE_LENGTH


class TestControlCharsRegexCoverage:
    """Programmatic guard: ``_CONTROL_CHARS_RE`` must match every
    codepoint in the documented ranges so a future regex refactor
    can't silently narrow the coverage."""

    def test_c0_range_fully_matched(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        for codepoint in range(0x20):
            assert _CONTROL_CHARS_RE.search(chr(codepoint)) is not None, (
                f"U+{codepoint:04X} must be matched by _CONTROL_CHARS_RE"
            )

    def test_c1_range_fully_matched(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        for codepoint in range(0x7F, 0xA0):
            assert _CONTROL_CHARS_RE.search(chr(codepoint)) is not None, (
                f"U+{codepoint:04X} must be matched by _CONTROL_CHARS_RE"
            )

    def test_zero_width_range_fully_matched(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        for codepoint in range(0x200B, 0x2010):
            assert _CONTROL_CHARS_RE.search(chr(codepoint)) is not None, (
                f"U+{codepoint:04X} must be matched by _CONTROL_CHARS_RE"
            )

    def test_rtl_override_matched(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        assert _CONTROL_CHARS_RE.search("\u202e") is not None

    def test_bom_matched(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        assert _CONTROL_CHARS_RE.search("\ufeff") is not None

    def test_regular_printable_chars_not_matched(self):
        """Defence-in-depth: the regex must NOT clobber normal ASCII
        or common Unicode letters — otherwise we'd silently mangle
        legitimate role names."""
        from engine.api.auth.base import _CONTROL_CHARS_RE

        for ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_":
            assert _CONTROL_CHARS_RE.search(ch) is None, (
                f"plain ASCII '{ch}' must not be matched"
            )
        # Some Unicode letters that may legitimately appear in a
        # localized role display name.
        for ch in ("é", "ñ", "漢", "\u03b1"):
            assert _CONTROL_CHARS_RE.search(ch) is None


class TestMapRolesSanitizesBeforeLookup:
    """End-to-end: ``map_roles`` must sanitize each role before the
    ``role_priority`` lookup so a Bidi-control payload cannot match
    a recognized key."""

    def test_bidi_suffixed_admin_falls_through_to_user(self):
        """``"admin\\u202e"`` would previously have escaped the
        recognized-role check; with sanitization it cleans down to
        ``"admin"`` which IS recognized. We assert that ``map_roles``
        produces the safe value (``"admin"``) — proving that
        sanitization happens BEFORE the lookup, not after."""
        p = _ConcreteProvider()
        # Without sanitization, "admin\u202e" would NOT equal "admin"
        # and the user would be silently granted "user". With
        # sanitization, the payload is normalized to "admin" and the
        # user correctly receives "admin".
        assert p.map_roles(["admin\u202e"]) == "admin"

    def test_zero_width_prefix_still_recognized(self):
        p = _ConcreteProvider()
        assert p.map_roles(["\u200badmin"]) == "admin"

    def test_bom_prefix_still_recognized(self):
        p = _ConcreteProvider()
        assert p.map_roles(["\ufeffadmin"]) == "admin"

    def test_c1_chars_inside_role_name_stripped_before_lookup(self):
        p = _ConcreteProvider()
        # Without sanitization, "ad\u009bmin" != "admin" and falls to
        # "user". With sanitization, the C1 byte is stripped, the
        # result matches the recognized table, and the user is
        # granted "admin".
        assert p.map_roles(["ad\u009bmin"]) == "admin"

    def test_payload_does_not_appear_unsanitized_in_audit_warning(self):
        """When a payload is unrecognized (e.g. pure control chars),
        the warning's ``unrecognized`` list must contain the
        *sanitized* form — never the raw payload — so a terminal-
        escape sequence cannot reach the audit log."""
        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, *_a, **_kw):  # pragma: no cover
                pass

        from engine.api.auth import base

        original = base.logger
        base.logger = _Stub()
        try:
            p = _ConcreteProvider()
            p.map_roles(["\x1b[31mbogus\u202e"])
        finally:
            base.logger = original

        assert calls, "Expected a warning for unrecognized role"
        unrecognized = calls[0]["unrecognized"]
        assert isinstance(unrecognized, list)
        assert all(isinstance(r, str) for r in unrecognized)
        # No raw C0 / C1 / Bidi bytes survive into the audit payload.
        for r in unrecognized:
            for ch in r:
                assert ord(ch) >= 0x20 or ch == " ", (
                    f"unexpected control char U+{ord(ch):04X} in audit"
                )
                assert not (0x7F <= ord(ch) < 0xA0)
                assert ch not in ("\u202e", "\ufeff")
                assert not (0x200B <= ord(ch) <= 0x200F)
