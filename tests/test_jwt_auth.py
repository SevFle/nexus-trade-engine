from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from engine.api.auth.jwt import (
    ALGORITHM,
    create_access_token,
    decode_token,
    generate_refresh_token,
    get_refresh_token_expiry,
    hash_token,
)
from engine.config import Settings


@pytest.fixture
def mock_settings(monkeypatch):
    s = Settings(secret_key="test-secret-key-for-jwt-tests", secret_key_previous="")
    monkeypatch.setattr("engine.api.auth.jwt.settings", s)
    return s


@pytest.fixture
def mock_settings_with_rotation(monkeypatch):
    s = Settings(
        secret_key="new-secret-key",
        secret_key_previous="old-secret-key",
        jwt_access_token_expire_minutes=60,
        jwt_refresh_token_expire_days=7,
    )
    monkeypatch.setattr("engine.api.auth.jwt.settings", s)
    return s


class TestCreateAccessToken:
    def test_creates_valid_jwt(self, mock_settings):
        token = create_access_token(sub="user-1", email="a@b.com", role="admin")
        payload = jwt.decode(token, "test-secret-key-for-jwt-tests", algorithms=[ALGORITHM])
        assert payload["sub"] == "user-1"
        assert payload["email"] == "a@b.com"
        assert payload["role"] == "admin"
        assert payload["type"] == "access"

    def test_default_provider_is_local(self, mock_settings):
        token = create_access_token(sub="u", email="x@y.com", role="user")
        payload = jwt.decode(token, "test-secret-key-for-jwt-tests", algorithms=[ALGORITHM])
        assert payload["provider"] == "local"

    def test_custom_provider(self, mock_settings):
        token = create_access_token(sub="u", email="x@y.com", role="user", provider="google")
        payload = jwt.decode(token, "test-secret-key-for-jwt-tests", algorithms=[ALGORITHM])
        assert payload["provider"] == "google"

    def test_custom_expiry(self, mock_settings):
        delta = timedelta(minutes=5)
        token = create_access_token(
            sub="u", email="x@y.com", role="user", expires_delta=delta
        )
        payload = jwt.decode(token, "test-secret-key-for-jwt-tests", algorithms=[ALGORITHM])
        assert payload["exp"] - payload["iat"] == 300

    def test_default_expiry_from_settings(self, mock_settings):
        token = create_access_token(sub="u", email="x@y.com", role="user")
        payload = jwt.decode(token, "test-secret-key-for-jwt-tests", algorithms=[ALGORITHM])
        expected_delta = mock_settings.jwt_access_token_expire_minutes * 60
        assert abs((payload["exp"] - payload["iat"]) - expected_delta) <= 1

    def test_contains_iat(self, mock_settings):
        token = create_access_token(sub="u", email="x@y.com", role="user")
        payload = jwt.decode(token, "test-secret-key-for-jwt-tests", algorithms=[ALGORITHM])
        assert "iat" in payload
        assert isinstance(payload["iat"], (int, float))


class TestDecodeToken:
    def test_decode_valid_token(self, mock_settings):
        token = create_access_token(sub="user-1", email="a@b.com", role="admin")
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "user-1"
        assert payload["type"] == "access"

    def test_decode_returns_none_for_invalid_token(self, mock_settings):
        result = decode_token("not-a-valid-jwt")
        assert result is None

    def test_decode_returns_none_for_wrong_secret(self, mock_settings):
        token = jwt.encode(
            {"sub": "u", "type": "access", "exp": datetime.now(tz=UTC) + timedelta(hours=1)},
            "wrong-secret",
            algorithm=ALGORITHM,
        )
        assert decode_token(token) is None

    def test_decode_returns_none_for_expired_token(self, mock_settings):
        token = create_access_token(
            sub="u", email="x@y.com", role="user", expires_delta=timedelta(seconds=-1)
        )
        assert decode_token(token) is None

    def test_decode_returns_none_for_non_access_type(self, mock_settings):
        payload = {
            "sub": "u",
            "type": "refresh",
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, "test-secret-key-for-jwt-tests", algorithm=ALGORITHM)
        assert decode_token(token) is None

    def test_decode_with_key_rotation_old_key(self, mock_settings_with_rotation):
        token = jwt.encode(
            {
                "sub": "u",
                "type": "access",
                "exp": datetime.now(tz=UTC) + timedelta(hours=1),
            },
            "old-secret-key",
            algorithm=ALGORITHM,
        )
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "u"

    def test_decode_with_key_rotation_new_key(self, mock_settings_with_rotation):
        token = jwt.encode(
            {
                "sub": "u",
                "type": "access",
                "exp": datetime.now(tz=UTC) + timedelta(hours=1),
            },
            "new-secret-key",
            algorithm=ALGORITHM,
        )
        payload = decode_token(token)
        assert payload is not None

    def test_decode_without_previous_key(self, mock_settings):
        token = jwt.encode(
            {
                "sub": "u",
                "type": "access",
                "exp": datetime.now(tz=UTC) + timedelta(hours=1),
            },
            "test-secret-key-for-jwt-tests",
            algorithm=ALGORITHM,
        )
        payload = decode_token(token)
        assert payload is not None


class TestGenerateRefreshToken:
    def test_returns_hex_string(self):
        token = generate_refresh_token()
        assert isinstance(token, str)
        int(token, 16)

    def test_correct_length(self):
        token = generate_refresh_token()
        assert len(token) == 64

    def test_unique_tokens(self):
        tokens = {generate_refresh_token() for _ in range(100)}
        assert len(tokens) == 100


class TestHashToken:
    def test_returns_hex_string(self):
        h = hash_token("test-token")
        assert isinstance(h, str)
        assert len(h) == 64

    def test_deterministic(self):
        assert hash_token("abc") == hash_token("abc")

    def test_different_inputs_different_hashes(self):
        assert hash_token("a") != hash_token("b")


class TestGetRefreshTokenExpiry:
    def test_returns_future_datetime(self, mock_settings):
        expiry = get_refresh_token_expiry()
        assert expiry > datetime.now(tz=UTC)

    def test_default_7_days(self, mock_settings):
        before = datetime.now(tz=UTC) + timedelta(days=7)
        expiry = get_refresh_token_expiry()
        after = datetime.now(tz=UTC) + timedelta(days=7)
        assert before <= expiry <= after
