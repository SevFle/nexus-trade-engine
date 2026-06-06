"""Tests for ``auth_overwrite_role_on_login`` flag in federated login paths.

Background
----------
SEV-741 introduced a defense-in-depth flag to control whether the local
user's role is overwritten by the IdP-asserted role on each federated
login.  The flag defaults to ``False`` so a misconfigured or compromised
upstream Identity Provider cannot silently downgrade or escalate a
previously-granted local role.

These tests pin the behavior of the flag in both states for each
federated provider that extracts a role from upstream claims/groups:

* ``LDAPAuthProvider`` — role derived from ``memberOf`` groups via
  ``ldap_role_mapping``.
* ``OIDCAuthProvider`` — role derived from a configurable claim
  (``oidc_role_claim``).

For each provider we cover:

1. **Default (False)** — local role is preserved, a warning is emitted,
   and the DB is not flushed for the role update.
2. **Opt-in (True)** — local role is overwritten with the normalized
   IdP role, and the DB is flushed.

The local "LocalAuthProvider" is intentionally not covered here: it
does not consume external claims and therefore has no role-overwrite
path.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.ldap import LDAPAuthProvider
from engine.api.auth.oidc import OIDCAuthProvider
from engine.config import Settings

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _settings_with_overwrite_flag(*, overwrite: bool) -> Settings:
    """Build a Settings instance with the overwrite flag explicitly set."""
    return Settings(auth_overwrite_role_on_login=overwrite)


# ---------------------------------------------------------------------------
# LDAP — flag-driven overwrite behavior
# ---------------------------------------------------------------------------


def _make_ldap_attrs(member_of: list[bytes] | None = None):
    attrs: dict[str, list[bytes]] = {
        "uid": [b"flaguser"],
        "mail": [b"flaguser@example.com"],
        "cn": [b"Flag User"],
    }
    if member_of is not None:
        attrs["memberOf"] = member_of
    return attrs


class _FakeLDAPConn:
    def __init__(self, search_results):
        self._search_results = search_results
        self._options: dict[int, object] = {}

    def set_option(self, opt, value):
        self._options[opt] = value

    def simple_bind_s(self, dn, password):
        return None

    def search_s(self, base, scope, filterstr, attrlist):
        return self._search_results

    def unbind_s(self):
        return None


def _build_ldap_mock(search_results):
    mock_ldap = MagicMock()
    mock_ldap.initialize = MagicMock(return_value=_FakeLDAPConn(search_results))
    mock_ldap.OPT_NETWORK_TIMEOUT = 7
    mock_ldap.OPT_TIMEOUT = 8
    mock_ldap.SCOPE_SUBTREE = 2
    mock_filter = MagicMock()
    mock_filter.escape_filter_chars = MagicMock(side_effect=lambda x: x)
    return mock_ldap, mock_filter


def _ldap_settings(monkeypatch, *, overwrite: bool) -> Settings:
    s = Settings(
        ldap_server_url="ldap://ldap.example.com:389",
        ldap_bind_dn="uid={{username}},ou=users,dc=example,dc=com",
        ldap_search_base="ou=users,dc=example,dc=com",
        ldap_role_mapping=json.dumps({
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=developers,ou=groups,dc=example,dc=com": "developer",
        }),
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.ldap.settings", s)
    return s


class TestLDAPOverwriteRoleOnLogin:
    """LDAP provider respects the ``auth_overwrite_role_on_login`` flag."""

    async def test_default_false_preserves_local_role(
        self, monkeypatch
    ):
        from engine.db.models import User

        s = _ldap_settings(monkeypatch, overwrite=False)
        assert s.auth_overwrite_role_on_login is False

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=flaguser,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="flaguser@example.com",
            display_name="Flag User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="flaguser",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        provider = LDAPAuthProvider()
        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="flaguser", password="pass", db=mock_db
            )

        assert result.success is True
        # Local role must be preserved when flag is False.
        assert existing_user.role == "user"
        # No flush should occur for the role update path.
        mock_db.flush.assert_not_called()

    async def test_default_false_logs_warning(self, monkeypatch):
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite=False)

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=flaguser,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="flaguser@example.com",
            display_name="Flag User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="flaguser",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, "level": "info", **kwargs})

        from engine.api.auth import ldap as ldap_module

        monkeypatch.setattr(ldap_module, "logger", _Stub())

        provider = LDAPAuthProvider()
        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            await provider.authenticate(
                username="flaguser", password="pass", db=mock_db
            )

        warning_calls = [
            c for c in calls if c["event"] == "auth.ldap.role_overwrite_skipped"
        ]
        assert len(warning_calls) == 1, (
            "Expected exactly one role_overwrite_skipped warning when the "
            "flag is False and the IdP role differs from the local role."
        )
        assert warning_calls[0]["local_role"] == "user"
        assert warning_calls[0]["idp_role"] == "admin"

    async def test_true_overwrites_local_role(self, monkeypatch):
        from engine.db.models import User

        s = _ldap_settings(monkeypatch, overwrite=True)
        assert s.auth_overwrite_role_on_login is True

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=flaguser,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="flaguser@example.com",
            display_name="Flag User",
            is_active=True,
            role="user",
            auth_provider="ldap",
            external_id="flaguser",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        provider = LDAPAuthProvider()
        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="flaguser", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_called()

    async def test_true_no_overwrite_when_roles_match(self, monkeypatch):
        """If the IdP role matches the local role, no flush is needed
        even when the flag is True."""
        from engine.db.models import User

        _ldap_settings(monkeypatch, overwrite=True)

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=flaguser,ou=users,dc=example,dc=com", attrs)]
        )

        existing_user = User(
            email="flaguser@example.com",
            display_name="Flag User",
            is_active=True,
            role="admin",
            auth_provider="ldap",
            external_id="flaguser",
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        provider = LDAPAuthProvider()
        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="flaguser", password="pass", db=mock_db
            )

        assert result.success is True
        assert existing_user.role == "admin"
        # Role matched, so the overwrite branch must not have flushed.
        mock_db.flush.assert_not_called()


# ---------------------------------------------------------------------------
# OIDC — flag-driven overwrite behavior
# ---------------------------------------------------------------------------


DISCOVERY_DOC = {
    "authorization_endpoint": "https://id.example.com/authorize",
    "token_endpoint": "https://id.example.com/token",
    "jwks_uri": "https://id.example.com/jwks",
}


class _FakeHttpxResponse:
    def __init__(self, json_data=None, raise_error=None):
        self._json_data = json_data
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error:
            raise self._raise_error

    def json(self):
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, get_responses=None, post_responses=None):
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, **kwargs):
        if self._get_responses:
            return self._get_responses.pop(0)
        return _FakeHttpxResponse(json_data={})

    async def post(self, url, **kwargs):
        if self._post_responses:
            return self._post_responses.pop(0)
        return _FakeHttpxResponse(json_data={})


def _oidc_settings(monkeypatch, *, overwrite: bool) -> Settings:
    s = Settings(
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_redirect_uri="https://app.example.com/callback",
        oidc_role_claim="roles",
        auth_overwrite_role_on_login=overwrite,
    )
    monkeypatch.setattr("engine.api.auth.oidc.settings", s)
    return s


def _build_oidc_mock_client(rsa_keys, id_token_claims):
    import jwt as _jwt
    from jwt.algorithms import RSAAlgorithm

    private_key, pub_key = rsa_keys
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = "test-kid-123"
    kid = "test-kid-123"
    claims = {"aud": "test-client-id", **id_token_claims}
    id_token = _jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})

    disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
    token_resp = _FakeHttpxResponse(json_data={"id_token": id_token, "access_token": "at"})
    jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})

    get_responses = [disc_resp, jwks_resp]
    post_responses = [token_resp]
    return _FakeAsyncClient(get_responses=get_responses, post_responses=post_responses)


@pytest.fixture
def rsa_keys():
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


class TestOIDCOverwriteRoleOnLogin:
    """OIDC provider respects the ``auth_overwrite_role_on_login`` flag."""

    async def test_default_false_preserves_local_role(
        self, monkeypatch, rsa_keys
    ):
        from engine.db.models import User

        s = _oidc_settings(monkeypatch, overwrite=False)
        assert s.auth_overwrite_role_on_login is False

        fake_client = _build_oidc_mock_client(
            rsa_keys,
            {
                "sub": "oidc-existing-1",
                "email": "existing1@example.com",
                "name": "Existing One",
                "roles": ["admin"],
            },
        )

        existing_user = User(
            email="existing1@example.com",
            display_name="Existing One",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-existing-1",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        provider = OIDCAuthProvider()
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        # Local role must be preserved when flag is False.
        assert existing_user.role == "user"
        mock_db.flush.assert_not_called()

    async def test_default_false_logs_warning(self, monkeypatch, rsa_keys):
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite=False)

        fake_client = _build_oidc_mock_client(
            rsa_keys,
            {
                "sub": "oidc-existing-2",
                "email": "existing2@example.com",
                "name": "Existing Two",
                "roles": ["admin"],
            },
        )

        existing_user = User(
            email="existing2@example.com",
            display_name="Existing Two",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-existing-2",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        calls: list[dict[str, object]] = []

        class _Stub:
            def warning(self, _event, **kwargs):
                calls.append({"event": _event, **kwargs})

            def info(self, _event, **kwargs):  # pragma: no cover
                calls.append({"event": _event, "level": "info", **kwargs})

        from engine.api.auth import oidc as oidc_module

        monkeypatch.setattr(oidc_module, "logger", _Stub())

        provider = OIDCAuthProvider()
        with patch("httpx.AsyncClient", return_value=fake_client):
            await provider.authenticate(code="auth-code", db=mock_db)

        warning_calls = [
            c for c in calls if c["event"] == "auth.oidc.role_overwrite_skipped"
        ]
        assert len(warning_calls) == 1, (
            "Expected exactly one role_overwrite_skipped warning when the "
            "flag is False and the IdP role differs from the local role."
        )
        assert warning_calls[0]["local_role"] == "user"
        assert warning_calls[0]["idp_role"] == "admin"

    async def test_true_overwrites_local_role(self, monkeypatch, rsa_keys):
        from engine.db.models import User

        s = _oidc_settings(monkeypatch, overwrite=True)
        assert s.auth_overwrite_role_on_login is True

        fake_client = _build_oidc_mock_client(
            rsa_keys,
            {
                "sub": "oidc-existing-3",
                "email": "existing3@example.com",
                "name": "Existing Three",
                "roles": ["admin"],
            },
        )

        existing_user = User(
            email="existing3@example.com",
            display_name="Existing Three",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-existing-3",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        provider = OIDCAuthProvider()
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_called()

    async def test_true_no_overwrite_when_roles_match(
        self, monkeypatch, rsa_keys
    ):
        """If the IdP role matches the local role, no flush is needed
        even when the flag is True."""
        from engine.db.models import User

        _oidc_settings(monkeypatch, overwrite=True)

        fake_client = _build_oidc_mock_client(
            rsa_keys,
            {
                "sub": "oidc-existing-4",
                "email": "existing4@example.com",
                "name": "Existing Four",
                "roles": ["admin"],
            },
        )

        existing_user = User(
            email="existing4@example.com",
            display_name="Existing Four",
            is_active=True,
            role="admin",
            auth_provider="oidc",
            external_id="oidc-existing-4",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        provider = OIDCAuthProvider()
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert existing_user.role == "admin"
        mock_db.flush.assert_not_called()


# ---------------------------------------------------------------------------
# New-user creation is unaffected by the flag — the mapped IdP role is
# always used for first-time federated users.
# ---------------------------------------------------------------------------


class TestNewUserRoleAssignmentUnaffectedByFlag:
    """The flag only governs role overwrite on subsequent logins; new
    users always receive their mapped IdP role on first login."""

    async def test_ldap_new_user_gets_mapped_role_with_flag_false(
        self, monkeypatch
    ):
        _ldap_settings(monkeypatch, overwrite=False)

        attrs = _make_ldap_attrs(
            member_of=[b"cn=admins,ou=groups,dc=example,dc=com"]
        )
        mock_ldap, mock_filter = _build_ldap_mock(
            search_results=[("uid=newuser,ou=users,dc=example,dc=com", attrs)]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        added_users: list[object] = []

        def track_add(user):
            added_users.append(user)
            user.is_active = True

        async def mock_refresh(user):
            user.is_active = True

        mock_db.add = MagicMock(side_effect=track_add)
        mock_db.refresh = AsyncMock(side_effect=mock_refresh)
        mock_db.flush = AsyncMock()

        idx = 0

        async def mock_execute(stmt):
            nonlocal idx
            idx += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        mock_db.execute = mock_execute

        provider = LDAPAuthProvider()
        with patch.dict("sys.modules", {"ldap": mock_ldap, "ldap.filter": mock_filter}):
            result = await provider.authenticate(
                username="newuser", password="pass", db=mock_db
            )

        assert result.success is True
        assert len(added_users) == 1
        assert added_users[0].role == "admin"
