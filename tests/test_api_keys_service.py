"""Unit tests for the API key issuance + verification service (gh#94)."""

from __future__ import annotations

import re

import pytest

from engine.api.auth.api_keys import (
    ApiKeyError,
    VALID_SCOPES,
    generate_token,
    hash_token,
    is_engine_token,
    normalise_scopes,
    split_token,
    verify_token,
)


class TestGenerateToken:
    def test_default_env(self):
        token = generate_token()
        assert re.match(r"^nxs_live_[0-9a-f]{32}$", token)

    def test_custom_env(self):
        token = generate_token(env="test")
        assert token.startswith("nxs_test_")

    def test_tokens_unique_across_calls(self):
        tokens = {generate_token() for _ in range(64)}
        assert len(tokens) == 64

    def test_invalid_env_rejected(self):
        with pytest.raises(ValueError):
            generate_token(env="")
        with pytest.raises(ValueError):
            generate_token(env="bad space")
        with pytest.raises(ValueError):
            generate_token(env="bad/slash")


class TestSplitToken:
    def test_returns_prefix_and_full(self):
        token = generate_token()
        prefix, full = split_token(token)
        assert prefix == token[:12]
        assert full == token

    def test_rejects_non_engine_token(self):
        with pytest.raises(ApiKeyError):
            split_token("not-an-engine-token")
        with pytest.raises(ApiKeyError):
            split_token("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.x.y")

    def test_rejects_too_short(self):
        with pytest.raises(ApiKeyError):
            split_token("nxs_")


class TestHashAndVerify:
    def test_roundtrip_succeeds(self):
        token = generate_token()
        hashed = hash_token(token)
        assert verify_token(token, hashed) is True

    def test_wrong_token_fails(self):
        a = generate_token()
        b = generate_token()
        hashed = hash_token(a)
        assert verify_token(b, hashed) is False

    def test_garbage_hash_returns_false_not_raise(self):
        token = generate_token()
        assert verify_token(token, "not-a-bcrypt-hash") is False


class TestNormaliseScopes:
    def test_default_is_read(self):
        assert normalise_scopes(None) == ["read"]
        assert normalise_scopes([]) == ["read"]

    def test_lower_and_strip(self):
        assert normalise_scopes([" Read ", "TRADE"]) == ["read", "trade"]

    def test_dedupes(self):
        assert normalise_scopes(["read", "read", "read"]) == ["read"]

    def test_unknown_scope_rejected(self):
        with pytest.raises(ValueError):
            normalise_scopes(["read", "wizard"])


class TestIsEngineToken:
    def test_engine_token_recognised(self):
        assert is_engine_token(generate_token()) is True

    def test_jwt_not_recognised(self):
        assert is_engine_token("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.body.sig") is False


class TestValidScopesContract:
    def test_documented_scopes_only(self):
        assert VALID_SCOPES == frozenset({"read", "trade", "admin"})
