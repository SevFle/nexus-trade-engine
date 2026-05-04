"""Comprehensive tests for engine/api/auth/oidc.py — SEV-264.

Covers:
  - OIDC discovery (success, caching, HTTPS enforcement)
  - JWKS retrieval (success, caching, key matching)
  - authenticate flow (happy path, missing params, token exchange failure,
    no matching key, incomplete profile, email conflict, disabled user,
    new user creation with role mapping)
  - get_authorize_url (with/without state)
  - name property
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.oidc import OIDCAuthProvider
from engine.config import Settings


def _generate_rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_jwk_kid(pub_key) -> tuple[dict[str, Any], str]:
    kid = "test-kid-123"
    from jwt.algorithms import RSAAlgorithm

    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    return jwk_dict, kid


def _sign_id_token(claims: dict, private_key, kid: str) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


DISCOVERY_DOC = {
    "authorization_endpoint": "https://id.example.com/authorize",
    "token_endpoint": "https://id.example.com/token",
    "jwks_uri": "https://id.example.com/jwks",
}


@pytest.fixture
def oidc_provider():
    return OIDCAuthProvider()


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_redirect_uri="https://app.example.com/callback",
        oidc_role_claim="roles",
    )
    monkeypatch.setattr("engine.api.auth.oidc.settings", s)
    return s


@pytest.fixture
def rsa_keys():
    return _generate_rsa_key_pair()


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


class TestOIDCNameProperty:
    def test_name_returns_oidc(self, oidc_provider):
        assert oidc_provider.name == "oidc"


class TestOIDCDiscovery:
    async def test_get_discovery_success(self, oidc_provider, mock_settings):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider._get_discovery()

        assert result == DISCOVERY_DOC

    async def test_get_discovery_caches_result(self, oidc_provider, mock_settings):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            result1 = await oidc_provider._get_discovery()
            result2 = await oidc_provider._get_discovery()

        assert result1 == DISCOVERY_DOC
        assert result2 == DISCOVERY_DOC
        assert len(fake_client._get_responses) == 0

    async def test_get_discovery_rejects_http_url(self, oidc_provider, mock_settings):
        mock_settings.oidc_discovery_url = "http://insecure.example.com/.well-known"
        with pytest.raises(ValueError, match="HTTPS"):
            await oidc_provider._get_discovery()

    async def test_get_discovery_rejects_no_scheme(self, oidc_provider, mock_settings):
        mock_settings.oidc_discovery_url = "id.example.com/.well-known"
        with pytest.raises(ValueError, match="HTTPS"):
            await oidc_provider._get_discovery()

    async def test_get_discovery_http_error_propagates(self, oidc_provider, mock_settings):
        resp = _FakeHttpxResponse(
            raise_error=httpx.HTTPStatusError(
                "Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )
        )
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            with pytest.raises(httpx.HTTPStatusError):
                await oidc_provider._get_discovery()


class TestOIDCJWKS:
    async def test_get_jwks_success(self, oidc_provider, mock_settings, rsa_keys):
        _, pub_key = rsa_keys
        jwk_dict, _ = _make_jwk_kid(pub_key)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
        fake_client = _FakeAsyncClient(get_responses=[disc_resp, jwks_resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider._get_jwks()

        assert "keys" in result
        assert len(result["keys"]) == 1

    async def test_get_jwks_caches_result(self, oidc_provider, mock_settings, rsa_keys):
        _, pub_key = rsa_keys
        jwk_dict, _ = _make_jwk_kid(pub_key)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
        fake_client = _FakeAsyncClient(get_responses=[disc_resp, jwks_resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            result1 = await oidc_provider._get_jwks()
            result2 = await oidc_provider._get_jwks()

        assert result1 == result2
        assert len(fake_client._get_responses) == 0


def _build_full_mock_client(rsa_keys, id_token_claims):
    private_key, pub_key = rsa_keys
    jwk_dict, kid = _make_jwk_kid(pub_key)
    claims = {"aud": "test-client-id", **id_token_claims}
    id_token = _sign_id_token(claims, private_key, kid)

    disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
    token_resp = _FakeHttpxResponse(json_data={"id_token": id_token, "access_token": "at"})
    jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})

    get_responses = [disc_resp, jwks_resp]
    post_responses = [token_resp]
    return _FakeAsyncClient(get_responses=get_responses, post_responses=post_responses)


class TestOIDCAuthenticate:
    async def test_authenticate_missing_code(self, oidc_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await oidc_provider.authenticate(db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_missing_db(self, oidc_provider, mock_settings):
        result = await oidc_provider.authenticate(code="abc")
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_missing_both(self, oidc_provider, mock_settings):
        result = await oidc_provider.authenticate()
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_token_exchange_fails(self, oidc_provider, mock_settings):
        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(
            raise_error=Exception("Token exchange failed")
        )
        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_authenticate_no_matching_kid(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)
        id_token = _sign_id_token(
            {"sub": "x", "email": "x@x.com"}, private_key, kid
        )

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": []})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_authenticate_happy_path_new_user(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-user-123",
                "email": "newuser@example.com",
                "name": "New User",
                "roles": ["admin"],
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "newuser@example.com"
        assert result.user_info.provider == "oidc"
        assert result.user_info.external_id == "oidc-user-123"
        assert result.user_info.display_name == "New User"
        assert result.user_info.roles == ["admin"]
        mock_db.add.assert_called_once()

    async def test_authenticate_existing_oidc_user(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        from engine.db.models import User

        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-user-existing",
                "email": "existing@example.com",
                "name": "Existing User",
            },
        )

        existing_user = User(
            email="existing@example.com",
            display_name="Existing User",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-user-existing",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "existing@example.com"
        mock_db.add.assert_not_called()

    async def test_authenticate_email_conflict_different_provider(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        from engine.db.models import User

        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-new-456",
                "email": "conflict@example.com",
                "name": "Conflict User",
            },
        )

        conflict_user = User(
            email="conflict@example.com",
            display_name="Conflict User",
            auth_provider="local",
        )
        mock_db = AsyncMock(spec=AsyncSession)

        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                r.scalar_one_or_none.return_value = None
            else:
                r.scalar_one_or_none.return_value = conflict_user
            return r

        mock_db.execute = mock_execute

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "different provider" in result.error

    async def test_authenticate_disabled_user(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        from engine.db.models import User

        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-disabled-789",
                "email": "disabled@example.com",
                "name": "Disabled User",
            },
        )

        disabled_user = User(
            email="disabled@example.com",
            display_name="Disabled User",
            is_active=False,
            auth_provider="oidc",
            external_id="oidc-disabled-789",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = disabled_user
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "disabled" in result.error

    async def test_authenticate_incomplete_profile_missing_email(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {"sub": "oidc-no-email", "email": "", "name": "No Email"},
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "Incomplete" in result.error

    async def test_authenticate_incomplete_profile_missing_sub(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {"sub": "", "email": "nosub@example.com", "name": "No Sub"},
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "Incomplete" in result.error

    async def test_authenticate_uses_preferred_username_fallback(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-pref-uname",
                "email": "pref@example.com",
                "preferred_username": "prefuser",
                "roles": ["user"],
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.display_name == "prefuser"

    async def test_authenticate_email_local_part_as_name_fallback(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-email-fallback",
                "email": "myname@company.com",
                "roles": ["user"],
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.display_name == "myname"

    async def test_authenticate_role_mapping_developer(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-dev-role",
                "email": "dev@example.com",
                "roles": ["developer", "user"],
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert len(created_users) == 1
        assert created_users[0].role == "developer"

    async def test_authenticate_non_list_roles_gets_default(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-nonlist-role",
                "email": "nonlist@example.com",
                "roles": "admin",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert len(created_users) == 1
        assert created_users[0].role == "user"

    async def test_authenticate_raw_claims_included_in_user_info(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-claims-check",
                "email": "claims@example.com",
                "name": "Claims User",
                "custom_field": "custom_value",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.raw_claims["custom_field"] == "custom_value"

    async def test_authenticate_token_exchange_sends_correct_params(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)
        id_token = _sign_id_token(
            {"sub": "oidc-param-check", "email": "params@example.com", "name": "Params"},
            private_key,
            kid,
        )

        posted_data = {}

        class CapturingClient(_FakeAsyncClient):
            async def post(self, url, **kwargs):
                posted_data.update(kwargs.get("data", {}))
                return _FakeHttpxResponse(json_data={"id_token": id_token})

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
        fake_client = CapturingClient(get_responses=[disc_resp, jwks_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            await oidc_provider.authenticate(code="my-auth-code", db=mock_db)

        assert posted_data["code"] == "my-auth-code"
        assert posted_data["client_id"] == "test-client-id"
        assert posted_data["client_secret"] == "test-client-secret"
        assert posted_data["redirect_uri"] == "https://app.example.com/callback"
        assert posted_data["grant_type"] == "authorization_code"


class TestOIDCAuthorizeUrl:
    async def test_get_authorize_url(self, oidc_provider, mock_settings):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url()

        assert "https://id.example.com/authorize" in url
        assert "client_id=test-client-id" in url
        assert "redirect_uri=" in url
        assert "response_type=code" in url
        assert "scope=openid" in url
        assert "state=" not in url

    async def test_get_authorize_url_with_state(self, oidc_provider, mock_settings):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url(state="random-state")

        assert "state=random-state" in url


class TestOIDCRoleMapping:
    def test_map_roles_admin_wins(self, oidc_provider):
        assert oidc_provider.map_roles(["user", "admin", "developer"]) == "admin"

    def test_map_roles_developer_wins_over_user(self, oidc_provider):
        assert oidc_provider.map_roles(["user", "developer"]) == "developer"

    def test_map_roles_user_default(self, oidc_provider):
        assert oidc_provider.map_roles(["user"]) == "user"

    def test_map_roles_unknown_role(self, oidc_provider):
        assert oidc_provider.map_roles(["unknown_role"]) == "user"

    def test_map_roles_empty_list(self, oidc_provider):
        assert oidc_provider.map_roles([]) == "user"

    def test_map_roles_case_insensitive(self, oidc_provider):
        assert oidc_provider.map_roles(["ADMIN"]) == "admin"
        assert oidc_provider.map_roles(["  Admin  "]) == "admin"


class TestOIDCRecentFixIsActive:
    async def test_new_user_created_with_is_active_true(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-active-check",
                "email": "active@example.com",
                "name": "Active User",
                "roles": ["user"],
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert len(created_users) == 1
        assert created_users[0].is_active is True

    async def test_new_user_hashed_password_is_none(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-pwd-check",
                "email": "pwd@example.com",
                "name": "Pwd User",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert len(created_users) == 1
        assert created_users[0].hashed_password is None

    async def test_new_user_auth_provider_is_oidc(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-provider-check",
                "email": "provider@example.com",
                "name": "Provider User",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert created_users[0].auth_provider == "oidc"
        assert created_users[0].external_id == "oidc-provider-check"

    async def test_db_flush_and_refresh_called_on_new_user(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-flush-check",
                "email": "flush@example.com",
                "name": "Flush User",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once()

    async def test_db_flush_not_called_for_existing_user(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        from engine.db.models import User

        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-existing-no-flush",
                "email": "existing-no-flush@example.com",
                "name": "Existing No Flush",
            },
        )

        existing_user = User(
            email="existing-no-flush@example.com",
            display_name="Existing No Flush",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="oidc-existing-no-flush",
        )
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        mock_db.flush.assert_not_called()
        mock_db.refresh.assert_not_called()
        mock_db.add.assert_not_called()


class TestOIDCEdgeCases:
    async def test_authenticate_empty_string_code(self, oidc_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await oidc_provider.authenticate(code="", db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_code_none_explicitly(self, oidc_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await oidc_provider.authenticate(code=None, db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_missing_id_token_in_response(
        self, oidc_provider, mock_settings
    ):
        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"access_token": "at-only"})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": []})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_authenticate_jwt_wrong_audience(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)

        claims = {
            "sub": "oidc-wrong-aud",
            "email": "wrong-aud@example.com",
            "name": "Wrong Aud",
            "aud": "wrong-audience",
        }
        id_token = _sign_id_token(claims, private_key, kid)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_authenticate_jwt_expired(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)

        from datetime import UTC, datetime

        claims = {
            "sub": "oidc-expired",
            "email": "expired@example.com",
            "name": "Expired",
            "aud": "test-client-id",
            "exp": datetime(2020, 1, 1, tzinfo=UTC),
        }
        id_token = _sign_id_token(claims, private_key, kid)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_authenticate_multiple_jwks_keys_second_matches(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)

        _other_private, other_pub = _generate_rsa_key_pair()
        other_jwk, _other_kid = _make_jwk_kid(other_pub)
        other_jwk["kid"] = "wrong-kid-000"

        claims = {
            "sub": "oidc-multi-key",
            "email": "multi@example.com",
            "name": "Multi Key",
            "aud": "test-client-id",
        }
        id_token = _sign_id_token(claims, private_key, kid)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(
            json_data={"keys": [other_jwk, jwk_dict]}
        )

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "multi@example.com"

    async def test_authenticate_name_is_none_falls_to_preferred_username(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-name-none",
                "email": "name-none@example.com",
                "name": None,
                "preferred_username": "prefname",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert created_users[0].display_name == "prefname"

    async def test_authenticate_name_is_empty_string_falls_to_preferred_username(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-name-empty",
                "email": "name-empty@example.com",
                "name": "",
                "preferred_username": "prefname2",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert created_users[0].display_name == "prefname2"

    async def test_authenticate_no_name_no_preferred_username_falls_to_email(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-email-only",
                "email": "user@domain.org",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert created_users[0].display_name == "user"

    async def test_authenticate_missing_role_claim_defaults_to_user(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-no-roles",
                "email": "noroles@example.com",
                "name": "No Roles",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert created_users[0].role == "user"

    async def test_authenticate_custom_role_claim_key(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        mock_settings.oidc_role_claim = "groups"

        fake_client = _build_full_mock_client(
            rsa_keys,
            {
                "sub": "oidc-custom-claim",
                "email": "custom@example.com",
                "name": "Custom Claim",
                "groups": ["admin"],
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created_users = []
        mock_db.add = MagicMock(side_effect=lambda u: created_users.append(u))

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is True
        assert created_users[0].role == "admin"

    async def test_authenticate_discovery_http_error_returns_generic_message(
        self, oidc_provider, mock_settings
    ):
        disc_resp = _FakeHttpxResponse(
            raise_error=httpx.HTTPStatusError(
                "Bad Gateway",
                request=MagicMock(),
                response=MagicMock(status_code=502),
            )
        )
        fake_client = _FakeAsyncClient(get_responses=[disc_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_authenticate_jwks_http_error_returns_generic_message(
        self, oidc_provider, mock_settings
    ):
        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        jwks_resp = _FakeHttpxResponse(
            raise_error=httpx.HTTPStatusError(
                "JWKS Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )
        )
        fake_client = _FakeAsyncClient(get_responses=[disc_resp, jwks_resp])

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error


class TestOIDCDiscoveryEdgeCases:
    async def test_discovery_ftp_scheme_rejected(self, oidc_provider, mock_settings):
        mock_settings.oidc_discovery_url = "ftp://example.com/.well-known"
        with pytest.raises(ValueError, match="HTTPS"):
            await oidc_provider._get_discovery()

    async def test_discovery_empty_url_rejected(self, oidc_provider, mock_settings):
        mock_settings.oidc_discovery_url = ""
        with pytest.raises(ValueError, match="HTTPS"):
            await oidc_provider._get_discovery()

    async def test_discovery_cache_persisted_across_calls(
        self, oidc_provider, mock_settings
    ):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            r1 = await oidc_provider._get_discovery()
            r2 = await oidc_provider._get_discovery()

        assert r1 is r2

    async def test_jwks_cache_persisted_across_calls(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        _, pub_key = rsa_keys
        jwk_dict, _ = _make_jwk_kid(pub_key)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
        fake_client = _FakeAsyncClient(get_responses=[disc_resp, jwks_resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            r1 = await oidc_provider._get_jwks()
            r2 = await oidc_provider._get_jwks()

        assert r1 is r2

    async def test_provider_initial_state(self, oidc_provider):
        assert oidc_provider._discovery_cache is None
        assert oidc_provider._jwks_cache is None


class TestOIDCAuthorizeUrlEdgeCases:
    async def test_authorize_url_with_empty_state(
        self, oidc_provider, mock_settings
    ):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url(state="")

        assert "state=" not in url

    async def test_authorize_url_with_special_chars_in_state(
        self, oidc_provider, mock_settings
    ):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url(state="abc123-_~")

        assert "state=abc123-_~" in url

    async def test_authorize_url_uses_cached_discovery(
        self, oidc_provider, mock_settings
    ):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url1 = await oidc_provider.get_authorize_url(state="first")
            url2 = await oidc_provider.get_authorize_url(state="second")

        assert "state=first" in url1
        assert "state=second" in url2
        assert len(fake_client._get_responses) == 0


class TestOIDCRoleMappingEdgeCases:
    def test_map_roles_all_unknown(self, oidc_provider):
        assert oidc_provider.map_roles(["foo", "bar", "baz"]) == "user"

    def test_map_roles_admin_developer_user_all_present(self, oidc_provider):
        assert oidc_provider.map_roles(["user", "developer", "admin"]) == "admin"

    def test_map_roles_developer_only(self, oidc_provider):
        assert oidc_provider.map_roles(["developer"]) == "developer"

    def test_map_roles_duplicate_roles(self, oidc_provider):
        assert oidc_provider.map_roles(["admin", "admin"]) == "admin"

    def test_map_roles_whitespace_only_role(self, oidc_provider):
        assert oidc_provider.map_roles(["   "]) == "user"

    def test_map_roles_mixed_case_priority(self, oidc_provider):
        result = oidc_provider.map_roles(["DEVELOPER", "Admin"])
        assert result == "admin"

    def test_map_roles_priority_order(self, oidc_provider):
        assert oidc_provider.map_roles(["user", "developer"]) == "developer"
        assert oidc_provider.map_roles(["developer", "admin"]) == "admin"
        assert oidc_provider.map_roles(["user", "admin"]) == "admin"
