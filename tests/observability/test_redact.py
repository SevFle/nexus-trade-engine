"""Tests for the redaction processor — strips secrets from log records."""

from __future__ import annotations

import pytest

from engine.observability.redact import REDACTED, redact_processor


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
    def test_nested_dict_redacted(self):
        out = _emit({"event": "x", "auth": {"token": "leak", "user": "ok"}})
        assert out["auth"]["token"] == REDACTED
        assert out["auth"]["user"] == "ok"

    def test_list_of_dicts_redacted(self):
        out = _emit({"event": "x", "items": [{"password": "p"}, {"name": "n"}]})
        assert out["items"][0]["password"] == REDACTED
        assert out["items"][1]["name"] == "n"


class TestPatternRedaction:
    def test_authorization_bearer_in_value_is_redacted(self):
        out = _emit({"event": "x", "header": "Bearer eyJhbGciOi..."})
        assert "eyJhbGciOi" not in str(out["header"])

    def test_jwt_like_string_value_redacted(self):
        jwt_like = "aaaaaaaaaa.bbbbbbbbbb.cccccccccc-dddddd_eeee"
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
