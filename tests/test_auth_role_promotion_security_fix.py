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

    def _call(self, current_role, mapped_role, *, overwrite: bool, is_new_user: bool = False):
        from engine.api.auth.base import _should_overwrite_role

        return _should_overwrite_role(
            current_role,
            mapped_role,
            _SettingsStub(overwrite=overwrite),
            is_new_user=is_new_user,
        )

    def test_new_user_always_returns_true_when_opted_in(self):
        """First-time user creation: no prior role to preserve."""
        assert self._call(None, "user", overwrite=True, is_new_user=True) is True

    def test_new_user_always_returns_true_when_opted_out(self):
        """Even when overwrite is disabled, brand-new users must still
        receive an initial role — ``None`` short-circuits the policy
        when the caller confirms it's a fresh insert."""
        assert self._call(None, "admin", overwrite=False, is_new_user=True) is True

    def test_existing_user_with_none_role_requires_opt_in(self):
        """SEV-741 follow-up: ``current_role=None`` on an EXISTING user
        is anomalous (the column has a non-null default). Treat it as
        an existing-row case and require operator opt-in rather than
        silently allowing the IdP to set the role.
        """
        assert self._call(None, "user", overwrite=False, is_new_user=False) is False
        assert self._call(None, "admin", overwrite=False, is_new_user=False) is False
        assert self._call(None, "user", overwrite=True, is_new_user=False) is True

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

    def test_existing_user_with_none_role_bare_config_defaults_to_false(self):
        """An existing-row / None-role edge case against a config that
        doesn't expose the setting: must default to safe (no overwrite).
        """
        from engine.api.auth.base import _should_overwrite_role

        class _BareConfig:
            pass

        assert (
            _should_overwrite_role(None, "admin", _BareConfig(), is_new_user=False)
            is False
        )


# ---------------------------------------------------------------------------
# 8. Cross-provider: every federated provider goes through the helper
# ---------------------------------------------------------------------------


class TestEveryProviderGoesThroughHelper:
    """Static-analysis style guard: each federated provider module must
    import the helper. Catches accidental revert / re-implementation
    that bypasses the centralized SEV-741 policy."""

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        [
            ("engine.api.auth.ldap", "LDAPAuthProvider"),
            ("engine.api.auth.oidc", "OIDCAuthProvider"),
            ("engine.api.auth.google", "GoogleAuthProvider"),
            ("engine.api.auth.github_oauth", "GitHubAuthProvider"),
        ],
    )
    def test_provider_imports_should_overwrite_role(self, module_path, class_name):
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        # SEV-741: providers must funnel role-mutation through the
        # centralized ``_apply_role_mapping`` helper (which itself
        # delegates to ``_should_overwrite_role``). Accept either
        # import as evidence that the provider goes through the
        # canonical policy path.
        assert ("_should_overwrite_role" in src) or ("_apply_role_mapping" in src), (
            f"{module_path} must import _should_overwrite_role or "
            "_apply_role_mapping from engine.api.auth.base (SEV-741)."
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
    def test_provider_imports_apply_role_mapping(self, module_path, class_name):
        """SEV-741 follow-up: every provider must go through the
        ``_apply_role_mapping`` helper so sanitization, the opt-in
        overwrite decision, the audit log event, and the flush all
        happen in lock-step."""
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        assert "_apply_role_mapping" in src, (
            f"{module_path} must import _apply_role_mapping from "
            "engine.api.auth.base (SEV-741 follow-up)."
        )
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
    def test_provider_imports_sanitize_role(self, module_path, class_name):
        """SEV-741 follow-up: every provider must sanitize the mapped
        role through ``_sanitize_role`` (NFKC + BiDi strip +
        allow-list) before persisting it."""
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        assert "_sanitize_role" in src, (
            f"{module_path} must import _sanitize_role from "
            "engine.api.auth.base (SEV-741 follow-up)."
        )
        assert hasattr(mod, class_name)


# ---------------------------------------------------------------------------
# 9. ALLOWED_ROLES frozenset contract
# ---------------------------------------------------------------------------


class TestAllowedRolesContract:
    """SEV-741 follow-up: ``ALLOWED_ROLES`` is the single source of
    truth for which role strings may be persisted to ``user.role``.
    Pinned here so that an accidental drift between ``map_roles``,
    ``_sanitize_role`` and the column-default contract is caught.
    """

    def test_is_a_frozenset(self):
        from engine.api.auth.base import ALLOWED_ROLES

        assert isinstance(ALLOWED_ROLES, frozenset)

    def test_contains_all_documented_roles(self):
        from engine.api.auth.base import ALLOWED_ROLES

        for role in (
            "viewer",
            "user",
            "retail_trader",
            "quant_dev",
            "developer",
            "portfolio_manager",
            "admin",
        ):
            assert role in ALLOWED_ROLES, (
                f"'{role}' must be in ALLOWED_ROLES — it is documented "
                "in the role_priority map of IAuthProvider.map_roles."
            )

    def test_does_not_contain_aliases(self):
        """No implicit-promotion aliases leaked into the allow-list."""
        from engine.api.auth.base import ALLOWED_ROLES

        # Pre-SEV-741 these were silently translated; they must not
        # appear as first-class roles now.
        assert "root" not in ALLOWED_ROLES
        assert "superuser" not in ALLOWED_ROLES
        assert "" not in ALLOWED_ROLES


# ---------------------------------------------------------------------------
# 10. _sanitize_role pipeline (NFKC -> BiDi strip -> allow-list)
# ---------------------------------------------------------------------------


class TestSanitizeRole:
    """``_sanitize_role`` is the input-validation gate that every IdP
    claim must pass through before being persisted to ``user.role``.
    The pipeline (NFKC normalize, strip BiDi/zero-width, lowercase +
    strip, allow-list check) is pinned case-by-case below.
    """

    def test_passes_through_recognized_role(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("admin") == "admin"
        assert _sanitize_role("user") == "user"
        assert _sanitize_role("viewer") == "viewer"
        assert _sanitize_role("developer") == "developer"

    def test_lowercases_input(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("ADMIN") == "admin"
        assert _sanitize_role("Admin") == "admin"
        assert _sanitize_role("QuAnT_dEv") == "quant_dev"

    def test_strips_whitespace(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("  admin  ") == "admin"
        assert _sanitize_role("\tadmin\n") == "admin"

    def test_unknown_role_collapses_to_user(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("superuser") == "user"
        assert _sanitize_role("root") == "user"
        assert _sanitize_role("not-a-real-role") == "user"

    def test_empty_string_collapses_to_user(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("") == "user"
        assert _sanitize_role("   ") == "user"

    def test_non_string_input_collapses_to_user(self):
        """Defensive: ``None``, ``int``, ``list`` from a hostile IdP
        must not crash the auth flow."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(None) == "user"
        assert _sanitize_role(42) == "user"
        assert _sanitize_role(["admin"]) == "user"
        assert _sanitize_role({"role": "admin"}) == "user"

    def test_nfkc_collapses_fullwidth_homoglyphs(self):
        """The fullwidth form of "admin" (codepoints U+FF41-U+FF4E) must
        NFKC-normalize to ASCII "admin" and pass the allow-list — but
        ONLY the canonical ASCII form is what gets persisted, so a
        downstream equality check against the allow-list never sees the
        original glyph."""
        from engine.api.auth.base import _sanitize_role

        fullwidth_admin = "\uff41\uff44\uff4d\uff49\uff4e"  # fullwidth "admin"
        assert _sanitize_role(fullwidth_admin) == "admin"

    def test_nfkc_collapses_superscript_digits_silently(self):
        """A role like 'admin²' (using a superscript 2) is not in the
        allow-list, so it collapses to 'user'. The point of this
        test is that NFKC runs first without raising — superscript 2
        normalizes to '2', producing 'admin2', which is correctly
        rejected by the allow-list."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("admin\uda00\udc32" if False else "admin\u00b2") == "user"

    def test_strips_bidi_left_to_right_override(self):
        """U+202D (LRO) must be stripped so an attacker can't hide
        'admin' inside what renders as 'nimda' (or anything else)
        in logs."""
        from engine.api.auth.base import _sanitize_role

        lro = "\u202d"
        assert _sanitize_role(f"{lro}admin") == "admin"
        assert _sanitize_role(f"ad{lro}min") == "admin"

    def test_strips_bidi_right_to_left_override(self):
        """U+202E (RLO) — the classic Trojan Source / log-spoofing
        code point — must be stripped."""
        from engine.api.auth.base import _sanitize_role

        rlo = "\u202e"
        assert _sanitize_role(f"admin{rlo}") == "admin"
        assert _sanitize_role(f"{rlo}admin{rlo}") == "admin"

    def test_strips_full_bidi_override_range(self):
        """All code points in the \u202a-\u202e range must be stripped."""
        from engine.api.auth.base import _sanitize_role

        for cp in range(0x202A, 0x202E + 1):
            ch = chr(cp)
            assert _sanitize_role(f"admin{ch}") == "admin", (
                f"U+{cp:04X} must be stripped by _sanitize_role"
            )

    def test_strips_directional_isolates(self):
        """U+2066-U+2069 (LRI, RLI, FSI, PDI) must be stripped."""
        from engine.api.auth.base import _sanitize_role

        for cp in range(0x2066, 0x2069 + 1):
            ch = chr(cp)
            assert _sanitize_role(f"admin{ch}") == "admin", (
                f"U+{cp:04X} must be stripped by _sanitize_role"
            )

    def test_strips_zero_width_characters(self):
        """ZWSP, ZWNJ, ZWJ, WJ and friends must not survive."""
        from engine.api.auth.base import _sanitize_role

        for cp in range(0x200B, 0x200F + 1):
            ch = chr(cp)
            assert _sanitize_role(f"admin{ch}") == "admin", (
                f"U+{cp:04X} must be stripped by _sanitize_role"
            )

    def test_strips_line_and_paragraph_separators(self):
        """U+2028 / U+2029 would enable log-injection — strip them."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("admin\u2028user") == "user"  # 'admin user' not allowed
        assert _sanitize_role("admin\u2029user") == "user"

    def test_warning_fires_for_rejected_role(self, monkeypatch):
        """Operators need visibility when an IdP ships a junk role."""
        from engine.api.auth import base

        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, **kwargs})

        monkeypatch.setattr(base, "logger", _Stub())
        base._sanitize_role("superuser")
        assert any(c["event"] == "auth.sanitize_role.rejected" for c in calls)

    def test_warning_fires_for_non_string_input(self, monkeypatch):
        from engine.api.auth import base

        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, **kwargs})

        monkeypatch.setattr(base, "logger", _Stub())
        base._sanitize_role(None)
        assert any(
            c["event"] == "auth.sanitize_role.rejected" and c.get("reason") == "not_string"
            for c in calls
        )


# ---------------------------------------------------------------------------
# 11. _apply_role_mapping orchestration
# ---------------------------------------------------------------------------


class _FakeUser:
    """Lightweight stand-in for ``engine.db.models.User`` so the
    helper can be unit-tested without spinning up SQLAlchemy."""

    def __init__(self, *, role: str | None = "user", is_active: bool = True) -> None:
        self.role = role
        self.is_active = is_active
        self.id = "fake-id"


class _FakeDB:
    """``AsyncSession`` mock that just records flush calls."""

    def __init__(self) -> None:
        self.flush_calls = 0

    async def flush(self) -> None:
        self.flush_calls += 1


class TestApplyRoleMapping:
    """``_apply_role_mapping`` orchestrates sanitization, the opt-in
    overwrite decision, the audit log event, and the DB flush. Each
    of those responsibilities is pinned below.
    """

    async def test_existing_user_different_role_overwrites_when_opted_in(self):
        from engine.api.auth.base import _apply_role_mapping

        user = _FakeUser(role="user")
        db = _FakeDB()
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=True),
            is_new_user=False,
            provider_name="test",
            db=db,
        )
        assert user.role == "admin"
        assert db.flush_calls == 1

    async def test_existing_user_same_role_skips_overwrite(self):
        from engine.api.auth.base import _apply_role_mapping

        user = _FakeUser(role="admin")
        db = _FakeDB()
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=True),
            is_new_user=False,
            provider_name="test",
            db=db,
        )
        assert user.role == "admin"
        assert db.flush_calls == 0

    async def test_existing_user_different_role_preserved_when_opted_out(self):
        from engine.api.auth.base import _apply_role_mapping

        user = _FakeUser(role="user")
        db = _FakeDB()
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=False),
            is_new_user=False,
            provider_name="test",
            db=db,
        )
        assert user.role == "user"
        assert db.flush_calls == 0

    async def test_new_user_overwrites_when_opted_out(self):
        """``is_new_user=True`` short-circuits the opt-in check — a
        fresh insert must always land its mapped role."""
        from engine.api.auth.base import _apply_role_mapping

        user = _FakeUser(role=None)
        db = _FakeDB()
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=False),
            is_new_user=True,
            provider_name="test",
            db=db,
        )
        assert user.role == "admin"
        assert db.flush_calls == 1

    async def test_existing_user_with_none_role_requires_opt_in(self):
        """SEV-741 follow-up: an existing user with anomalous
        ``role=None`` still requires operator opt-in."""
        from engine.api.auth.base import _apply_role_mapping

        user = _FakeUser(role=None)
        db = _FakeDB()
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=False),
            is_new_user=False,
            provider_name="test",
            db=db,
        )
        assert user.role is None  # unchanged
        assert db.flush_calls == 0

    async def test_sanitize_runs_before_overwrite_decision(self):
        """Hostile input that survives the IdP claim must still be
        sanitized by ``_apply_role_mapping`` — an embedded BiDi
        override is stripped, leaving a clean 'admin' that then
        flows through the overwrite policy."""
        from engine.api.auth.base import _apply_role_mapping

        user = _FakeUser(role="user")
        await _apply_role_mapping(
            user,
            "\u202eadmin",
            _SettingsStub(overwrite=True),
            is_new_user=False,
            provider_name="test",
        )
        assert user.role == "admin"

    async def test_sanitize_collapses_unknown_role_to_user(self):
        from engine.api.auth.base import _apply_role_mapping

        user = _FakeUser(role="user")
        await _apply_role_mapping(
            user,
            "superuser",
            _SettingsStub(overwrite=True),
            is_new_user=False,
            provider_name="test",
        )
        # 'superuser' is not in ALLOWED_ROLES; sanitize collapses to
        # 'user'. Then 'user' == 'user' so overwrite is a no-op.
        assert user.role == "user"

    async def test_db_none_is_tolerated(self):
        """When called without a session (e.g. by tests / dry-runs),
        ``_apply_role_mapping`` must still mutate the user row in
        memory."""
        from engine.api.auth.base import _apply_role_mapping

        user = _FakeUser(role="user")
        await _apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=True),
            is_new_user=False,
            provider_name="test",
        )
        assert user.role == "admin"

    async def test_audit_log_event_fires_on_overwrite(self, monkeypatch):
        from engine.api.auth import base

        events: list[tuple[str, dict[str, object]]] = []

        class _Stub:
            def info(self, _event, **kwargs):
                events.append((_event, kwargs))

            def warning(self, _event, **kwargs):  # pragma: no cover
                events.append((_event, kwargs))

        monkeypatch.setattr(base, "logger", _Stub())
        user = _FakeUser(role="user")
        await base._apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=True),
            is_new_user=False,
            provider_name="oidc",
        )
        assert any(
            event == "auth.oidc.role_overwritten" and kw.get("previous_role") == "user"
            and kw.get("new_role") == "admin"
            for event, kw in events
        ), f"expected auth.oidc.role_overwritten in {events!r}"

    async def test_audit_log_event_silent_on_noop(self, monkeypatch):
        from engine.api.auth import base

        events: list[tuple[str, dict[str, object]]] = []

        class _Stub:
            def info(self, _event, **kwargs):
                events.append((_event, kwargs))

            def warning(self, _event, **kwargs):  # pragma: no cover
                events.append((_event, kwargs))

        monkeypatch.setattr(base, "logger", _Stub())
        user = _FakeUser(role="admin")
        await base._apply_role_mapping(
            user,
            "admin",
            _SettingsStub(overwrite=True),
            is_new_user=False,
            provider_name="oidc",
        )
        assert not any("role_overwritten" in e for e, _ in events), (
            f"no overwrite event expected for same-role case, got {events!r}"
        )


# ---------------------------------------------------------------------------
# 12. Provider-level: is_active check is BEFORE role-mutation
# ---------------------------------------------------------------------------


class TestProviderIsActiveOrdering:
    """SEV-741 follow-up: every federated provider must reject a
    disabled user BEFORE any role-mutation path runs. Mutating the
    role of a disabled account would silently pre-stage an
    escalation the moment the account is reactivated.
    """

    @pytest.mark.parametrize(
        ("module_path", "provider_factory"),
        [
            (
                "engine.api.auth.oidc",
                lambda: __import__(
                    "engine.api.auth.oidc", fromlist=["OIDCAuthProvider"]
                ).OIDCAuthProvider(),
            ),
            (
                "engine.api.auth.google",
                lambda: __import__(
                    "engine.api.auth.google", fromlist=["GoogleAuthProvider"]
                ).GoogleAuthProvider(),
            ),
            (
                "engine.api.auth.ldap",
                lambda: __import__(
                    "engine.api.auth.ldap", fromlist=["LDAPAuthProvider"]
                ).LDAPAuthProvider(),
            ),
            (
                "engine.api.auth.github_oauth",
                lambda: __import__(
                    "engine.api.auth.github_oauth", fromlist=["GitHubAuthProvider"]
                ).GitHubAuthProvider(),
            ),
        ],
    )
    def test_disabled_check_appears_before_apply_role_mapping(
        self, module_path, provider_factory
    ):
        """Source-order guard: in every provider's ``authenticate``
        method, the ``is_active`` branch must appear textually BEFORE
        the ``_apply_role_mapping`` call.
        """
        import inspect

        mod = __import__(module_path, fromlist=[module_path.rsplit(".", 1)[-1]])
        # Pull the authenticate method off the provider class.
        src = inspect.getsource(mod)
        # Use module source so we don't have to disassemble. The
        # ``is_active`` check uses the literal substring
        # ``not user.is_active``; the role-mutation goes through
        # ``_apply_role_mapping``.
        active_idx = src.find("not user.is_active")
        apply_idx = src.find("_apply_role_mapping(")
        assert active_idx != -1, (
            f"{module_path} must contain 'not user.is_active' check"
        )
        assert apply_idx != -1, (
            f"{module_path} must call _apply_role_mapping"
        )
        assert active_idx < apply_idx, (
            f"{module_path}: 'not user.is_active' check must appear "
            f"BEFORE _apply_role_mapping call (SEV-741 follow-up). "
            f"got is_active@{active_idx}, apply@{apply_idx}"
        )

    def test_no_redundant_is_active_check_at_bottom(self):
        """OIDC must not still have the ``if not user.is_active``
        guard at the bottom — it should appear exactly once per
        provider file (in its pre-mutation position)."""
        import inspect

        from engine.api.auth import oidc

        src = inspect.getsource(oidc)
        assert src.count("not user.is_active") == 1, (
            "OIDC should have exactly one 'not user.is_active' check "
            "(moved to before the role-mutation branch)."
        )
