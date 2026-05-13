"""Tests for engine.api.auth.api_keys — API key lifecycle.

Improves coverage for token generation, hashing, verification, and
scope normalization.
"""

from __future__ import annotations

import pytest

from engine.api.auth.api_keys import (
    VALID_SCOPES,
    ApiKeyError,
    generate_token,
    hash_token,
    is_engine_token,
    normalise_scopes,
    split_token,
    verify_token,
)


class TestTokenGeneration:
    def test_format(self):
        tok = generate_token(env="test")
        assert tok.startswith("nxs_test_")

    def test_custom_env(self):
        tok = generate_token(env="live")
        assert tok.startswith("nxs_live_")

    def test_invalid_env_empty(self):
        with pytest.raises(ValueError, match="non-empty"):
            generate_token(env="")

    def test_invalid_env_special_chars(self):
        with pytest.raises(ValueError):
            generate_token(env="test!@#")

    def test_tokens_are_unique(self):
        t1 = generate_token()
        t2 = generate_token()
        assert t1 != t2


class TestSplitToken:
    def test_valid_token(self):
        tok = generate_token(env="live")
        prefix, full = split_token(tok)
        assert prefix == tok[:12]
        assert full == tok

    def test_short_token_raises(self):
        with pytest.raises(ApiKeyError):
            split_token("nxs_live_ab")

    def test_non_engine_token_raises(self):
        with pytest.raises(ApiKeyError):
            split_token("not_engine_token_here_at_least_12")


class TestIsEngineToken:
    def test_engine_token(self):
        assert is_engine_token("nxs_live_abc123") is True

    def test_jwt_token(self):
        assert is_engine_token("eyJhbGciOi") is False

    def test_empty(self):
        assert is_engine_token("") is False


class TestHashAndVerify:
    def test_hash_verify_roundtrip(self):
        tok = generate_token()
        hashed = hash_token(tok)
        assert verify_token(tok, hashed)

    def test_wrong_token_fails(self):
        tok = generate_token()
        hashed = hash_token(tok)
        assert not verify_token("nxs_live_wrongtoken", hashed)

    def test_malformed_hash_returns_false(self):
        assert not verify_token("sometoken", "not-a-hash")


class TestNormaliseScopes:
    def test_default_scope(self):
        assert normalise_scopes(None) == ["read"]
        assert normalise_scopes([]) == ["read"]

    def test_valid_scopes(self):
        assert normalise_scopes(["read", "trade"]) == ["read", "trade"]

    def test_dedup(self):
        assert normalise_scopes(["read", "read"]) == ["read"]

    def test_case_insensitive(self):
        assert normalise_scopes(["Read", "TRADE"]) == ["read", "trade"]

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError, match="unknown scope"):
            normalise_scopes(["superuser"])

    def test_admin_scope(self):
        assert normalise_scopes(["admin"]) == ["admin"]

    def test_whitespace_trimmed(self):
        assert normalise_scopes([" read "]) == ["read"]


class TestValidScopes:
    def test_contains_expected(self):
        assert "read" in VALID_SCOPES
        assert "trade" in VALID_SCOPES
        assert "admin" in VALID_SCOPES
