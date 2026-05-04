"""Comprehensive tests for engine/api/auth/base.py, registry.py, and
additional OIDC/jwt edge-case branches exposed by branch=true coverage.

Targets:
  - IAuthProvider default method implementations (get_user_info, create_user)
  - AuthProviderRegistry: register, get, providers, ordered_names, authenticate
  - OIDCAuthProvider: additional branch edge cases
  - JWT: key rotation with no previous key set
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.api.auth.jwt import (
    ALGORITHM,
    create_access_token,
    decode_token,
    generate_refresh_token,
    get_refresh_token_expiry,
    hash_token,
)
from engine.api.auth.oidc import OIDCAuthProvider
from engine.api.auth.registry import AuthProviderRegistry
from engine.config import Settings


def _generate_rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_jwk_kid(pub_key) -> tuple[dict[str, Any], str]:
    kid = "test-kid-base-reg"
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


class _StubProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "stub"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult(success=True, user_info=UserInfo(email="stub@test.com"))


class _AnotherProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "another"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult(success=True, user_info=UserInfo(email="another@test.com"))


# ---------------------------------------------------------------------------
# IAuthProvider base class — default implementations
# ---------------------------------------------------------------------------


class TestIAuthProviderDefaults:
    async def test_get_user_info_returns_none(self):
        provider = _StubProvider()
        result = await provider.get_user_info("ext-123")
        assert result is None

    async def test_get_user_info_returns_none_different_id(self):
        provider = _StubProvider()
        result = await provider.get_user_info("ext-456")
        assert result is None

    async def test_create_user_returns_failure(self):
        provider = _StubProvider()
        result = await provider.create_user(UserInfo(email="x@y.com"))
        assert result.success is False
        assert "not supported" in result.error
        assert "stub" in result.error

    def test_map_roles_with_empty_list(self):
        provider = _StubProvider()
        assert provider.map_roles([]) == "user"

    def test_map_roles_admin_priority(self):
        provider = _StubProvider()
        assert provider.map_roles(["user", "admin"]) == "admin"

    def test_map_roles_developer_priority(self):
        provider = _StubProvider()
        assert provider.map_roles(["user", "developer"]) == "developer"

    def test_map_roles_unknown_stays_user(self):
        provider = _StubProvider()
        assert provider.map_roles(["unknown_role", "custom"]) == "user"

    def test_map_roles_case_insensitive_and_stripped(self):
        provider = _StubProvider()
        assert provider.map_roles(["  ADMIN  "]) == "admin"
        assert provider.map_roles(["Developer"]) == "developer"

    def test_map_roles_admin_beats_developer(self):
        provider = _StubProvider()
        assert provider.map_roles(["developer", "admin"]) == "admin"


class TestUserInfoDataclass:
    def test_default_values(self):
        info = UserInfo()
        assert info.external_id is None
        assert info.email == ""
        assert info.display_name == ""
        assert info.provider == "local"
        assert info.roles == ["user"]
        assert info.raw_claims == {}

    def test_custom_values(self):
        info = UserInfo(
            external_id="ext-1",
            email="test@test.com",
            display_name="Test User",
            provider="oidc",
            roles=["admin"],
            raw_claims={"sub": "ext-1"},
        )
        assert info.external_id == "ext-1"
        assert info.provider == "oidc"
        assert info.roles == ["admin"]

    def test_independent_roles_lists(self):
        info1 = UserInfo()
        info2 = UserInfo()
        info1.roles.append("admin")
        assert info2.roles == ["user"]


class TestAuthResultDataclass:
    def test_default_failure(self):
        result = AuthResult()
        assert result.success is False
        assert result.user_info is None
        assert result.error is None

    def test_success_result(self):
        result = AuthResult(success=True, user_info=UserInfo(email="a@b.com"))
        assert result.success is True
        assert result.user_info is not None

    def test_error_result(self):
        result = AuthResult(success=False, error="Something went wrong")
        assert result.error == "Something went wrong"


# ---------------------------------------------------------------------------
# AuthProviderRegistry
# ---------------------------------------------------------------------------


class TestAuthProviderRegistry:
    def test_register_and_get(self):
        registry = AuthProviderRegistry()
        provider = _StubProvider()
        registry.register(provider)
        assert registry.get("stub") is provider

    def test_get_unknown_returns_none(self):
        registry = AuthProviderRegistry()
        assert registry.get("nonexistent") is None

    def test_providers_property_returns_copy(self):
        registry = AuthProviderRegistry()
        registry.register(_StubProvider())
        p = registry.providers
        assert "stub" in p
        p["extra"] = None
        assert "extra" not in registry.providers

    def test_ordered_names_returns_copy(self):
        registry = AuthProviderRegistry()
        registry.register(_StubProvider())
        names = registry.ordered_names
        assert names == ["stub"]
        names.append("extra")
        assert registry.ordered_names == ["stub"]

    def test_register_preserves_order(self):
        registry = AuthProviderRegistry()
        registry.register(_StubProvider())
        registry.register(_AnotherProvider())
        assert registry.ordered_names == ["stub", "another"]

    def test_register_same_provider_does_not_duplicate(self):
        registry = AuthProviderRegistry()
        registry.register(_StubProvider())
        registry.register(_StubProvider())
        assert registry.ordered_names == ["stub"]
        assert len(registry.providers) == 1

    async def test_authenticate_delegates_to_provider(self):
        registry = AuthProviderRegistry()
        registry.register(_StubProvider())
        result = await registry.authenticate("stub")
        assert result.success is True
        assert result.user_info.email == "stub@test.com"

    async def test_authenticate_unknown_provider_returns_error(self):
        registry = AuthProviderRegistry()
        result = await registry.authenticate("nonexistent")
        assert result.success is False
        assert "Unknown provider" in result.error
        assert "nonexistent" in result.error

    async def test_authenticate_forwards_kwargs(self):
        call_kwargs = {}

        class _CapturingProvider(IAuthProvider):
            @property
            def name(self) -> str:
                return "capture"

            async def authenticate(self, **kwargs):
                call_kwargs.update(kwargs)
                return AuthResult(success=True)

        registry = AuthProviderRegistry()
        registry.register(_CapturingProvider())
        await registry.authenticate("capture", code="abc", db=MagicMock())
        assert call_kwargs["code"] == "abc"
        assert "db" in call_kwargs


# ---------------------------------------------------------------------------
# OIDC — additional branch edge cases
# ---------------------------------------------------------------------------


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


class TestOIDCEdgeCases:
    async def test_authenticate_empty_string_code(self, oidc_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await oidc_provider.authenticate(code="", db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_code_none(self, oidc_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await oidc_provider.authenticate(code=None, db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_authenticate_id_token_missing_from_response(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"access_token": "at-only"})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_authenticate_jwt_decode_fails(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)
        other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad_token = _sign_id_token(
            {"sub": "x", "email": "x@x.com", "aud": "wrong-audience"},
            other_private,
            kid,
        )

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": bad_token, "access_token": "at"})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_authenticate_no_kid_in_header(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        token_no_kid = jwt.encode(
            {"sub": "x", "email": "x@x.com", "aud": "test-client-id"},
            private_key,
            algorithm="RS256",
        )

        jwk_dict, kid = _make_jwk_kid(pub_key)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(
            json_data={"id_token": token_no_kid, "access_token": "at"}
        )
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="auth-code", db=mock_db)

        assert result.success is False

    async def test_authenticate_name_from_claims_name_field(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)
        id_token = _sign_id_token(
            {
                "sub": "oidc-name-test",
                "email": "name@example.com",
                "name": "Full Name",
                "preferred_username": "prefname",
                "aud": "test-client-id",
            },
            private_key,
            kid,
        )

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
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
        assert result.user_info.display_name == "Full Name"

    async def test_authenticate_name_false_uses_preferred_username(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        private_key, pub_key = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key)
        id_token = _sign_id_token(
            {
                "sub": "oidc-false-name",
                "email": "fn@example.com",
                "name": False,
                "preferred_username": "fallbackuser",
                "aud": "test-client-id",
            },
            private_key,
            kid,
        )

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})
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
        assert result.user_info.display_name == "fallbackuser"

    async def test_get_authorize_url_without_state_omits_state_param(
        self, oidc_provider, mock_settings
    ):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url()

        assert "state=" not in url
        assert "client_id=test-client-id" in url
        assert "response_type=code" in url

    async def test_get_authorize_url_with_empty_state_omits_state_param(
        self, oidc_provider, mock_settings
    ):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url(state="")

        assert "state=" not in url

    async def test_jwks_http_error_propagates(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        _, pub_key = rsa_keys
        jwk_dict, _ = _make_jwk_kid(pub_key)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        jwks_resp = _FakeHttpxResponse(
            raise_error=httpx.HTTPStatusError(
                "Not Found",
                request=MagicMock(),
                response=MagicMock(status_code=404),
            )
        )
        fake_client = _FakeAsyncClient(get_responses=[disc_resp, jwks_resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            with pytest.raises(httpx.HTTPStatusError):
                await oidc_provider._get_jwks()


# ---------------------------------------------------------------------------
# JWT — key rotation edge cases with branch coverage
# ---------------------------------------------------------------------------


class TestJWTKeyRotationBranches:
    def test_decode_with_no_previous_key_set(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "primary-secret-only")
        monkeypatch.setenv("NEXUS_SECRET_KEY_PREVIOUS", "")
        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(sub="u1", email="a@b.com", role="user")
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "u1"

    def test_decode_with_previous_key_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "primary-key")
        monkeypatch.delenv("NEXUS_SECRET_KEY_PREVIOUS", raising=False)
        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(sub="u2", email="b@c.com", role="admin")
        payload = decode_token(token)
        assert payload is not None

    def test_create_uses_default_expiry_when_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key-for-expiry")
        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(sub="u3", email="c@d.com", role="user")
        payload = decode_token(token)
        assert payload is not None
        expected_exp = datetime.now(tz=UTC) + timedelta(
            minutes=settings.jwt_access_token_expire_minutes
        )
        actual_exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
        assert abs((actual_exp - expected_exp).total_seconds()) < 5

    def test_decode_token_with_malformed_jwt_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key-malformed")
        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        assert decode_token("not-a-valid-jwt") is None

    def test_decode_token_empty_string_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key-empty")
        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        assert decode_token("") is None

    def test_hash_token_empty_string(self):
        h = hash_token("")
        assert len(h) == 64

    def test_hash_token_long_input(self):
        h = hash_token("a" * 10000)
        assert len(h) == 64

    def test_generate_refresh_token_is_hex(self):
        token = generate_refresh_token()
        int(token, 16)

    def test_get_refresh_token_expiry_default(self, monkeypatch):
        monkeypatch.setenv("NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7")
        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        expiry = get_refresh_token_expiry()
        now = datetime.now(tz=UTC)
        assert expiry > now
        assert (expiry - now).days >= 6

    def test_create_access_token_default_provider(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key-default-provider")
        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(sub="u4", email="d@e.com", role="user")
        payload = decode_token(token)
        assert payload is not None
        assert payload["provider"] == "local"
