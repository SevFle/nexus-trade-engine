"""Tests for the redaction processor — strips secrets from log records."""

from __future__ import annotations

import pytest

from engine.observability.redact import (
    _BANNED_KEYS_LOWER,
    REDACTED,
    redact_processor,
)


def _emit(event_dict: dict) -> dict:
    return redact_processor(None, "info", dict(event_dict))


class TestExactKeyRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "Password",
            "PASSWORD",
            "token",
            "api_key",
            "apiKey",
            "secret",
            "authorization",
            "credit_card",
            "creditCard",
            "ssn",
            "access_token",
            "refresh_token",
            "client_secret",
        ],
    )
    def test_banned_key_replaced_with_redacted(self, key: str):
        out = _emit({"event": "login", key: "leak-me"})
        assert out[key] == REDACTED
        assert out["event"] == "login"

    def test_non_sensitive_key_preserved(self):
        out = _emit({"event": "login", "user_id": "u-1", "ip": "1.2.3.4"})
        assert out["user_id"] == "u-1"
        assert out["ip"] == "1.2.3.4"


class TestNestedRedaction:
    def test_outer_banned_key_replaces_whole_value(self):
        # `auth` is itself a banned key; the entire payload it holds is
        # treated as sensitive and replaced wholesale.
        out = _emit({"event": "x", "auth": {"token": "leak", "user": "ok"}})
        assert out["auth"] == REDACTED

    def test_nested_dict_redacts_inner_banned_key(self):
        # When the outer key is *not* banned, recursion redacts the
        # banned inner key while preserving siblings.
        out = _emit(
            {"event": "x", "payload": {"token": "leak", "user": "ok"}}
        )
        assert out["payload"]["token"] == REDACTED
        assert out["payload"]["user"] == "ok"

    def test_list_of_dicts_redacted(self):
        out = _emit({"event": "x", "items": [{"password": "p"}, {"name": "n"}]})
        assert out["items"][0]["password"] == REDACTED
        assert out["items"][1]["name"] == "n"


class TestPatternRedaction:
    def test_authorization_bearer_in_value_is_redacted(self):
        out = _emit({"event": "x", "header": "Bearer eyJhbGciOi..."})
        assert "eyJhbGciOi" not in str(out["header"])

    def test_jwt_like_string_value_redacted(self):
        # Real JWT segments are 16+ chars; the regex requires that to
        # avoid matching dotted module paths.
        jwt_like = (
            "aaaaaaaaaaaaaaaaaaaa."
            "bbbbbbbbbbbbbbbbbbbb."
            "cccccccccccccccc-dddd"
        )
        out = _emit({"event": "x", "note": f"token={jwt_like}"})
        assert jwt_like not in str(out["note"])

    def test_credit_card_number_redacted_in_value(self):
        out = _emit({"event": "x", "note": "card 4242 4242 4242 4242 declined"})
        assert "4242 4242 4242 4242" not in str(out["note"])


class TestPreservesShape:
    def test_processor_returns_dict(self):
        original = {"event": "x", "password": "secret"}
        result = redact_processor(None, "info", original)
        assert isinstance(result, dict)
        assert "event" in result
        assert "password" in result


class TestBannedKeyAllCovered:
    """Auto-covers every banned key without needing manual updates."""

    @pytest.mark.parametrize("key", sorted(_BANNED_KEYS_LOWER))
    def test_each_banned_key_redacted(self, key: str):
        out = _emit({"event": "x", key: "leak-me"})
        assert out[key] == REDACTED


class TestBytesValue:
    def test_bytes_secret_is_scrubbed(self):
        pem = (
            b"-----BEGIN RSA PRIVATE KEY-----\n"
            b"MIIEowIBAAKCAQEAxxxxxxx\n"
            b"-----END RSA PRIVATE KEY-----"
        )
        out = _emit({"event": "x", "blob": pem})
        assert b"BEGIN RSA PRIVATE KEY" not in str(out["blob"]).encode()
        assert "BEGIN RSA PRIVATE KEY" not in str(out["blob"])


class TestPemBlock:
    def test_pem_block_in_string_value_is_redacted(self):
        s = (
            "header\n-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIE...secret\n"
            "-----END RSA PRIVATE KEY-----\nfooter"
        )
        out = _emit({"event": "x", "data": s})
        assert "BEGIN RSA PRIVATE KEY" not in out["data"]
        assert "MIIE...secret" not in out["data"]
        assert "header" in out["data"]
        assert "footer" in out["data"]


class TestNonStringKey:
    def test_non_string_banned_key_still_redacts(self):
        # Edge case: structlog may receive an Enum key from upstream code
        from enum import Enum

        class K(Enum):
            TOKEN = "token"

        out = _emit({"event": "x", K.TOKEN.value: "leak"})
        assert out["token"] == REDACTED
