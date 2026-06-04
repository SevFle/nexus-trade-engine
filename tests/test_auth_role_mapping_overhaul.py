"""Comprehensive tests for the auth role-mapping overhaul.

Covers the four pillars of the change:

1. ``_ROLE_PROMOTIONS`` has been removed and ``map_roles`` no longer silently
   elevates external claims (e.g. ``viewer`` → ``user``, ``quant_dev`` →
   ``developer``). External claims are faithfully reflected.
2. ``Settings.auth_overwrite_role_on_login`` defaults to ``False`` and is
   honoured by both LDAP and OIDC providers — existing users keep their role
   on subsequent logins unless the operator explicitly opts in.
3. A warning log is emitted for *every* unrecognized role in external claims
   (not just when all roles are unrecognized).
4. The legacy ``test_map_roles_new_domain_roles`` semantics are updated to
   reflect the no-silent-promotion contract.

These tests run against the actual ``IAuthProvider.map_roles`` implementation
as well as the concrete LDAP/OIDC providers so that end-to-end behaviour is
verified.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import AuthResult, IAuthProvider
from engine.api.auth.ldap import LDAPAuthProvider
from engine.api.auth.oidc import OIDCAuthProvider
from engine.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConcreteProvider(IAuthProvider):
    """Minimal concrete provider for unit-testing IAuthProvider behaviour."""

    @property
    def name(self) -> str:
        return "concrete-test"

    async def authenticate(self, **_kwargs):
        return AuthResult(success=True)


def _make_settings(**overrides) -> Settings:
    """Build a Settings instance with sensible LDAP/OIDC defaults."""
    defaults: dict = {
        "ldap_server_url": "ldap://ldap.example.com:389",
        "ldap_bind_dn": "uid={{username}},ou=users,dc=example,dc=com",
        "ldap_search_base": "ou=users,dc=example,dc=com",
        "ldap_role_mapping": json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=developers,ou=groups,dc=example,dc=com": "developer",
            "cn=quant,ou=groups,dc=example,dc=com": "quant_dev",
        }),
        "oidc_discovery_url": "https://id.example.com/.well-known/openid-configuration",
        "oidc_client_id": "test-client-id",
        "oidc_client_secret": "test-client-secret",
        "oidc_redirect_uri": "https://app.example.com/callback",
        "oidc_role_claim": "roles",
    }
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# Section 1 — _ROLE_PROMOTIONS is gone and map_roles does not silently elevate
# ---------------------------------------------------------------------------


class TestRolePromotionsMapRemoved:
    """The _ROLE_PROMOTIONS dictionary must not exist anywhere in the codebase."""

    def test_role_promotions_map_does_not_exist_in_base_module(self):
        import engine.api.auth.base as base_mod

        assert not hasattr(base_mod, "_ROLE_PROMOTIONS"), (
            "_ROLE_PROMOTIONS map must be removed from engine.api.auth.base"
        )

    def test_no_silent_viewer_to_user_promotion(self):
        p = _ConcreteProvider()
        assert p.map_roles(["viewer"]) == "viewer"

    def test_no_silent_quant_dev_to_developer_promotion(self):
        p = _ConcreteProvider()
        assert p.map_roles(["quant_dev"]) == "quant_dev"

    def test_no_silent_promotion_when_mixed_with_higher_role(self):
        """When mixing previously-promoted roles with a higher canonical role,
        the higher role wins on merit — not because of promotion."""
        p = _ConcreteProvider()
        # viewer + admin → admin (admin wins because priority 6 > 0, not via promotion)
        assert p.map_roles(["viewer", "admin"]) == "admin"
        # quant_dev + portfolio_manager → portfolio_manager (5 > 3)
        assert p.map_roles(["quant_dev", "portfolio_manager"]) == "portfolio_manager"

    def test_faithful_reflection_of_recognized_roles(self):
        """Every canonical role, when passed alone, maps to itself."""
        p = _ConcreteProvider()
        for role in ("viewer", "user", "retail_trader", "quant_dev",
                     "developer", "portfolio_manager", "admin"):
            assert p.map_roles([role]) == role

    def test_priority_ordering_unaffected_by_promotion_removal(self):
        p = _ConcreteProvider()
        assert p.map_roles(["user", "admin", "developer"]) == "admin"
        assert p.map_roles(["user", "developer"]) == "developer"
        assert p.map_roles(["retail_trader", "portfolio_manager"]) == "portfolio_manager"

    def test_empty_external_roles_returns_user_baseline(self):
        p = _ConcreteProvider()
        assert p.map_roles([]) == "user"

    def test_only_unknown_roles_returns_user_baseline(self):
        p = _ConcreteProvider()
        assert p.map_roles(["superadmin", "god", "manager"]) == "user"

    def test_case_insensitive_role_recognition(self):
        p = _ConcreteProvider()
        assert p.map_roles(["ADMIN"]) == "admin"
        assert p.map_roles(["Quant_Dev"]) == "quant_dev"
        assert p.map_roles(["  Portfolio_Manager  "]) == "portfolio_manager"

    def test_quant_dev_no_longer_silently_grants_developer_access(self):
        """A user authenticating with only a 'quant_dev' claim must NOT be
        treated as a developer. This is the security guarantee of removing
        silent promotion."""
        from engine.api.auth.dependency import ROLE_HIERARCHY

        p = _ConcreteProvider()
        mapped = p.map_roles(["quant_dev"])
        assert mapped == "quant_dev"
        assert mapped != "developer"
        # In the canonical hierarchy quant_dev sits strictly below developer.
        assert ROLE_HIERARCHY["quant_dev"] < ROLE_HIERARCHY["developer"]


# ---------------------------------------------------------------------------
# Section 2 — auth_overwrite_role_on_login default and semantics
# ---------------------------------------------------------------------------


class TestAuthOverwriteRoleOnLoginSetting:
    def test_default_is_false(self):
        s = Settings()
        assert s.auth_overwrite_role_on_login is False

    def test_explicit_true(self):
        s = Settings(auth_overwrite_role_on_login=True)
        assert s.auth_overwrite_role_on_login is True

    def test_env_prefix_loaded(self, monkeypatch):
        monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", "true")
        s = Settings()
        assert s.auth_overwrite_role_on_login is True

    def test_env_prefix_disabled_explicit(self, monkeypatch):
        monkeypatch.setenv("NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN", "false")
        s = Settings()
        assert s.auth_overwrite_role_on_login is False


# ---------------------------------------------------------------------------
# Section 3 — warning emitted for ANY unrecognized role
# ---------------------------------------------------------------------------


class TestUnrecognizedRoleWarnings:
    """A warning must be logged for *each* unrecognized role, not just when
    the entire claim list is unrecognized."""

    def test_warning_for_single_unrecognized_role(self):
        p = _ConcreteProvider()
        with patch("engine.api.auth.base.logger") as mock_logger:
            result = p.map_roles(["admin", "unknown_role"])
        assert result == "admin"
        # Exactly one warning call for the single unrecognized role.
        mock_logger.warning.assert_called_once()
        args = mock_logger.warning.call_args.args
        kwargs = mock_logger.warning.call_args.kwargs
        assert args == ("auth.role.unknown",)
        assert kwargs.get("role") == "unknown_role"
        assert kwargs.get("provider") == "concrete-test"

    def test_warning_for_each_unrecognized_role(self):
        p = _ConcreteProvider()
        with patch("engine.api.auth.base.logger") as mock_logger:
            p.map_roles(["admin", "foo_role", "bar_role", "baz_role"])
        # Three distinct warnings for foo/bar/baz.
        assert mock_logger.warning.call_count == 3
        warned_roles = {
            call.kwargs.get("role") for call in mock_logger.warning.call_args_list
        }
        assert warned_roles == {"foo_role", "bar_role", "baz_role"}

    def test_warning_emitted_even_when_recognized_role_present(self):
        """The bug-fix scenario: previously a warning was only emitted when
        ALL roles were unrecognized. Now a warning fires for each one."""
        p = _ConcreteProvider()
        with patch("engine.api.auth.base.logger") as mock_logger:
            p.map_roles(["admin", "ghost_role"])
        # Even though 'admin' is recognized, 'ghost_role' still warns.
        assert mock_logger.warning.call_count == 1
        assert mock_logger.warning.call_args.kwargs.get("role") == "ghost_role"

    def test_no_warning_when_all_roles_recognized(self):
        p = _ConcreteProvider()
        with patch("engine.api.auth.base.logger") as mock_logger:
            p.map_roles(["admin", "developer", "viewer"])
        mock_logger.warning.assert_not_called()

    def test_no_warning_on_empty_input(self):
        p = _ConcreteProvider()
        with patch("engine.api.auth.base.logger") as mock_logger:
            p.map_roles([])
        mock_logger.warning.assert_not_called()

    def test_warning_normalized_role_name_appears(self):
        """Even when the claim has weird casing/whitespace, the warning
        should reference the normalized form so operators can grep logs."""
        p = _ConcreteProvider()
        with patch("engine.api.auth.base.logger") as mock_logger:
            p.map_roles(["  WEIRD_ROLE  "])
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.kwargs.get("role") == "weird_role"

    def test_warning_includes_provider_name(self):
        """Provider name should be captured to help debugging in mixed
        deployments (LDAP + OIDC, etc.)."""
        p = _ConcreteProvider()
        with patch("engine.api.auth.base.logger") as mock_logger:
            p.map_roles(["mystery_role"])
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.kwargs.get("provider") == "concrete-test"

    def test_warning_event_name_is_stable(self):
        """Operators rely on the structured event name for log queries."""
        p = _ConcreteProvider()
        with patch("engine.api.auth.base.logger") as mock_logger:
            p.map_roles(["bogus"])
        mock_logger.warning.assert_called_once_with(
            "auth.role.unknown",
            role="bogus",
            provider="concrete-test",
        )


# ---------------------------------------------------------------------------
# Section 4 — Updated test_map_roles_new_domain_roles semantics
# ---------------------------------------------------------------------------


class TestNewDomainRolesNoSilentPromotion:
    """This is the test called out explicitly in the prompt — it asserts the
    new contract for ``map_roles`` around the domain-specific roles."""

    def test_map_roles_new_domain_roles(self):
        p = _ConcreteProvider()
        # Previously these silently promoted to "developer" and "user".
        # Now the highest canonical role wins, with no auto-elevation.
        assert p.map_roles(["retail_trader", "quant_dev"]) == "quant_dev"
        assert p.map_roles(["portfolio_manager", "quant_dev"]) == "portfolio_manager"
        assert p.map_roles(["viewer"]) == "viewer"
        assert p.map_roles(["retail_trader"]) == "retail_trader"
        assert p.map_roles(["portfolio_manager"]) == "portfolio_manager"
        # Sanity: pure "quant_dev" is no longer elevated to "developer"
        assert p.map_roles(["quant_dev"]) == "quant_dev"

    def test_no_promotion_via_combination(self):
        """Even with multiple previously-promotable roles, none get auto-elevated."""
        p = _ConcreteProvider()
        # Both viewer and quant_dev used to promote; the highest canonical
        # claim is quant_dev, with no further bump.
        assert p.map_roles(["viewer", "quant_dev"]) == "quant_dev"
        # All previously-promotable + a non-promotable: only recognized
        # canonical priority wins.
        assert p.map_roles(["viewer", "quant_dev", "retail_trader"]) == "quant_dev"


# ---------------------------------------------------------------------------
# Section 5 — LDAP provider honours auth_overwrite_role_on_login
# ---------------------------------------------------------------------------


def _make_ldap_attrs(member_of: list[bytes] | None = None) -> dict[str, list[bytes]]:
    return {
        "uid": [b"testuser"],
        "mail": [b"testuser@example.com"],
        "cn": [b"Test User"],
        "memberOf": member_of if member_of is not None else [],
    }


class _FakeLDAPConn:
    def __init__(self, search_results: list[tuple[str, dict]]):
        self._results = search_results
        self._options: dict[int, object] = {}

    def set_option(self, key, value):
        self._options[key] = value

    def simple_bind_s(self, *args, **kwargs):
        return None

    def search_s(self, *args, **kwargs):
        return self._results

    def unbind_s(self):
        return None


def _build_ldap_mock(search_results):
    mock_conn = _FakeLDAPConn(search_results=search_results)
    mock_ldap = MagicMock()
    mock_ldap.initialize.return_value = mock_conn
    mock_ldap.SCOPE_SUBTREE = 2
    mock_ldap.OPT_NETWORK_TIMEOUT = 1
    mock_ldap.OPT_TIMEOUT = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = lambda s: s
    return mock_ldap, mock_filter


def _mock_execute_factory(first_result, second_result):
    async def mock_execute(_stmt):
        return MagicMock(scalar_one_or_none=MagicMock(
            return_value=first_result if _mock_execute_factory.calls == 0 else second_result
        ))
    _mock_execute_factory.calls = 0

    async def _wrapped(stmt):
        r = MagicMock()
        r.scalar_one_or_none.return_value = (
            first_result if _mock_execute_factory.calls == 0 else second_result
        )
        _mock_execute_factory.calls += 1
        return r
    return _wrapped


class TestLDAPRoleOverwriteOnLogin:
    """LDAP provider must respect Settings.auth_overwrite_role_on_login."""

    async def test_existing_user_role_preserved_when_setting_false(
        self, ldap_provider, monkeypatch,
    ):
        from engine.db.models import User

        s = _make_settings(auth_overwrite_role_on_login=False)
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=preserved,ou=users,dc=example,dc=com", attrs)]
        )

        existing = User(
            email="preserved@example.com",
            display_name="Preserved",
            is_active=True,
            role="user",  # different from the mapped "admin"
            auth_provider="ldap",
            external_id="preserved",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            r = await ldap_provider.authenticate(
                username="preserved", password="x", db=mock_db
            )

        assert r.success is True
        assert existing.role == "user"  # unchanged
        mock_db.flush.assert_not_called()

    async def test_existing_user_role_overwritten_when_setting_true(
        self, ldap_provider, monkeypatch,
    ):
        from engine.db.models import User

        s = _make_settings(auth_overwrite_role_on_login=True)
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=promoted,ou=users,dc=example,dc=com", attrs)]
        )

        existing = User(
            email="promoted@example.com",
            display_name="Promoted",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="promoted",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            r = await ldap_provider.authenticate(
                username="promoted", password="x", db=mock_db
            )

        assert r.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_called()

    async def test_new_user_role_assigned_on_creation_regardless_of_setting(
        self, ldap_provider, monkeypatch,
    ):
        """New users must always receive the mapped role at creation time —
        the auth_overwrite_role_on_login flag only governs subsequent logins."""
        s = _make_settings(auth_overwrite_role_on_login=False)
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=newadmin,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        added_users: list = []

        def track_add(user):
            user.is_active = True
            added_users.append(user)

        mock_db.add = MagicMock(side_effect=track_add)
        mock_db.refresh = AsyncMock(side_effect=lambda u: setattr(u, "is_active", True))
        mock_db.flush = AsyncMock()

        async def execute(_stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        mock_db.execute = execute

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            r = await ldap_provider.authenticate(
                username="newadmin", password="x", db=mock_db
            )

        assert r.success is True
        assert len(added_users) == 1
        assert added_users[0].role == "admin"

    async def test_no_overwrite_when_mapped_role_equals_stored(
        self, ldap_provider, monkeypatch,
    ):
        """If the mapped role already matches the stored one, nothing happens,
        even when auth_overwrite_role_on_login is True."""
        from engine.db.models import User

        s = _make_settings(auth_overwrite_role_on_login=True)
        monkeypatch.setattr("engine.api.auth.ldap.settings", s)

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=stable,ou=users,dc=example,dc=com", attrs)]
        )

        existing = User(
            email="stable@example.com",
            display_name="Stable",
            is_active=True,
            role="admin",  # matches mapped role
            auth_provider="ldap",
            external_id="stable",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            r = await ldap_provider.authenticate(
                username="stable", password="x", db=mock_db
            )

        assert r.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()


@pytest.fixture
def ldap_provider():
    return LDAPAuthProvider()


# ---------------------------------------------------------------------------
# Section 6 — OIDC provider honours auth_overwrite_role_on_login
# ---------------------------------------------------------------------------


DISCOVERY_DOC = {
    "authorization_endpoint": "https://id.example.com/authorize",
    "token_endpoint": "https://id.example.com/token",
    "jwks_uri": "https://id.example.com/jwks",
}


class _FakeHttpxResponse:
    def __init__(self, json_data=None, raise_error=None):
        self._json = json_data
        self._raise = raise_error

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, get_responses=None, post_responses=None):
        self._gets = list(get_responses or [])
        self._posts = list(post_responses or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, *_args, **_kwargs):
        return self._gets.pop(0)

    async def post(self, *_args, **_kwargs):
        return self._posts.pop(0)


def _generate_rsa_key_pair():
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return pk, pk.public_key()


def _make_jwk_kid(pub_key) -> tuple[dict, str]:
    from jwt.algorithms import RSAAlgorithm
    kid = "test-kid-overhaul"
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    return jwk_dict, kid


def _build_oidc_client(claims: dict, audience: str = "test-client-id"):
    """Build a fully-mocked httpx client that returns a signed id_token."""
    private_key, pub_key = _generate_rsa_key_pair()
    jwk_dict, kid = _make_jwk_kid(pub_key)
    full_claims = {"aud": audience, **claims}
    id_token = jwt.encode(full_claims, private_key, algorithm="RS256", headers={"kid": kid})

    return _FakeAsyncClient(
        get_responses=[
            _FakeHttpxResponse(json_data=DISCOVERY_DOC),
            _FakeHttpxResponse(json_data={"keys": [jwk_dict]}),
        ],
        post_responses=[
            _FakeHttpxResponse(json_data={"id_token": id_token, "access_token": "at"}),
        ],
    )


@pytest.fixture
def oidc_provider():
    return OIDCAuthProvider()


class TestOIDCRoleOverwriteOnLogin:
    async def test_existing_user_role_preserved_when_setting_false(
        self, oidc_provider, monkeypatch,
    ):
        """When overwrite is disabled (default), an existing OIDC user's role
        is NOT updated even if their external claims change."""
        from engine.db.models import User

        s = _make_settings(auth_overwrite_role_on_login=False)
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)

        existing = User(
            email="user@example.com",
            display_name="Existing",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-id-1",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        fake_client = _build_oidc_client({
            "sub": "oidc-id-1",
            "email": "user@example.com",
            "name": "Existing",
            "roles": ["admin"],  # would map to admin, but should be ignored
        })

        with patch("httpx.AsyncClient", return_value=fake_client):
            r = await oidc_provider.authenticate(code="abc", db=mock_db)

        assert r.success is True
        # Existing user role untouched.
        assert existing.role == "user"
        mock_db.flush.assert_not_called()

    async def test_existing_user_role_overwritten_when_setting_true(
        self, oidc_provider, monkeypatch,
    ):
        from engine.db.models import User

        s = _make_settings(auth_overwrite_role_on_login=True)
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)

        existing = User(
            email="promote@example.com",
            display_name="Promote Me",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-id-2",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        fake_client = _build_oidc_client({
            "sub": "oidc-id-2",
            "email": "promote@example.com",
            "name": "Promote Me",
            "roles": ["admin"],
        })

        with patch("httpx.AsyncClient", return_value=fake_client):
            r = await oidc_provider.authenticate(code="abc", db=mock_db)

        assert r.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_called()

    async def test_new_user_role_assigned_on_creation_regardless_of_setting(
        self, oidc_provider, monkeypatch,
    ):
        """New OIDC users must always get the mapped role at creation; the
        overwrite flag only governs subsequent logins."""
        s = _make_settings(auth_overwrite_role_on_login=False)
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)

        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=lambda u: setattr(u, "is_active", True))
        added: list = []
        mock_db.add = MagicMock(side_effect=lambda u: (setattr(u, "is_active", True), added.append(u)))

        fake_client = _build_oidc_client({
            "sub": "oidc-new",
            "email": "new@example.com",
            "name": "New User",
            "roles": ["admin"],
        })

        with patch("httpx.AsyncClient", return_value=fake_client):
            r = await oidc_provider.authenticate(code="abc", db=mock_db)

        assert r.success is True
        assert len(added) == 1
        assert added[0].role == "admin"

    async def test_no_overwrite_when_mapped_role_equals_stored(
        self, oidc_provider, monkeypatch,
    ):
        from engine.db.models import User

        s = _make_settings(auth_overwrite_role_on_login=True)
        monkeypatch.setattr("engine.api.auth.oidc.settings", s)

        existing = User(
            email="stable@example.com",
            display_name="Stable",
            is_active=True,
            role="admin",  # matches the mapped role
            auth_provider="oidc",
            external_id="oidc-stable",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = result_mock
        mock_db.flush = AsyncMock()

        fake_client = _build_oidc_client({
            "sub": "oidc-stable",
            "email": "stable@example.com",
            "name": "Stable",
            "roles": ["admin"],
        })

        with patch("httpx.AsyncClient", return_value=fake_client):
            r = await oidc_provider.authenticate(code="abc", db=mock_db)

        assert r.success is True
        assert existing.role == "admin"
        mock_db.flush.assert_not_called()


# ---------------------------------------------------------------------------
# Section 7 — Cross-cutting invariants
# ---------------------------------------------------------------------------


class TestCrossCuttingInvariants:
    """Invariants that must hold across the entire role-mapping surface."""

    def test_priority_dictionary_in_base_matches_dependency_hierarchy(self):
        """The role-priority table embedded in map_roles must agree with the
        canonical ROLE_HIERARCHY used for authorization checks."""
        from engine.api.auth.dependency import ROLE_HIERARCHY

        # map_roles uses an inline dict; we verify by sampling each role.
        p = _ConcreteProvider()
        for low, high in [
            ("viewer", "user"),
            ("user", "retail_trader"),
            ("retail_trader", "quant_dev"),
            ("quant_dev", "developer"),
            ("developer", "portfolio_manager"),
            ("portfolio_manager", "admin"),
        ]:
            assert p.map_roles([low, high]) == high, (
                f"Expected {high} to win over {low}"
            )
            assert ROLE_HIERARCHY[high] > ROLE_HIERARCHY[low]

    def test_map_roles_idempotent(self):
        """Calling map_roles twice with the same input returns the same role."""
        p = _ConcreteProvider()
        assert p.map_roles(["admin", "developer"]) == p.map_roles(["admin", "developer"])

    def test_unknown_roles_do_not_pollute_priority(self):
        """Even if an unknown role's name collides with a canonical one after
        normalization, the canonical table is the sole source of truth."""
        p = _ConcreteProvider()
        # "admin " (trailing space) normalizes to "admin" → recognized
        assert p.map_roles(["admin "]) == "admin"
        # "admoon" is similar but distinct → unrecognized
        assert p.map_roles(["admoon"]) == "user"

    def test_no_role_promotions_attribute_anywhere_in_auth(self):
        """Sanity check: no module under engine/api/auth should expose
        _ROLE_PROMOTIONS anymore."""
        import importlib
        import pkgutil

        import engine.api.auth as auth_pkg

        found_in: list[str] = []
        for module_info in pkgutil.iter_modules(auth_pkg.__path__):
            full_name = f"engine.api.auth.{module_info.name}"
            try:
                mod = importlib.import_module(full_name)
            except Exception:  # noqa: S112 - sanity sweep, skip unloadable
                continue
            if hasattr(mod, "_ROLE_PROMOTIONS"):
                found_in.append(full_name)
        assert found_in == [], f"_ROLE_PROMOTIONS still found in: {found_in}"
