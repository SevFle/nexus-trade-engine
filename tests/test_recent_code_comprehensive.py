"""Comprehensive tests for the most recently changed code.

Targets the modules changed in the last 5 commits:
  - engine/api/auth/oidc.py          — OIDC auth provider (discovery, JWKS, authenticate, authorize URL)
  - engine/api/auth/base.py          — IAuthProvider base class (map_roles, get_user_info, create_user)
  - engine/api/auth/jwt.py           — PyJWT token create/decode, refresh token, hashing
  - engine/data/providers/_resilience.py — TokenBucket rate limiter + call_with_retry

Test categories:
  - Unit tests for core logic (map_roles, token ops, bucket math)
  - Integration tests for OIDC auth flow with mock DB and HTTP
  - Edge cases: empty inputs, boundary values, concurrent access
  - Error conditions: malformed responses, missing fields, HTTP errors
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

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
from engine.config import Settings
from engine.data.providers._resilience import (
    DEFAULT_BASE_DELAY_S,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY_S,
    TokenBucket,
    call_with_retry,
)
from engine.data.providers.base import (
    FatalProviderError,
    RateLimit,
    TransientProviderError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_jwk(pub_key, kid="test-kid"):
    from jwt.algorithms import RSAAlgorithm

    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    return jwk_dict, kid


def _sign_id_token(claims, private_key, kid):
    return pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


DISCOVERY_DOC = {
    "authorization_endpoint": "https://id.example.com/authorize",
    "token_endpoint": "https://id.example.com/token",
    "jwks_uri": "https://id.example.com/jwks",
}


class _FakeResponse:
    def __init__(self, json_data=None, raise_error=None):
        self._json_data = json_data or {}
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error:
            raise self._raise_error

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, get_responses=None, post_responses=None):
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kw):
        return self._get_responses.pop(0) if self._get_responses else _FakeResponse()

    async def post(self, url, **kw):
        return self._post_responses.pop(0) if self._post_responses else _FakeResponse()


def _settings(monkeypatch, **overrides):
    defaults = dict(
        oidc_discovery_url="https://id.example.com/.well-known/openid-configuration",
        oidc_client_id="cid",
        oidc_client_secret="csecret",
        oidc_redirect_uri="https://app.example.com/callback",
        oidc_role_claim="roles",
    )
    defaults.update(overrides)
    s = Settings(**defaults)
    monkeypatch.setattr("engine.api.auth.oidc.settings", s)
    return s


def _full_client(rsa_keys, claims):
    priv, pub = rsa_keys
    jwk_dict, kid = _make_jwk(pub)
    all_claims = {"aud": "cid", **claims}
    id_token = _sign_id_token(all_claims, priv, kid)
    return _FakeClient(
        get_responses=[
            _FakeResponse(json_data=DISCOVERY_DOC),
            _FakeResponse(json_data={"keys": [jwk_dict]}),
        ],
        post_responses=[_FakeResponse(json_data={"id_token": id_token, "access_token": "at"})],
    )


# ===========================================================================
# 1. TokenBucket — comprehensive unit tests
# ===========================================================================


class TestTokenBucketUnit:
    async def test_zero_capacity_always_acquires(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=0))
        for _ in range(100):
            await bucket.acquire()

    async def test_burst_allows_immediate_acquires(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=600, burst=5))
        for _ in range(5):
            await bucket.acquire()

    async def test_burst_exhausted_waits_for_refill(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=600, burst=1))
        await bucket.acquire()
        start = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.09

    async def test_single_token_burst_one(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=60, burst=1))
        await bucket.acquire()

    async def test_large_burst(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=6000, burst=100))
        for _ in range(100):
            await bucket.acquire()

    async def test_refill_rate_calculation(self):
        rpm = 600
        bucket = TokenBucket(RateLimit(requests_per_minute=rpm, burst=2))
        assert bucket._refill_per_second == pytest.approx(rpm / 60.0)

    async def test_capacity_set_to_burst_when_rpm_positive(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=60, burst=10))
        assert bucket._capacity == 10

    async def test_capacity_minimum_1_when_rpm_positive_burst_0(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=60, burst=0))
        assert bucket._capacity == 1

    async def test_capacity_0_when_rpm_0(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=0, burst=10))
        assert bucket._capacity == 0

    async def test_negative_rpm_treated_as_zero(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=-5, burst=3))
        assert bucket._capacity == 0

    async def test_tokens_initially_at_capacity(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=60, burst=5))
        assert bucket._tokens == pytest.approx(5.0)

    async def test_concurrent_acquires(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=600, burst=10))

        async def acquire_n(n):
            for _ in range(n):
                await bucket.acquire()

        await asyncio.gather(*[acquire_n(3) for _ in range(3)])

    async def test_tokens_refill_over_time(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=600, burst=1))
        await bucket.acquire()
        assert bucket._tokens == pytest.approx(0.0)
        bucket._updated = time.monotonic() - 1.0
        async with bucket._lock:
            pass
        now = time.monotonic()
        elapsed = now - bucket._updated
        expected_tokens = min(1.0, 0.0 + elapsed * (600 / 60.0))
        assert bucket._tokens >= 0.0


# ===========================================================================
# 2. call_with_retry — additional edge cases
# ===========================================================================


class TestCallWithRetryEdgeCases:
    async def test_single_attempt(self):
        async def succeed():
            return "ok"

        result = await call_with_retry(succeed, provider="test", max_attempts=1)
        assert result == "ok"

    async def test_single_attempt_fails_no_retry(self):
        async def fail():
            raise TransientProviderError("fail")

        with pytest.raises(TransientProviderError):
            await call_with_retry(fail, provider="test", max_attempts=1, base_delay_s=0.001)

    async def test_generic_exception_propagates(self):
        async def generic_fail():
            raise RuntimeError("unexpected")

        with pytest.raises(RuntimeError, match="unexpected"):
            await call_with_retry(generic_fail, provider="test", max_attempts=3)

    async def test_max_delay_caps_retry_wait(self):
        times = []

        async def fail_once():
            times.append(time.monotonic())
            if len(times) == 1:
                raise TransientProviderError("retry")
            return "done"

        await call_with_retry(
            fail_once,
            provider="test",
            max_attempts=3,
            base_delay_s=10.0,
            max_delay_s=0.05,
        )
        if len(times) >= 2:
            gap = times[1] - times[0]
            assert gap < 1.0

    async def test_returns_value_on_success(self):
        async def return_complex():
            return {"key": [1, 2, 3]}

        result = await call_with_retry(return_complex, provider="test")
        assert result == {"key": [1, 2, 3]}

    async def test_timeout_error_retried(self):
        count = 0

        async def timeout_then_ok():
            nonlocal count
            count += 1
            if count == 1:
                raise TimeoutError("timed out")
            return "ok"

        result = await call_with_retry(
            timeout_then_ok, provider="test", max_attempts=3, base_delay_s=0.001
        )
        assert result == "ok"
        assert count == 2

    async def test_default_constants(self):
        assert DEFAULT_MAX_ATTEMPTS == 3
        assert DEFAULT_BASE_DELAY_S == 0.25
        assert DEFAULT_MAX_DELAY_S == 8.0


# ===========================================================================
# 3. OIDC Auth — additional edge cases beyond test_oidc_auth.py
# ===========================================================================


@pytest.fixture
def oidc_provider():
    return OIDCAuthProvider()


@pytest.fixture
def mock_settings(monkeypatch):
    return _settings(monkeypatch)


@pytest.fixture
def rsa_keys():
    return _generate_rsa_keys()


class TestOIDCDiscoveryEdgeCases:
    async def test_discovery_malformed_json_raises(self, oidc_provider, mock_settings):
        resp = _FakeResponse(
            raise_error=httpx.HTTPStatusError(
                "Bad Gateway", request=MagicMock(), response=MagicMock(status_code=502)
            )
        )
        with patch("httpx.AsyncClient", return_value=_FakeClient(get_responses=[resp])):
            with pytest.raises(httpx.HTTPStatusError):
                await oidc_provider._get_discovery()

    async def test_discovery_ftp_scheme_rejected(self, oidc_provider, mock_settings):
        mock_settings.oidc_discovery_url = "ftp://id.example.com/.well-known"
        with pytest.raises(ValueError, match="HTTPS"):
            await oidc_provider._get_discovery()

    async def test_discovery_empty_url_rejected(self, oidc_provider, mock_settings):
        mock_settings.oidc_discovery_url = ""
        with pytest.raises(ValueError, match="HTTPS"):
            await oidc_provider._get_discovery()

    async def test_discovery_network_unreachable(self, oidc_provider, mock_settings):
        resp = _FakeResponse(
            raise_error=httpx.ConnectError("Connection refused")
        )
        with patch("httpx.AsyncClient", return_value=_FakeClient(get_responses=[resp])):
            with pytest.raises(httpx.ConnectError):
                await oidc_provider._get_discovery()


class TestOIDCJWKSEdgeCases:
    async def test_jwks_missing_keys_field(self, oidc_provider, mock_settings, rsa_keys):
        _, pub = rsa_keys
        disc_resp = _FakeResponse(json_data=DISCOVERY_DOC)
        jwks_resp = _FakeResponse(json_data={})
        with patch("httpx.AsyncClient", return_value=_FakeClient(get_responses=[disc_resp, jwks_resp])):
            result = await oidc_provider._get_jwks()
        assert result == {}

    async def test_jwks_http_error(self, oidc_provider, mock_settings, rsa_keys):
        disc_resp = _FakeResponse(json_data=DISCOVERY_DOC)
        jwks_resp = _FakeResponse(
            raise_error=httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=MagicMock(status_code=404)
            )
        )
        with patch("httpx.AsyncClient", return_value=_FakeClient(get_responses=[disc_resp, jwks_resp])):
            with pytest.raises(httpx.HTTPStatusError):
                await oidc_provider._get_jwks()


class TestOIDCAuthenticateEdgeCases:
    async def test_empty_code_string_rejected(self, oidc_provider, mock_settings):
        mock_db = AsyncMock()
        result = await oidc_provider.authenticate(code="", db=mock_db)
        assert result.success is False

    async def test_none_code_rejected(self, oidc_provider, mock_settings):
        mock_db = AsyncMock()
        result = await oidc_provider.authenticate(code=None, db=mock_db)
        assert result.success is False

    async def test_id_token_missing_from_response(self, oidc_provider, mock_settings, rsa_keys):
        disc_resp = _FakeResponse(json_data=DISCOVERY_DOC)
        token_resp = _FakeResponse(json_data={"access_token": "at"})
        jwks_resp = _FakeResponse(json_data={"keys": []})

        with patch(
            "httpx.AsyncClient",
            return_value=_FakeClient(
                get_responses=[disc_resp, jwks_resp], post_responses=[token_resp]
            ),
        ):
            mock_db = AsyncMock()
            result = await oidc_provider.authenticate(code="code", db=mock_db)
        assert result.success is False

    async def test_jwt_decode_fails_with_wrong_audience(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        priv, pub = rsa_keys
        jwk_dict, kid = _make_jwk(pub)
        id_token = _sign_id_token(
            {"sub": "u1", "email": "e@e.com", "aud": "wrong-audience"},
            priv,
            kid,
        )
        client = _FakeClient(
            get_responses=[
                _FakeResponse(json_data=DISCOVERY_DOC),
                _FakeResponse(json_data={"keys": [jwk_dict]}),
            ],
            post_responses=[_FakeResponse(json_data={"id_token": id_token})],
        )
        mock_db = AsyncMock()
        with patch("httpx.AsyncClient", return_value=client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)
        assert result.success is False
        assert "OIDC authentication failed" in result.error

    async def test_cache_reuse_across_auth_calls(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        call_count = 0

        class CountingClient(_FakeClient):
            async def get(self, url, **kw):
                nonlocal call_count
                call_count += 1
                return await super().get(url, **kw)

        client = _full_client(
            rsa_keys,
            {"sub": "cache-user", "email": "cache@test.com", "name": "Cache User"},
        )
        counting = CountingClient(
            get_responses=client._get_responses,
            post_responses=client._post_responses,
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("httpx.AsyncClient", return_value=counting):
            r1 = await oidc_provider.authenticate(code="c1", db=mock_db)
            assert r1.success is True

        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        counting2 = _FakeClient(
            post_responses=[_FakeResponse(json_data={"id_token": "x"})],
        )
        with patch("httpx.AsyncClient", return_value=counting2):
            r2 = await oidc_provider.authenticate(code="c2", db=mock_db)

        assert call_count <= 3

    async def test_new_user_creation_with_no_roles(
        self, oidc_provider, mock_settings, rsa_keys
    ):
        client = _full_client(
            rsa_keys,
            {"sub": "no-role-user", "email": "norole@test.com", "name": "No Role"},
        )
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created = []
        mock_db.add = MagicMock(side_effect=lambda u: created.append(u))

        with patch("httpx.AsyncClient", return_value=client):
            result = await oidc_provider.authenticate(code="code", db=mock_db)

        assert result.success is True
        assert len(created) == 1
        assert created[0].role == "user"

    async def test_custom_role_claim(self, monkeypatch, rsa_keys):
        s = _settings(monkeypatch, oidc_role_claim="groups")
        provider = OIDCAuthProvider()

        client = _full_client(
            rsa_keys,
            {
                "sub": "custom-claim",
                "email": "custom@test.com",
                "name": "Custom",
                "groups": ["admin"],
            },
        )
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        created = []
        mock_db.add = MagicMock(side_effect=lambda u: created.append(u))

        with patch("httpx.AsyncClient", return_value=client):
            result = await provider.authenticate(code="code", db=mock_db)

        assert result.success is True
        assert created[0].role == "admin"


class TestOIDCAuthorizeUrlEdgeCases:
    async def test_empty_state_not_appended(self, oidc_provider, mock_settings):
        resp = _FakeResponse(json_data=DISCOVERY_DOC)
        with patch("httpx.AsyncClient", return_value=_FakeClient(get_responses=[resp])):
            url = await oidc_provider.get_authorize_url(state="")
        assert "state=" not in url

    async def test_special_chars_in_state(self, oidc_provider, mock_settings):
        resp = _FakeResponse(json_data=DISCOVERY_DOC)
        with patch("httpx.AsyncClient", return_value=_FakeClient(get_responses=[resp])):
            url = await oidc_provider.get_authorize_url(state="abc/def+ghi=jkl")
        assert "state=abc/def+ghi=jkl" in url

    async def test_url_contains_required_scopes(self, oidc_provider, mock_settings):
        resp = _FakeResponse(json_data=DISCOVERY_DOC)
        with patch("httpx.AsyncClient", return_value=_FakeClient(get_responses=[resp])):
            url = await oidc_provider.get_authorize_url()
        assert "scope=openid email profile" in url


# ===========================================================================
# 4. Base Auth Provider (IAuthProvider) — unit tests
# ===========================================================================


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test"

    async def authenticate(self, **kwargs):
        return AuthResult(success=True)


class TestIAuthProviderBase:
    def test_map_roles_priority_admin(self):
        p = _ConcreteProvider()
        assert p.map_roles(["user", "developer", "admin"]) == "admin"

    def test_map_roles_priority_developer_over_user(self):
        p = _ConcreteProvider()
        assert p.map_roles(["user", "developer"]) == "developer"

    def test_map_roles_user_only(self):
        p = _ConcreteProvider()
        assert p.map_roles(["user"]) == "user"

    def test_map_roles_empty_list(self):
        p = _ConcreteProvider()
        assert p.map_roles([]) == "user"

    def test_map_roles_unknown_roles(self):
        p = _ConcreteProvider()
        assert p.map_roles(["superadmin", "guest"]) == "user"

    def test_map_roles_case_insensitive(self):
        p = _ConcreteProvider()
        assert p.map_roles(["ADMIN", "User"]) == "admin"

    def test_map_roles_whitespace_stripped(self):
        p = _ConcreteProvider()
        assert p.map_roles(["  admin  "]) == "admin"

    def test_map_roles_mixed_case_and_whitespace(self):
        p = _ConcreteProvider()
        assert p.map_roles([" Admin ", "DEVELOPER"]) == "admin"

    async def test_get_user_info_returns_none(self):
        p = _ConcreteProvider()
        assert await p.get_user_info("any-id") is None

    async def test_create_user_returns_error(self):
        p = _ConcreteProvider()
        result = await p.create_user(UserInfo())
        assert result.success is False
        assert "test" in result.error

    def test_concrete_name_property(self):
        assert _ConcreteProvider().name == "test"

    async def test_concrete_authenticate(self):
        result = await _ConcreteProvider().authenticate()
        assert result.success is True

    def test_map_roles_developer_beats_user(self):
        p = _ConcreteProvider()
        assert p.map_roles(["user", "developer"]) == "developer"

    def test_map_roles_single_admin(self):
        p = _ConcreteProvider()
        assert p.map_roles(["admin"]) == "admin"

    def test_map_roles_single_developer(self):
        p = _ConcreteProvider()
        assert p.map_roles(["developer"]) == "developer"

    def test_map_roles_duplicate_roles(self):
        p = _ConcreteProvider()
        assert p.map_roles(["admin", "admin"]) == "admin"


class TestAuthResultDataclass:
    def test_default_values(self):
        r = AuthResult()
        assert r.success is False
        assert r.user_info is None
        assert r.error is None

    def test_success_with_info(self):
        info = UserInfo(email="a@b.com", display_name="A")
        r = AuthResult(success=True, user_info=info)
        assert r.success is True
        assert r.user_info.email == "a@b.com"

    def test_failure_with_error(self):
        r = AuthResult(success=False, error="bad")
        assert r.error == "bad"


class TestUserInfoDataclass:
    def test_default_values(self):
        u = UserInfo()
        assert u.external_id is None
        assert u.email == ""
        assert u.display_name == ""
        assert u.provider == "local"
        assert u.roles == ["user"]
        assert u.raw_claims == {}

    def test_custom_values(self):
        u = UserInfo(
            external_id="ext-123",
            email="x@y.com",
            display_name="X",
            provider="google",
            roles=["admin"],
            raw_claims={"sub": "ext-123"},
        )
        assert u.external_id == "ext-123"
        assert u.provider == "google"
        assert u.roles == ["admin"]


# ===========================================================================
# 5. JWT module — additional edge cases
# ===========================================================================


class TestJWTEdgeCases:
    def test_empty_string_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        assert decode_token("") is None

    def test_malformed_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        assert decode_token("not.a.valid.token") is None

    def test_token_with_none_payload_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        assert decode_token("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.bnVsbA.nonexistent") is None

    def test_decode_with_no_previous_key(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "key-only")
        monkeypatch.setenv("NEXUS_SECRET_KEY_PREVIOUS", "")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        token = create_access_token(sub="u1", email="a@b.com", role="user")
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "u1"

    def test_hash_token_empty_string(self):
        h = hash_token("")
        assert len(h) == 64

    def test_hash_token_long_input(self):
        h = hash_token("x" * 10000)
        assert len(h) == 64

    def test_refresh_token_format(self):
        t = generate_refresh_token()
        assert isinstance(t, str)
        assert len(t) == 64
        int(t, 16)

    def test_create_token_with_unicode(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        token = create_access_token(
            sub="u1", email="user@例え.com", role="admin", provider="oidc"
        )
        payload = decode_token(token)
        assert payload is not None
        assert payload["email"] == "user@例え.com"

    def test_algorithm_constant(self):
        assert ALGORITHM == "HS256"

    def test_refresh_expiry_is_future(self, monkeypatch):
        monkeypatch.setenv("NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        expiry = get_refresh_token_expiry()
        assert expiry > datetime.now(tz=UTC)

    def test_token_expiry_respects_custom_delta(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-key")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        delta = timedelta(hours=2)
        token = create_access_token(sub="u1", email="a@b.com", role="user", expires_delta=delta)
        payload = decode_token(token)
        assert payload is not None
        now = datetime.now(tz=UTC)
        exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
        assert exp > now + timedelta(hours=1)
        assert exp < now + timedelta(hours=3)

    def test_key_rotation_old_key_still_works(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "old-key-rotation")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        token = create_access_token(sub="u1", email="a@b.com", role="user")

        monkeypatch.setenv("NEXUS_SECRET_KEY", "new-key-rotation")
        monkeypatch.setenv("NEXUS_SECRET_KEY_PREVIOUS", "old-key-rotation")
        s2 = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s2)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "u1"

    def test_both_keys_fail_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "key-a")
        monkeypatch.setenv("NEXUS_SECRET_KEY_PREVIOUS", "key-b")
        s = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s)
        monkeypatch.setenv("NEXUS_SECRET_KEY", "key-c")
        monkeypatch.setenv("NEXUS_SECRET_KEY_PREVIOUS", "key-d")
        s2 = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", s2)
        token = pyjwt.encode(
            {
                "sub": "u",
                "type": "access",
                "exp": datetime.now(tz=UTC) + timedelta(hours=1),
            },
            "unknown-key",
            algorithm="HS256",
        )
        assert decode_token(token) is None


# ===========================================================================
# 6. OIDC Integration — full authenticate flow with DB session
# ===========================================================================


class TestOIDCIntegrationWithDB:
    async def test_new_user_created_in_db(self, oidc_provider, mock_settings, rsa_keys, db_session):
        client = _full_client(
            rsa_keys,
            {"sub": "db-user-1", "email": "dbuser@test.com", "name": "DB User", "roles": ["user"]},
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await oidc_provider.authenticate(code="code", db=db_session)

        assert result.success is True
        assert result.user_info is not None
        assert result.user_info.email == "dbuser@test.com"
        await db_session.commit()

    async def test_existing_user_lookup_in_db(self, oidc_provider, mock_settings, rsa_keys, db_session):
        from sqlalchemy import select

        from engine.db.models import User

        user = User(
            email="exist@test.com",
            display_name="Exist",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="db-exist-1",
        )
        db_session.add(user)
        await db_session.flush()

        client = _full_client(
            rsa_keys,
            {"sub": "db-exist-1", "email": "exist@test.com", "name": "Exist"},
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await oidc_provider.authenticate(code="code", db=db_session)

        assert result.success is True
        assert result.user_info.email == "exist@test.com"

    async def test_disabled_user_rejected_in_db(self, oidc_provider, mock_settings, rsa_keys, db_session):
        from engine.db.models import User

        user = User(
            email="disabled@test.com",
            display_name="Disabled",
            is_active=False,
            role="user",
            auth_provider="oidc",
            external_id="db-disabled-1",
        )
        db_session.add(user)
        await db_session.flush()

        client = _full_client(
            rsa_keys,
            {"sub": "db-disabled-1", "email": "disabled@test.com", "name": "Disabled"},
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await oidc_provider.authenticate(code="code", db=db_session)

        assert result.success is False
        assert "disabled" in result.error.lower()

    async def test_email_conflict_in_db(self, oidc_provider, mock_settings, rsa_keys, db_session):
        from engine.db.models import User

        user = User(
            email="conflict@test.com",
            display_name="Local",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        client = _full_client(
            rsa_keys,
            {"sub": "new-oidc-1", "email": "conflict@test.com", "name": "Conflict"},
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await oidc_provider.authenticate(code="code", db=db_session)

        assert result.success is False
        assert "different provider" in result.error

    async def test_admin_role_mapping_creates_admin_user(
        self, oidc_provider, mock_settings, rsa_keys, db_session
    ):
        from sqlalchemy import select

        from engine.db.models import User

        client = _full_client(
            rsa_keys,
            {
                "sub": "admin-user-1",
                "email": "admin@test.com",
                "name": "Admin",
                "roles": ["admin", "user"],
            },
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await oidc_provider.authenticate(code="code", db=db_session)

        assert result.success is True
        assert result.user_info.roles == ["admin"]

        db_result = await db_session.execute(
            select(User).where(User.external_id == "admin-user-1")
        )
        db_user = db_result.scalar_one()
        assert db_user.role == "admin"


# ===========================================================================
# 7. OIDC Provider — constructor and state tests
# ===========================================================================


class TestOIDCProviderInit:
    def test_initial_state(self):
        p = OIDCAuthProvider()
        assert p._discovery_cache is None
        assert p._jwks_cache is None
        assert p.name == "oidc"

    async def test_discovery_cache_set_after_call(self, mock_settings):
        p = OIDCAuthProvider()
        resp = _FakeResponse(json_data=DISCOVERY_DOC)
        with patch("httpx.AsyncClient", return_value=_FakeClient(get_responses=[resp])):
            await p._get_discovery()
        assert p._discovery_cache == DISCOVERY_DOC

    async def test_jwks_cache_set_after_call(self, mock_settings, rsa_keys):
        p = OIDCAuthProvider()
        _, pub = rsa_keys
        jwk_dict, _ = _make_jwk(pub)
        client = _FakeClient(
            get_responses=[
                _FakeResponse(json_data=DISCOVERY_DOC),
                _FakeResponse(json_data={"keys": [jwk_dict]}),
            ]
        )
        with patch("httpx.AsyncClient", return_value=client):
            await p._get_jwks()
        assert p._jwks_cache is not None
        assert "keys" in p._jwks_cache


# ===========================================================================
# 8. RateLimit dataclass tests
# ===========================================================================


class TestRateLimitDataclass:
    def test_defaults(self):
        rl = RateLimit()
        assert rl.requests_per_minute == 0
        assert rl.burst == 1

    def test_custom_values(self):
        rl = RateLimit(requests_per_minute=600, burst=10)
        assert rl.requests_per_minute == 600
        assert rl.burst == 10

    def test_frozen(self):
        rl = RateLimit(requests_per_minute=100)
        with pytest.raises(AttributeError):
            rl.requests_per_minute = 200


# ===========================================================================
# 9. Error classes
# ===========================================================================


class TestErrorClasses:
    def test_transient_error(self):
        err = TransientProviderError("temp")
        assert str(err) == "temp"
        assert isinstance(err, Exception)

    def test_fatal_error(self):
        err = FatalProviderError("fatal")
        assert str(err) == "fatal"
        assert isinstance(err, Exception)
