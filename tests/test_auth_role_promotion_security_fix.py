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
        # Import is reflected in the module's source text.
        assert "_should_overwrite_role" in inspect.getsource(mod), (
            f"{module_path} must import _should_overwrite_role from "
            "engine.api.auth.base (SEV-741)."
        )
        # The provider class still exists.
        assert hasattr(mod, class_name)

    @pytest.mark.parametrize(
        "module_path",
        [
            "engine.api.auth.ldap",
            "engine.api.auth.oidc",
            "engine.api.auth.google",
            "engine.api.auth.github_oauth",
        ],
    )
    def test_provider_calls_helper_with_is_new_user_false(self, module_path):
        """SEV-741 follow-up: every federated provider reaches the
        helper only from its existing-user branch (the new-user branch
        creates the row directly without consulting the helper). The
        existing-user branch must pass ``is_new_user=False`` so that a
        NULL ``role`` column on an existing row is treated as a data
        anomaly requiring operator opt-in, not as "no prior role to
        preserve"."""
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        assert "is_new_user=False" in src, (
            f"{module_path} must call _should_overwrite_role with "
            "is_new_user=False in its existing-user branch "
            "(SEV-741 follow-up)."
        )


# ---------------------------------------------------------------------------
# 9. ALLOWED_ROLES frozenset + _sanitize_role helper (SEV-741 follow-up)
# ---------------------------------------------------------------------------


class TestAllowedRolesFrozenSet:
    """``ALLOWED_ROLES`` is the single source of truth for the set of
    internal role names a federated login may assert. It must:

    * be a ``frozenset`` (immutable, hashable, safe to default-arg);
    * contain every key in ``map_roles``'s ``role_priority`` table;
    * reject common attack payloads by their absence.
    """

    def test_allowed_roles_is_a_frozenset(self):
        from engine.api.auth.base import ALLOWED_ROLES

        assert isinstance(ALLOWED_ROLES, frozenset)

    def test_allowed_roles_contains_all_priority_keys(self):
        from engine.api.auth.base import ALLOWED_ROLES

        # Must match the priority table inside ``map_roles`` — these
        # are the only roles that can possibly be returned.
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
                f"{role!r} must be in ALLOWED_ROLES — it is a key in "
                "map_roles' role_priority table."
            )

    def test_dangerous_synonyms_are_excluded(self):
        """Defence-in-depth: ``root``/``superuser``/``god``/``sysadmin``
        must NOT be in the allow-list. If a future role expansion
        accidentally grants one of these names, this test fires."""
        from engine.api.auth.base import ALLOWED_ROLES

        for poison in ("root", "superuser", "god", "sysadmin", "su"):
            assert poison not in ALLOWED_ROLES


class TestSanitizeRoleHappyPath:
    """``_sanitize_role`` accepts every legitimate role spelling."""

    @pytest.mark.parametrize(
        "raw",
        [
            "viewer",
            "user",
            "retail_trader",
            "quant_dev",
            "developer",
            "portfolio_manager",
            "admin",
        ],
    )
    def test_canonical_role_returned_verbatim(self, raw):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(raw) == raw

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ADMIN", "admin"),
            ("Admin", "admin"),
            ("  Admin  ", "admin"),
            ("QuAnT_dEv", "quant_dev"),
            ("Portfolio_Manager", "portfolio_manager"),
        ],
    )
    def test_case_and_whitespace_tolerated(self, raw, expected):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(raw) == expected


class TestSanitizeRoleAttacksCollapseToUser:
    """Every attack vector collapses safely to ``"user"``."""

    def test_non_string_input_returns_user(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(None) == "user"
        assert _sanitize_role(123) == "user"
        assert _sanitize_role(["admin"]) == "user"

    def test_empty_string_returns_user(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("") == "user"

    def test_whitespace_only_returns_user(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("   ") == "user"
        assert _sanitize_role("\t") == "user"

    @pytest.mark.parametrize(
        "oversized",
        [
            "admin" + "x" * 28,  # 33 chars — just over the limit
            "a" * 100,
            "a" * 1024,
            "a" * 65536,
        ],
    )
    def test_oversized_input_rejected_before_regex(self, oversized):
        """SEV-741 follow-up requirement #3: an oversized payload must
        be rejected *immediately*, before the control-character regex
        or NFKC normalization runs. Otherwise a multi-MiB IdP claim
        could DoS the auth path."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(oversized) == "user"

    @pytest.mark.parametrize(
        "control_char",
        [
            "\x00",  # NUL
            "\x01",
            "\x05",
            "\n",  # LF — log-injection vector
            "\r",  # CR — log-injection vector
            "\t",  # HT
            "\x1b",  # ESC — terminal escape
            "\x7f",  # DEL
            "\x9c",  # C1 (8-bit control)
        ],
    )
    def test_control_characters_rejected(self, control_char):
        """Any C0/DEL/C1 byte in the input causes rejection. We do
        NOT silently strip — a payload that contained a control char
        was crafted and must not pass."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(f"admin{control_char}") == "user"
        assert _sanitize_role(f"{control_char}admin") == "user"

    def test_idp_asserting_admin_with_control_char_collapses_to_user(self):
        """SEV-741 follow-up required test #1: an IdP asserting a
        tampered ``admin`` (here with a NUL byte intended to confuse
        downstream C-string handling) must collapse to ``user``, NOT
        be silently accepted as ``admin`` after stripping."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("admin\x00") == "user"
        assert _sanitize_role("\x00admin") == "user"
        assert _sanitize_role("ad\x00min") == "user"

    @pytest.mark.parametrize(
        "fullwidth",
        [
            "ａｄｍｉｎ",  # U+FF41 U+FF44 U+FF4D U+FF49 U+FF4E
            "ｕｓｅｒ",  # fullwidth "user"
            "ｖｉｅｗｅｒ",  # fullwidth "viewer"
            "ｄｅｖｅｌｏｐｅｒ",  # fullwidth "developer"
            "ｑｕａｎｔ＿ｄｅｖ",  # fullwidth "quant_dev"
        ],
    )
    def test_fullwidth_unicode_rejected(self, fullwidth):
        """SEV-741 follow-up required test #2: fullwidth Unicode must
        NOT be NFKC-normalized to its ASCII lookalike and accepted.
        ``unicodedata.normalize("NFKC", "ａｄｍｉｎ") == "admin"``, but
        accepting the normalized form would let an attacker bypass
        log-matching / allow-list comparisons by visually-identical
        strings. ``_sanitize_role`` must detect the NFKC change and
        reject."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(fullwidth) == "user"

    @pytest.mark.parametrize(
        "homoglyph",
        [
            "аdmin",  # Cyrillic 'а' (U+0430), looks like ASCII 'a'
            "аdmіn",  # Cyrillic 'а' + 'і' (U+0456)
            "admın",  # dotless Latin 'ı' (NFKC-stable — caught by allow-list)
        ],
    )
    def test_other_homoglyphs_rejected(self, homoglyph):
        """Defence in depth: Cyrillic lookalikes are either caught by
        the NFKC-stability check or by the final allow-list match."""
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role(homoglyph) == "user"

    def test_unrecognized_role_name_collapses_to_user(self):
        from engine.api.auth.base import _sanitize_role

        assert _sanitize_role("superuser") == "user"
        assert _sanitize_role("root") == "user"
        assert _sanitize_role("god") == "user"
        assert _sanitize_role("sysadmin") == "user"


class TestSanitizeRoleUsedByMapRoles:
    """End-to-end: ``map_roles`` actually routes input through
    ``_sanitize_role``. A tampered ``admin`` claim must collapse to
    ``"user"`` at the API boundary, not be silently accepted."""

    def test_admin_with_nul_collapses_to_user_via_map_roles(self):
        from engine.api.auth.base import IAuthProvider

        class _P(IAuthProvider):
            @property
            def name(self) -> str:
                return "t"

            async def authenticate(self, **_):
                from engine.api.auth.base import AuthResult

                return AuthResult()

        assert _P().map_roles(["admin\x00"]) == "user"

    def test_fullwidth_admin_collapses_to_user_via_map_roles(self):
        from engine.api.auth.base import IAuthProvider

        class _P(IAuthProvider):
            @property
            def name(self) -> str:
                return "t"

            async def authenticate(self, **_):
                from engine.api.auth.base import AuthResult

                return AuthResult()

        assert _P().map_roles(["ａｄｍｉｎ"]) == "user"

    def test_oversized_role_collapses_to_user_via_map_roles(self):
        from engine.api.auth.base import IAuthProvider

        class _P(IAuthProvider):
            @property
            def name(self) -> str:
                return "t"

            async def authenticate(self, **_):
                from engine.api.auth.base import AuthResult

                return AuthResult()

        assert _P().map_roles(["admin" + "x" * 100]) == "user"

    def test_tampered_admin_does_not_silently_overwrite_recognized(
        self, monkeypatch
    ):
        """If a tampered ``admin`` is sent alongside a valid role, the
        valid role still wins (the tampered string is unrecognized)."""
        from engine.api.auth.base import IAuthProvider

        class _P(IAuthProvider):
            @property
            def name(self) -> str:
                return "t"

            async def authenticate(self, **_):
                from engine.api.auth.base import AuthResult

                return AuthResult()

        # "admin\x00" is unrecognized; "viewer" wins.
        assert _P().map_roles(["admin\x00", "viewer"]) == "viewer"


# ---------------------------------------------------------------------------
# 10. _should_overwrite_role: NULL-role on an existing user (SEV-741 follow-up)
# ---------------------------------------------------------------------------


class TestShouldOverwriteRoleNullExistingUser:
    """SEV-741 follow-up requirement #4: an existing user with a NULL
    ``role`` column is *not* the same as a brand-new user. A NULL role
    on an existing row is a data anomaly — it might be the result of a
    botched migration, a manual DB edit, or an attacker who found a
    way to clear the column. Letting a federated login silently fill
    it in would hand the IdP a privileged escalation path.

    The new ``is_new_user`` keyword distinguishes the two cases:

    * ``is_new_user=True`` (default, backward compatible): brand-new
      row being created right now, ``current_role is None`` is
      expected, helper returns ``True`` unconditionally.
    * ``is_new_user=False``: row already exists, ``current_role is
      None`` is anomalous, helper defers to
      ``auth_overwrite_role_on_login``.
    """

    def _call(self, current_role, mapped_role, *, overwrite: bool, is_new_user: bool = True):
        from engine.api.auth.base import _should_overwrite_role

        return _should_overwrite_role(
            current_role,
            mapped_role,
            _SettingsStub(overwrite=overwrite),
            is_new_user=is_new_user,
        )

    # --- backward compatibility: is_new_user defaults to True -------

    def test_default_is_new_user_preserves_old_behavior_opted_in(self):
        """Old call sites that don't pass ``is_new_user`` keep the
        original semantics: ``None`` -> True."""
        assert self._call(None, "admin", overwrite=True) is True

    def test_default_is_new_user_preserves_old_behavior_opted_out(self):
        assert self._call(None, "admin", overwrite=False) is True

    # --- required test: NULL-role existing user blocked without opt-in

    def test_null_role_existing_user_blocked_without_opt_in(self):
        """SEV-741 follow-up required test #3: a row that already
        exists but has ``role=None`` must NOT be silently repaired by
        the IdP-mapped role when the operator hasn't opted in."""
        assert (
            self._call(None, "admin", overwrite=False, is_new_user=False)
            is False
        )

    def test_null_role_existing_user_blocked_even_for_user_role(self):
        """Even mapping to the lowest-privilege ``user`` role must
        respect the opt-in — otherwise an IdP that asserts *anything*
        could anchor itself into a previously-anomalous row."""
        assert (
            self._call(None, "user", overwrite=False, is_new_user=False)
            is False
        )

    def test_null_role_existing_user_allowed_with_opt_in(self):
        """With operator opt-in, the helper permits the repair — the
        auditor can trace it back to the explicit setting."""
        assert (
            self._call(None, "admin", overwrite=True, is_new_user=False)
            is True
        )

    def test_null_role_existing_user_demotion_blocked_without_opt_in(self):
        """Same guard protects against demotion-style attacks: an
        existing NULL row cannot be filled with ``user`` to lock in
        a low-privilege foothold."""
        assert (
            self._call(None, "user", overwrite=False, is_new_user=False)
            is False
        )

    def test_null_role_existing_user_missing_setting_blocks(self):
        """If the config object doesn't expose the setting at all,
        default to safe (block)."""
        from engine.api.auth.base import _should_overwrite_role

        class _Bare:
            pass

        assert (
            _should_overwrite_role(None, "admin", _Bare(), is_new_user=False)
            is False
        )

    def test_null_role_existing_user_truthy_setting_allows(self):
        from engine.api.auth.base import _should_overwrite_role

        class _Truthy:
            auth_overwrite_role_on_login = 1

        assert (
            _should_overwrite_role(None, "admin", _Truthy(), is_new_user=False)
            is True
        )

    def test_distinction_between_new_and_existing_with_null_role(self):
        """Pin the semantic difference: same arguments, flip
        ``is_new_user``, behavior flips."""
        assert self._call(None, "admin", overwrite=False, is_new_user=True) is True
        assert (
            self._call(None, "admin", overwrite=False, is_new_user=False)
            is False
        )


class TestProvidersPassIsNewUserFalse:
    """Static guard: each federated provider's existing-user branch
    must call ``_should_overwrite_role(..., is_new_user=False)`` so
    that the NULL-role protection above is actually applied to real
    logins."""

    @pytest.mark.parametrize(
        "module_path",
        [
            "engine.api.auth.ldap",
            "engine.api.auth.oidc",
            "engine.api.auth.google",
            "engine.api.auth.github_oauth",
        ],
    )
    def test_existing_user_branch_signals_existing_user(self, module_path):
        import importlib
        import inspect

        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        assert "is_new_user=False" in src, (
            f"{module_path} must pass is_new_user=False when calling "
            "_should_overwrite_role from its existing-user branch."
        )
