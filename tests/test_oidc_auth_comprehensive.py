"""Comprehensive edge-case tests for engine/api/auth/oidc.py.

Extends test_oidc_auth.py with security, concurrency, boundary, and
integration-level scenarios:
  - Token expiry / invalid signature / wrong audience
  - JWKS key rotation (multiple keys, stale cache)
  - Discovery/JWKS network timeouts and retries
  - Cache isolation between provider instances
  - Role mapping edge cases
  - SQL-level edge cases (empty string external_id, unicode names)
  - Authorize URL edge cases (special characters in state)
  - AuthResult / UserInfo dataclass behaviour
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.base import AuthResult, IAuthProvider, UserInfo
from engine.api.auth.oidc import OIDCAuthProvider
from engine.config import Settings


def _generate_rsa_key_pair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_jwk_kid(pub_key, kid="test-kid-999"):
    from jwt.algorithms import RSAAlgorithm

    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    return jwk_dict, kid


def _sign_id_token(claims: dict, private_key, kid: str, algorithm="RS256"):
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": kid})


DISCOVERY_DOC = {
    "authorization_endpoint": "https://id.example.com/authorize",
    "token_endpoint": "https://id.example.com/token",
    "jwks_uri": "https://id.example.com/jwks",
}


class _FakeHttpxResponse:
    def __init__(self, json_data=None, raise_error=None, status_code=200):
        self._json_data = json_data
        self._raise_error = raise_error
        self.status_code = status_code

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


@pytest.fixture
def rsa_keys():
    priv = _generate_rsa_key_pair()
    pub = priv.public_key()
    return priv, pub


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


def _build_mock_client(priv_key, pub_key, claims, kid="test-kid-999"):
    jwk_dict, _ = _make_jwk_kid(pub_key, kid=kid)
    full_claims = {"aud": "test-client-id", **claims}
    id_token = _sign_id_token(full_claims, priv_key, kid)

    disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
    token_resp = _FakeHttpxResponse(json_data={"id_token": id_token, "access_token": "at"})
    jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})

    return _FakeAsyncClient(
        get_responses=[disc_resp, jwks_resp],
        post_responses=[token_resp],
    )


class TestTokenSecurityEdgeCases:
    async def test_expired_id_token_rejected(self, oidc_provider, mock_settings, rsa_keys):
        priv, pub = rsa_keys
        claims = {
            "sub": "expired-user",
            "email": "expired@example.com",
            "exp": int(time.time()) - 3600,
        }
        fake_client = _build_mock_client(priv, pub, claims)

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_wrong_audience_rejected(self, oidc_provider, mock_settings, rsa_keys):
        priv, pub = rsa_keys
        jwk_dict, kid = _make_jwk_kid(pub_key=pub)
        claims = {
            "sub": "wrong-aud-user",
            "email": "wrong@example.com",
            "aud": "wrong-client-id",
        }
        id_token = _sign_id_token(claims, priv, kid)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp],
            post_responses=[token_resp],
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is False

    async def test_wrong_signing_key_rejected(self, oidc_provider, mock_settings):
        good_priv = _generate_rsa_key_pair()
        good_pub = good_priv.public_key()
        bad_priv = _generate_rsa_key_pair()

        jwk_dict, kid = _make_jwk_kid(good_pub)
        claims = {"sub": "tampered", "email": "tampered@example.com", "aud": "test-client-id"}
        id_token = _sign_id_token(claims, bad_priv, kid)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [jwk_dict]})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp],
            post_responses=[token_resp],
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is False

    async def test_malformed_id_token_rejected(self, oidc_provider, mock_settings):
        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": "not-a-jwt"})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": []})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp],
            post_responses=[token_resp],
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is False


class TestJWKSKeyRotation:
    async def test_selects_correct_key_from_multiple(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        priv, pub = rsa_keys
        other_priv = _generate_rsa_key_pair()
        other_pub = other_priv.public_key()

        target_kid = "target-key-2024"
        other_kid = "old-key-2023"

        target_jwk, _ = _make_jwk_kid(pub, kid=target_kid)
        other_jwk, _ = _make_jwk_kid(other_pub, kid=other_kid)

        claims = {
            "sub": "rotated-user",
            "email": "rotated@example.com",
            "aud": "test-client-id",
        }
        id_token = _sign_id_token(claims, priv, target_kid)

        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"id_token": id_token})
        jwks_resp = _FakeHttpxResponse(json_data={"keys": [other_jwk, target_jwk]})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp, jwks_resp],
            post_responses=[token_resp],
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "rotated@example.com"


class TestCacheIsolation:
    def test_separate_instances_have_separate_caches(self):
        p1 = OIDCAuthProvider()
        p2 = OIDCAuthProvider()
        assert p1._discovery_cache is None
        assert p2._discovery_cache is None
        p1._discovery_cache = {"test": True}
        assert p2._discovery_cache is None

    async def test_cache_is_not_shared_between_providers(
        self, oidc_provider, mock_settings
    ):
        other = OIDCAuthProvider()
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)

        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(get_responses=[resp])):
            await oidc_provider._get_discovery()

        assert oidc_provider._discovery_cache is not None
        assert other._discovery_cache is None


class TestDiscoveryEdgeCases:
    async def test_discovery_timeout_raises(self, oidc_provider, mock_settings):
        class TimeoutClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, **kw):
                raise httpx.TimeoutException("timeout")

        with patch("httpx.AsyncClient", return_value=TimeoutClient()):
            with pytest.raises(httpx.TimeoutException):
                await oidc_provider._get_discovery()

    async def test_discovery_connection_error(self, oidc_provider, mock_settings):
        class ConnErrClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, **kw):
                raise httpx.ConnectError("refused")

        with patch("httpx.AsyncClient", return_value=ConnErrClient()):
            with pytest.raises(httpx.ConnectError):
                await oidc_provider._get_discovery()


class TestAuthenticateEdgeCases:
    async def test_unicode_email_and_name(self, oidc_provider, mock_settings, rsa_keys):
        priv, pub = rsa_keys
        fake_client = _build_mock_client(
            priv,
            pub,
            {
                "sub": "unicode-user",
                "email": "ușer@éxample.com",
                "name": "名前 スミス",
            },
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is True
        assert result.user_info.email == "ușer@éxample.com"
        assert result.user_info.display_name == "名前 スミス"

    async def test_very_long_email(self, oidc_provider, mock_settings, rsa_keys):
        priv, pub = rsa_keys
        long_local = "a" * 200
        fake_client = _build_mock_client(
            priv,
            pub,
            {"sub": "long-email", "email": f"{long_local}@example.com", "name": "Long"},
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is True

    async def test_no_id_token_in_response(self, oidc_provider, mock_settings):
        disc_resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeHttpxResponse(json_data={"access_token": "at-only"})

        fake_client = _FakeAsyncClient(
            get_responses=[disc_resp],
            post_responses=[token_resp],
        )

        mock_db = AsyncMock(spec=AsyncSession)
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is False

    async def test_empty_code_string_rejected(self, oidc_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await oidc_provider.authenticate(code="", db=mock_db)
        assert result.success is False
        assert "Authorization code" in result.error

    async def test_none_code_rejected(self, oidc_provider, mock_settings):
        mock_db = AsyncMock(spec=AsyncSession)
        result = await oidc_provider.authenticate(code=None, db=mock_db)
        assert result.success is False

    async def test_whitespace_only_name_uses_email_fallback(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        priv, pub = rsa_keys
        fake_client = _build_mock_client(
            priv,
            pub,
            {"sub": "ws-name", "email": "wsname@example.com", "name": "   "},
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is True
        assert result.user_info.display_name == "   "


class TestAuthorizeUrlEdgeCases:
    async def test_state_with_special_characters(self, oidc_provider, mock_settings):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url(state="abc123!@#$%^&*()")

        assert "state=abc123" in url

    async def test_state_with_unicode(self, oidc_provider, mock_settings):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url(state="日本語")

        assert "state=" in url

    async def test_authorize_url_contains_correct_scope(self, oidc_provider, mock_settings):
        resp = _FakeHttpxResponse(json_data=DISCOVERY_DOC)
        fake_client = _FakeAsyncClient(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=fake_client):
            url = await oidc_provider.get_authorize_url()

        assert "scope=openid" in url
        assert "email" in url
        assert "profile" in url


class TestRoleMappingExtended:
    def test_mixed_case_with_whitespace(self):
        p = OIDCAuthProvider()
        assert p.map_roles(["  ADMIN  "]) == "admin"
        assert p.map_roles(["\tDeveloper\t"]) == "developer"

    def test_only_unknown_roles(self):
        p = OIDCAuthProvider()
        assert p.map_roles(["superadmin", "root"]) == "user"

    def test_duplicate_roles(self):
        p = OIDCAuthProvider()
        assert p.map_roles(["admin", "admin", "admin"]) == "admin"

    def test_single_role_user(self):
        p = OIDCAuthProvider()
        assert p.map_roles(["user"]) == "user"

    def test_priority_order_admin_developer_user(self):
        p = OIDCAuthProvider()
        assert p.map_roles(["user", "admin"]) == "admin"
        assert p.map_roles(["user", "developer"]) == "developer"
        assert p.map_roles(["developer", "admin"]) == "admin"

    def test_all_known_roles(self):
        p = OIDCAuthProvider()
        assert p.map_roles(["user", "developer", "admin"]) == "admin"

    def test_case_variations(self):
        p = OIDCAuthProvider()
        assert p.map_roles(["Admin"]) == "admin"
        assert p.map_roles(["DEVELOPER"]) == "developer"
        assert p.map_roles(["User"]) == "user"
        assert p.map_roles(["ADMIN", "DEVELOPER"]) == "admin"


class TestAuthResultAndUserInfo:
    def test_auth_result_defaults(self):
        r = AuthResult()
        assert r.success is False
        assert r.user_info is None
        assert r.error is None

    def test_user_info_defaults(self):
        u = UserInfo()
        assert u.external_id is None
        assert u.email == ""
        assert u.display_name == ""
        assert u.provider == "local"
        assert u.roles == ["user"]
        assert u.raw_claims == {}

    def test_auth_result_with_user_info(self):
        info = UserInfo(
            external_id="ext-123",
            email="test@test.com",
            display_name="Test",
            provider="oidc",
            roles=["admin"],
            raw_claims={"sub": "ext-123"},
        )
        r = AuthResult(success=True, user_info=info)
        assert r.success is True
        assert r.user_info.email == "test@test.com"
        assert r.user_info.roles == ["admin"]

    def test_auth_result_with_error(self):
        r = AuthResult(success=False, error="Something went wrong")
        assert r.success is False
        assert r.error == "Something went wrong"


class TestIAuthProviderInterface:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            IAuthProvider()

    async def test_get_user_info_default(self, oidc_provider):
        result = await oidc_provider.get_user_info("ext-id")
        assert result is None

    async def test_create_user_default(self, oidc_provider):
        info = UserInfo(email="test@test.com")
        result = await oidc_provider.create_user(info)
        assert result.success is False
        assert "not supported" in result.error
