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

    def test_empty_input_returns_viewer(self):
        """SEV-741 follow-up: least-privilege fallback.  An empty
        external role set must collapse to ``viewer`` (read-only) and
        **not** ``user``, which grants write access."""
        assert _ConcreteProvider().map_roles([]) == "viewer"

    def test_all_unrecognized_falls_back_to_viewer(self):
        """SEV-741 follow-up: when no external role matches a known
        internal role, we must fall back to ``viewer`` (least privilege)
        instead of ``user``."""
        assert (
            _ConcreteProvider().map_roles(["superuser", "root", "god"]) == "viewer"
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
        which is not a known role.  Should fall through to ``viewer``
        without crashing."""
        assert _ConcreteProvider().map_roles(["   "]) == "viewer"


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
        assert p.map_roles(["totally_bogus"]) == "viewer"
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
        # Least-privilege fallback still applies; the unrecognized-role
        # warning must NOT fire (no roles were unrecognized — there
        # were no roles at all).  A separate ``fallback_to_least_*
        # warning is emitted by the fallback path (covered elsewhere).
        assert p.map_roles([]) == "viewer"
        assert not any(
            c["event"] == "auth.map_roles.unrecognized_roles" for c in calls
        )

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
