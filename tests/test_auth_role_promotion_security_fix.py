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
    import the centralized role-mapping helper and must not mutate
    ``user.role`` directly. Catches accidental revert / re-implementation
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
    def test_provider_imports_apply_role_mapping(self, module_path, class_name):
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        # Import is reflected in the module's source text.
        assert "_apply_role_mapping" in inspect.getsource(mod), (
            f"{module_path} must import _apply_role_mapping from "
            "engine.api.auth.base (SEV-741 centralized policy)."
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
    def test_provider_does_not_directly_mutate_user_role(
        self, module_path, class_name
    ):
        """After the SEV-741 refactor, providers must not assign to
        ``user.role`` on the existing-user path — the centralized
        helper owns the mutation. (New-user creation via the ``User()``
        constructor is excluded; that path legitimately sets the
        initial role.)"""
        import importlib
        import inspect
        import re

        mod = importlib.import_module(module_path)
        source = inspect.getsource(mod)

        # Strip any new-user ``User(role=mapped_role, ...)`` block
        # before grepping — the constructor legitimately sets the
        # initial role and is out of scope for this guard.
        sanitized = re.sub(r"User\([^)]*\)", "User(...)", source, flags=re.DOTALL)

        assert "user.role =" not in sanitized, (
            f"{module_path} must not assign to user.role directly; route "
            "the overwrite through _apply_role_mapping instead."
        )
        assert hasattr(mod, class_name)


# ---------------------------------------------------------------------------
# 9. Centralized _apply_role_mapping helper (SEV-741 follow-up)
# ---------------------------------------------------------------------------


class _UserStub:
    """Minimal stand-in for ``engine.db.models.User`` so the helper
    can be exercised without spinning up SQLAlchemy."""

    def __init__(
        self,
        *,
        role: str | None = "user",
        auth_provider: str = "test",
        user_id: int = 1,
    ) -> None:
        self.role = role
        self.auth_provider = auth_provider
        self.id = user_id


class TestApplyRoleMappingHelper:
    """``_apply_role_mapping`` is the single entry point providers use
    to mutate ``user.role`` on a federated login. Pinned here in
    isolation so the policy can be reviewed independently of the
    providers that consume it."""

    def _call(self, user, mapped_role, *, overwrite: bool):
        from engine.api.auth.base import _apply_role_mapping

        return _apply_role_mapping(
            user, mapped_role, _SettingsStub(overwrite=overwrite)
        )

    def test_returns_true_and_mutates_when_overwrite_allowed(self):
        u = _UserStub(role="user")
        assert self._call(u, "admin", overwrite=True) is True
        assert u.role == "admin"

    def test_returns_false_and_preserves_when_overwrite_blocked(self):
        u = _UserStub(role="user")
        assert self._call(u, "admin", overwrite=False) is False
        assert u.role == "user"

    def test_same_role_is_a_noop_even_when_opted_in(self):
        """Skip the audit event + write when nothing would change."""
        u = _UserStub(role="admin")
        assert self._call(u, "admin", overwrite=True) is False
        assert u.role == "admin"

    def test_emits_audit_event_when_overwriting(self, monkeypatch):
        """Successful overwrites must emit the structured log event so
        operators can audit / alert on IdP-driven role changes."""
        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def warning(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, "level": "warning", **kwargs})

        from engine.api.auth import base

        monkeypatch.setattr(base, "logger", _Stub())

        u = _UserStub(role="user", auth_provider="ldap", user_id=42)
        assert self._call(u, "admin", overwrite=True) is True
        assert calls, "expected an audit event"
        event = calls[0]
        assert event["event"] == "auth.role_overwritten"
        assert event["previous_role"] == "user"
        assert event["new_role"] == "admin"
        assert event["provider"] == "ldap"
        assert event["user_id"] == "42"

    def test_no_audit_event_when_blocked(self, monkeypatch):
        calls: list[dict[str, object]] = []

        class _Stub:
            def info(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, **kwargs})

            def warning(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, **kwargs})

        from engine.api.auth import base

        monkeypatch.setattr(base, "logger", _Stub())

        u = _UserStub(role="user")
        assert self._call(u, "admin", overwrite=False) is False
        assert calls == []

    def test_demotion_allowed_when_opted_in(self):
        u = _UserStub(role="admin")
        assert self._call(u, "user", overwrite=True) is True
        assert u.role == "user"

    def test_demotion_blocked_when_opted_out(self):
        u = _UserStub(role="admin")
        assert self._call(u, "user", overwrite=False) is False
        assert u.role == "admin"


# ---------------------------------------------------------------------------
# 10. Role sanitization: _CONTROL_CHARS_RE / _MAX_ROLE_LENGTH
# ---------------------------------------------------------------------------


class TestRoleSanitization:
    """Defence-in-depth on IdP-asserted role strings: control chars
    are stripped and over-long strings are truncated before the role
    is considered for persistence. Prevents log-bombing and hidden
    character smuggling through the audit trail."""

    def test_control_chars_regex_matches_c0_range(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        # NUL through US (0x00..0x1F) — every byte in the C0 range.
        for code in range(0x20):
            assert _CONTROL_CHARS_RE.search(chr(code)), (
                f"C0 control U+{code:04X} must match _CONTROL_CHARS_RE"
            )

    def test_control_chars_regex_matches_del_and_c1_range(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        # DEL (U+007F) and C1 controls (U+0080..U+009F).
        for code in range(0x7F, 0xA0):
            assert _CONTROL_CHARS_RE.search(chr(code)), (
                f"DEL/C1 control U+{code:04X} must match _CONTROL_CHARS_RE"
            )

    def test_control_chars_regex_matches_common_invisibles(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        # Zero-width / bidi-override characters enumerated in the regex.
        for code in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
                     0x202E, 0xFEFF):
            assert _CONTROL_CHARS_RE.search(chr(code)), (
                f"Unicode invisible U+{code:04X} must match _CONTROL_CHARS_RE"
            )

    def test_control_chars_regex_does_not_match_printable_ascii(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        # Printable ASCII (space..tilde) must pass through unchanged.
        for code in range(0x20, 0x7F):
            assert not _CONTROL_CHARS_RE.search(chr(code))

    def test_control_chars_regex_does_not_match_normal_letters(self):
        from engine.api.auth.base import _CONTROL_CHARS_RE

        assert not _CONTROL_CHARS_RE.search("admin")
        assert not _CONTROL_CHARS_RE.search("portfolio_manager")
        # Non-ASCII printable letters (e.g. accented Latin) must NOT
        # match — they are legitimate in some IdP role names.
        assert not _CONTROL_CHARS_RE.search("rôle")

    def test_sanitize_strips_control_characters(self):
        from engine.api.auth.base import _sanitize_role

        # NUL embedded between two valid bytes — must be stripped.
        assert _sanitize_role("ad\x00min") == "admin"
        # Tab + LF injection attempt — must be stripped.
        assert _sanitize_role("admin\t\n") == "admin"
        # Zero-width space prepended — must be stripped.
        assert _sanitize_role("\u200badmin") == "admin"
        # BOM + RTL override — must be stripped.
        assert _sanitize_role("\ufeff\u202eadmin") == "admin"

    def test_sanitize_truncates_overlong_strings(self):
        from engine.api.auth.base import _MAX_ROLE_LENGTH, _sanitize_role

        # _MAX_ROLE_LENGTH is the cap; verify the constant is sensible
        # (it must comfortably exceed the longest legitimate role).
        assert len("portfolio_manager") <= _MAX_ROLE_LENGTH
        # An input longer than the cap is truncated.
        long_input = "x" * (_MAX_ROLE_LENGTH + 10)
        assert len(_sanitize_role(long_input)) == _MAX_ROLE_LENGTH
        # An input at exactly the cap survives intact.
        exact = "y" * _MAX_ROLE_LENGTH
        assert _sanitize_role(exact) == exact

    def test_sanitize_collapses_whitespace_only_to_default(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("   ") == "user"
        assert _sanitize_role("\t\n") == "user"

    def test_sanitize_handles_non_string_input_defensively(self):
        from engine.api.auth.base import _sanitize_role

        # A misconfigured IdP could push a list / int / None — collapse
        # to the safe default instead of raising.
        assert _sanitize_role(None) == "user"
        assert _sanitize_role(123) == "user"
        assert _sanitize_role(["admin"]) == "user"

    def test_sanitize_strips_then_truncates(self):
        from engine.api.auth.base import _MAX_ROLE_LENGTH, _sanitize_role

        # Control chars embedded inside an overlong string: both
        # transformations apply, in the right order (strip first,
        # then truncate).
        noisy = ("a\x00" * (_MAX_ROLE_LENGTH + 5)) + "tail"
        cleaned = _sanitize_role(noisy)
        assert len(cleaned) == _MAX_ROLE_LENGTH
        assert "\x00" not in cleaned

    def test_apply_role_mapping_sanitizes_before_deciding(self):
        """Sanitization runs *before* the overwrite-or-skip decision,
        so a hostile IdP cannot smuggle a hidden byte past the
        same-role short-circuit."""
        from engine.api.auth.base import _apply_role_mapping

        u = _UserStub(role="admin")
        # Same visible bytes as ``u.role`` but with a NUL injected —
        # must NOT short-circuit as 'same role'; after sanitization
        # the role still matches, so no overwrite fires. The point is
        # that the comparison happens on the cleaned value.
        assert _apply_role_mapping(u, "admin\x00", _SettingsStub(overwrite=True)) is False
        assert u.role == "admin"

    def test_apply_role_mapping_persists_sanitized_value(self):
        from engine.api.auth.base import _apply_role_mapping

        u = _UserStub(role="user")
        # A role with embedded control chars must be cleaned before
        # being persisted to user.role.
        assert _apply_role_mapping(
            u, "ad\x00min", _SettingsStub(overwrite=True)
        ) is True
        assert u.role == "admin"

