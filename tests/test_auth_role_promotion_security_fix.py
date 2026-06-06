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
2. ``auth_overwrite_role_on_login`` defaults to ``False`` **and** is
   honored by the federated login providers (LDAP/OIDC) so existing
   users' roles are not silently mutated on each login.
3. A warning is emitted for **any** unrecognized external role (not
   only when the entire set is unrecognized).
4. The least-privilege fallback for an empty / fully-unrecognized
   role list is ``viewer`` (not ``user``).
5. Unrecognized role strings are sanitized (control characters
   stripped, length capped) before being written to log records to
   prevent log injection.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.api.auth.base import AuthResult, IAuthProvider
from engine.config import Settings


@pytest.fixture
def rsa_keys():
    """Build an RSA keypair used to sign id_tokens in the OIDC tests."""
    from tests.test_oidc_auth import _generate_rsa_key_pair

    return _generate_rsa_key_pair()


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
        """Least-privilege fallback (SEV-741 follow-up): an empty role
        list must collapse to ``viewer`` (the lowest-privilege role)
        rather than ``user``, which carries write privileges."""
        assert _ConcreteProvider().map_roles([]) == "viewer"

    def test_all_unrecognized_falls_back_to_viewer(self):
        """Same least-privilege principle applies when every supplied
        role is unrecognized."""
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
        (least privilege) without crashing."""
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
        assert p.map_roles([]) == "viewer"
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

    def test_warning_mapped_is_viewer_when_nothing_recognized(self, monkeypatch):
        """The ``mapped=`` payload must report ``viewer`` when no
        recognized role is present (least-privilege fallback)."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        assert p.map_roles(["totally_bogus"]) == "viewer"
        assert calls
        assert calls[0]["mapped"] == "viewer"


# ---------------------------------------------------------------------------
# 3b. Sanitization of unrecognized roles before logging
# ---------------------------------------------------------------------------


class TestUnrecognizedRoleSanitization:
    """Unrecognized role strings must be sanitized (control characters
    stripped, length capped) before they reach log records.  Without
    this an attacker who controls upstream IdP group names could embed
    CR/LF sequences to forge additional log lines (log injection) or
    feed pathologically long strings to bloat log storage.
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

    def test_control_chars_stripped_from_unrecognized_role(self, monkeypatch):
        """Carriage-return / line-feed / NUL must be removed before the
        role string is logged, defeating classic log-injection."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        # A "bogus" role whose name embeds CR/LF — without sanitization
        # the CR/LF would appear verbatim in the log line and could be
        # mistaken for a separate log event by an aggregator.
        p.map_roles(["bogus\r\nFAKE_EVENT=privileged"])
        assert calls
        unrecognized = calls[0]["unrecognized"]
        assert len(unrecognized) == 1
        logged = unrecognized[0]
        assert "\r" not in logged
        assert "\n" not in logged
        assert "FAKE_EVENT" in logged  # the benign tail is preserved

    def test_nul_byte_stripped(self, monkeypatch):
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["ev\x00il"])
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert "\x00" not in logged

    def test_terminal_escape_stripped(self, monkeypatch):
        """ESC (0x1B) and other C0 control characters must be removed."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["x\x1b[31mred"])
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert "\x1b" not in logged

    def test_length_capped(self, monkeypatch):
        """A pathologically long role string must be truncated in the
        log payload (with an ellipsis) so that an attacker cannot
        bloat log storage by feeding oversized IdP group DN strings."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        huge = "A" * 10_000
        p.map_roles([huge])
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert len(logged) <= 300  # 256 + ellipsis + small slack
        assert logged.endswith("…")

    def test_benign_role_strings_logged_verbatim(self, monkeypatch):
        """Sanitization must be a pure function for benign inputs —
        ordinary ASCII names must survive unchanged."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["stale_group"])
        assert calls
        assert calls[0]["unrecognized"] == ["stale_group"]

    def test_tab_preserved_in_role_string(self, monkeypatch):
        """Horizontal tab (0x09) is generally benign in log records and
        is preserved by sanitization."""
        calls = self._patch(monkeypatch)
        p = _ConcreteProvider()
        p.map_roles(["a\tb"])
        assert calls
        logged = calls[0]["unrecognized"][0]
        assert "\t" in logged

    def test_non_string_input_is_coerced(self):
        """If an upstream provider somehow passes a non-string role
        (e.g. an int from a mis-parsed JWT claim), sanitization must
        coerce it to ``str`` rather than raise."""
        # The provider is expected to feed strings; this is a defensive
        # check on the sanitizer only.
        from engine.api.auth.base import _sanitize_role_for_log

        sanitized = _sanitize_role_for_log(12345)  # type: ignore[arg-type]
        assert sanitized == "12345"


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
# 4b. auth_overwrite_role_on_login gating in the federated login flow
# ---------------------------------------------------------------------------


class TestAuthOverwriteRoleOnLoginGating:
    """Verify that ``auth_overwrite_role_on_login`` is honored by both
    federated providers (LDAP, OIDC) when an existing user logs back in
    and the IdP now asserts a different role.

    With the flag at its default (False) the persisted role must be
    preserved.  When the flag is explicitly set to True the persisted
    role may be updated.
    """

    @pytest.fixture
    def ldap_existing_user(self):
        from engine.db.models import User

        return User(
            email="existing@example.com",
            display_name="Existing",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="existing",
        )

    @pytest.fixture
    def oidc_existing_user(self):
        from engine.db.models import User

        return User(
            email="existing@example.com",
            display_name="Existing",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="existing-oidc",
        )

    @staticmethod
    def _ldap_mocks_for_admin_group():
        """Build LDAP module mocks that return an "admins" group for the
        authenticated user.  Used to drive the IdP-asserts-higher-role
        scenario in the gating tests."""
        from unittest.mock import MagicMock

        fake_conn = MagicMock()
        fake_conn.simple_bind_s = MagicMock(return_value=None)
        fake_conn.search_s = MagicMock(
            return_value=[
                (
                    "uid=existing,ou=users,dc=example,dc=com",
                    {
                        "uid": [b"existing"],
                        "mail": [b"existing@example.com"],
                        "cn": [b"Existing"],
                        "memberOf": [b"cn=admins,ou=groups,dc=example,dc=com"],
                    },
                )
            ]
        )
        fake_conn.unbind_s = MagicMock(return_value=None)

        fake_ldap = MagicMock()
        fake_ldap.initialize = MagicMock(return_value=fake_conn)
        fake_ldap.SCOPE_SUBTREE = 2
        fake_ldap.OPT_NETWORK_TIMEOUT = 1
        fake_ldap.OPT_TIMEOUT = 2
        fake_filter = MagicMock()
        fake_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
        return fake_ldap, fake_filter

    @staticmethod
    def _ldap_role_mapping_json() -> str:
        import json

        return json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
        })

    async def test_ldap_does_not_overwrite_when_flag_false(
        self, monkeypatch, ldap_existing_user
    ):
        """Default behavior: an existing user's role is preserved even
        when the IdP now asserts a higher-privilege role."""
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.api.auth.ldap import LDAPAuthProvider

        # Disable the flag explicitly (default) and configure the LDAP
        # role mapping so the "admins" group maps to the "admin" role.
        s = Settings(
            _env_file=None,
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping=self._ldap_role_mapping_json(),
        )
        assert s.auth_overwrite_role_on_login is False
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        provider = LDAPAuthProvider()
        fake_ldap, fake_filter = self._ldap_mocks_for_admin_group()

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ldap_existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        import sys

        with (
            patch.dict(sys.modules, {"ldap": fake_ldap, "ldap.filter": fake_filter}),
        ):
            result = await provider.authenticate(
                username="existing", password="irrelevant", db=mock_db
            )

        assert result.success is True
        # Existing user's role must NOT be mutated when flag is False.
        assert ldap_existing_user.role == "user"
        mock_db.flush.assert_not_called()

    async def test_ldap_overwrites_when_flag_true(
        self, monkeypatch, ldap_existing_user
    ):
        """Opt-in: when ``auth_overwrite_role_on_login`` is True the
        provider updates the stored role to match the IdP assertion."""
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.api.auth.ldap import LDAPAuthProvider

        s = Settings(
            _env_file=None,
            auth_overwrite_role_on_login=True,
            ldap_server_url="ldap://ldap.example.com:389",
            ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
            ldap_search_base="ou=users,dc=example,dc=com",
            ldap_role_mapping=self._ldap_role_mapping_json(),
        )
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        provider = LDAPAuthProvider()
        fake_ldap, fake_filter = self._ldap_mocks_for_admin_group()

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ldap_existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        import sys

        with (
            patch.dict(sys.modules, {"ldap": fake_ldap, "ldap.filter": fake_filter}),
        ):
            result = await provider.authenticate(
                username="existing", password="irrelevant", db=mock_db
            )

        assert result.success is True
        assert ldap_existing_user.role == "admin"
        mock_db.flush.assert_called()

    async def test_oidc_does_not_overwrite_when_flag_false(
        self, monkeypatch, oidc_existing_user, rsa_keys
    ):
        """Default behavior: an existing OIDC user's role is preserved
        even when the IdP now asserts a higher-privilege role."""
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.api.auth.oidc import OIDCAuthProvider

        s = Settings(
            _env_file=None,
            oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
            oidc_client_id="test-client-id",
            oidc_client_secret="secret",
            oidc_redirect_uri="https://app.example.com/callback",
            oidc_role_claim="groups",
        )
        assert s.auth_overwrite_role_on_login is False
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)

        provider = OIDCAuthProvider()

        from tests.test_oidc_auth import _build_full_mock_client

        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "existing-oidc",
                "email": "existing@example.com",
                "name": "Existing",
                "groups": ["admin"],
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = oidc_existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await provider.authenticate(code="code", db=mock_db)

        assert result.success is True
        assert oidc_existing_user.role == "user"
        mock_db.flush.assert_not_called()

    async def test_oidc_overwrites_when_flag_true(
        self, monkeypatch, oidc_existing_user, rsa_keys
    ):
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from engine.api.auth.oidc import OIDCAuthProvider

        s = Settings(
            _env_file=None,
            auth_overwrite_role_on_login=True,
            oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
            oidc_client_id="test-client-id",
            oidc_client_secret="secret",
            oidc_redirect_uri="https://app.example.com/callback",
            oidc_role_claim="groups",
        )
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)

        provider = OIDCAuthProvider()

        from tests.test_oidc_auth import _build_full_mock_client

        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "existing-oidc",
                "email": "existing@example.com",
                "name": "Existing",
                "groups": ["admin"],
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = oidc_existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await provider.authenticate(code="code", db=mock_db)

        assert result.success is True
        assert oidc_existing_user.role == "admin"
        mock_db.flush.assert_called()


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
