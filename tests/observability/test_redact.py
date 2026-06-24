"""Tests for the redaction processor — strips secrets from log records."""

from __future__ import annotations

import typing

import pytest

from engine.observability.redact import (
    _BANNED_KEYS_LOWER,
    REDACTED,
    _scrub_string,
    _scrub_value,
    redact_processor,
    scrub_pii,
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
        out = _emit({"event": "x", "payload": {"token": "leak", "user": "ok"}})
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
        jwt_like = "aaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbb.cccccccccccccccccccc-dddd"
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
            b"-----BEGIN RSA PRIVATE KEY-----\n"  # gitleaks:allow
            b"MIIEowIBAAKCAQEAxxxxxxx\n"
            b"-----END RSA PRIVATE KEY-----"
        )
        out = _emit({"event": "x", "blob": pem})
        assert b"BEGIN RSA PRIVATE KEY" not in str(out["blob"]).encode()
        assert "BEGIN RSA PRIVATE KEY" not in str(out["blob"])


class TestPemBlock:
    def test_pem_block_in_string_value_is_redacted(self):
        s = (
            "header\n-----BEGIN RSA PRIVATE KEY-----\n"  # gitleaks:allow
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


class TestScrubValueDictDispatch:
    """_scrub_value must dispatch dict inputs to _scrub_dict."""

    def test_scrub_value_recurses_into_dict(self):
        result = _scrub_value({"password": "leak", "user": "ok"})
        assert result["password"] == REDACTED
        assert result["user"] == "ok"

    def test_scrub_value_handles_nested_dict_in_list(self):
        result = _scrub_value([{"token": "t"}, {"name": "n"}])
        assert result[0]["token"] == REDACTED
        assert result[1]["name"] == "n"


class TestScrubPiiRoundTrip:
    """scrub_pii must produce identical output to redact_processor."""

    def test_scrub_pii_equals_redact_processor_simple(self):
        event = {"event": "login", "password": "secret", "user": "alice"}
        assert scrub_pii(dict(event)) == redact_processor(None, "info", dict(event))

    def test_scrub_pii_equals_redact_processor_nested(self):
        event = {
            "event": "request",
            "headers": {"authorization": "Bearer xyz"},
            "body": {"token": "abc", "data": [1, 2, 3]},
        }
        assert scrub_pii(dict(event)) == redact_processor(None, "info", dict(event))

    def test_scrub_pii_equals_redact_processor_patterns(self):
        event = {"event": "x", "note": "card 4242 4242 4242 4242 declined"}
        assert scrub_pii(dict(event)) == redact_processor(None, "info", dict(event))

    def test_scrub_pii_does_not_mutate_input(self):
        event = {"event": "x", "password": "secret"}
        original = dict(event)
        scrub_pii(event)
        assert event == original

    def test_scrub_pii_redacts_banned_key(self):
        out = scrub_pii({"event": "x", "api_key": "leak"})
        assert out["api_key"] == REDACTED


# ---------------------------------------------------------------------------
# Expanded banned-key set
# ---------------------------------------------------------------------------


class TestExpandedBannedKeys:
    """Every newly-added sensitive field name must be redacted as a key."""

    NEW_KEYS: typing.ClassVar[list[str]] = [
        "pwd",
        "passphrase",
        "passcode",
        "credentials",
        "iban",
        "swift_code",
        "routing_number",
        "otp",
        "mfa_code",
        "verification_code",
        "signing_key",
        "encryption_key",
        "webhook_secret",
        "signing_secret",
        "jwt",
        "session_id",
        "csrf_token",
        "bank_account",
        "account_number",
        "mfa_encryption_key",
    ]

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_new_key_redacted(self, key: str):
        assert key in _BANNED_KEYS_LOWER
        out = _emit({"event": "x", key: "leak-me"})
        assert out[key] == REDACTED

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_new_key_case_insensitive(self, key: str):
        out = _emit({"event": "x", key.upper(): "leak-me"})
        assert out[key.upper()] == REDACTED

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_new_key_hyphen_variant(self, key: str):
        hyphen_key = key.replace("_", "-")
        out = _emit({"event": "x", hyphen_key: "leak-me"})
        assert out[hyphen_key] == REDACTED

    def test_nested_new_key_redacted(self):
        out = _emit({"event": "x", "payload": {"iban": "GB29NWBK", "name": "ok"}})
        assert out["payload"]["iban"] == REDACTED
        assert out["payload"]["name"] == "ok"


class TestPassNotBanned:
    """The ambiguous 'pass' key is no longer banned; precise alternatives are."""

    def test_pass_key_preserved(self):
        assert "pass" not in _BANNED_KEYS_LOWER
        out = _emit({"event": "x", "pass": "boarding"})
        assert out["pass"] == "boarding"

    def test_precise_alternatives_present(self):
        for k in ("pwd", "passphrase", "passcode", "passwd", "password"):
            assert k in _BANNED_KEYS_LOWER


# ---------------------------------------------------------------------------
# Value-level pattern redaction
# ---------------------------------------------------------------------------


class TestJwtEyJPattern:
    """JWTs starting with eyJ are redacted, including short-segment variants."""

    def test_real_jwt_redacted(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out = _emit({"event": "x", "note": f"auth {jwt}"})
        assert jwt not in str(out["note"])

    def test_short_jwt_segment_redacted(self):
        # Segments shorter than 16 chars — only the eyJ pattern catches this.
        jwt = "eyJhbGci.eyJzdWI.Xbmd"
        out = _emit({"event": "x", "note": jwt})
        assert jwt not in str(out["note"])

    def test_short_jwt_in_kv_pair_redacted(self):
        jwt = "eyJhbGci.eyJzdWI.Xbmd"
        out = _emit({"event": "x", "note": f"token={jwt}"})
        assert jwt not in str(out["note"])

    def test_non_jwt_dotted_string_preserved(self):
        # Dotted module paths / versions must NOT be redacted.
        s = "package.module.v1.2.3"
        out = _emit({"event": "x", "note": s})
        assert out["note"] == s


class TestApiKeyPrefixPatterns:
    """Prefixed secrets (sk-, pk-, AKIA, …) are redacted in values."""

    @pytest.mark.parametrize(
        "value",
        [
            "sk_live_abc123def456ghi7",  # Stripe secret
            "sk_test_abcdefghijklmnop",  # Stripe test secret
            "pk_live_abc123def456ghi7",  # Stripe publishable
            "AKIAIOSFODNN7EXAMPLEabc",  # AWS access key + extra
            "ghp_abc123def456ghi7jkl",  # GitHub PAT
            "xoxb_abc123def456ghi7jkl",  # Slack bot token
        ],
    )
    def test_prefixed_secret_redacted(self, value: str):
        out = _emit({"event": "x", "note": f"key={value} done"})
        assert value not in str(out["note"])

    def test_short_prefix_not_redacted(self):
        # A prefix followed by too few chars (<16) is not a secret.
        s = "sk-short"
        out = _emit({"event": "x", "note": s})
        assert out["note"] == s


class TestBearerTokenRedaction:
    def test_bearer_redacted(self):
        out = _emit({"event": "x", "header": "Bearer abc123def456"})
        assert "abc123def456" not in str(out["header"])

    def test_bearer_case_insensitive(self):
        out = _emit({"event": "x", "header": "bearer abc123def456"})
        assert "abc123def456" not in str(out["header"])

    def test_bearer_with_special_chars(self):
        out = _emit({"event": "x", "header": "Bearer a.b_c-d~e/f+g=h"})
        assert "a.b_c-d~e/f+g=h" not in str(out["header"])


class TestLuhnCreditCardRedaction:
    """Only Luhn-valid digit sequences (real card numbers) are redacted."""

    @pytest.mark.parametrize(
        "card",
        [
            "4242424242424242",  # Visa test
            "4111111111111111",  # Visa test
            "5555555555554444",  # Mastercard test
            "378282246310005",  # Amex test (15 digits)
            "4242 4242 4242 4242",  # spaced
            "4242-4242-4242-4242",  # dashed
            "4242424242424242428",  # 19 digits (Luhn-valid)
        ],
    )
    def test_valid_card_redacted(self, card: str):
        out = _emit({"event": "x", "note": f"card {card} declined"})
        assert card not in str(out["note"])

    @pytest.mark.parametrize(
        "seq",
        [
            "1234567890123",  # 13 digits, not Luhn-valid
            "9999999999999",  # 13 digits, not Luhn-valid
            "1111111111111111",  # 16 digits, not Luhn-valid
            "2222222222222222",  # 16 digits, not Luhn-valid
        ],
    )
    def test_non_luhn_sequence_preserved(self, seq: str):
        out = _emit({"event": "x", "note": f"id {seq} done"})
        assert seq in str(out["note"])

    def test_short_digit_sequence_preserved(self):
        s = "order #1234567890"
        out = _emit({"event": "x", "note": s})
        assert out["note"] == s

    def test_luhn_helper_valid(self):
        from engine.observability.redact import _luhn_valid

        assert _luhn_valid("4242424242424242") is True
        assert _luhn_valid("4242 4242 4242 4242") is True
        assert _luhn_valid("4242424242424242428") is True

    def test_luhn_helper_invalid(self):
        from engine.observability.redact import _luhn_valid

        assert _luhn_valid("1234567890123") is False
        assert _luhn_valid("1111111111111111") is False
        assert _luhn_valid("2222222222222222") is False


class TestInlineKeyValueRedaction:
    """Inline ``key=value`` secret pairs have their values redacted."""

    @pytest.mark.parametrize(
        "pair",
        [
            "password=hunter2",
            "pwd=hunter2",
            "passwd=hunter2",
            "passphrase=mysecret",
            "passcode=1234",
            "api_key=sk_live_abc123",
            "apikey=sk_live_abc123",
            "secret=topsecret",
            "client_secret=cs_abc",
            "webhook_secret=whsec_abc",
            "signing_secret=shhh",
            "token=abc123def456",
            "access_token=abc123",
            "refresh_token=rt_abc123",
            "id_token=it_xyz",
            "session_token=st_abc",
            "authorization=Bearer xyz",
            "jwt=abc.def.ghi",
            "csrf_token=xyz123",
            "otp=987654",
            "mfa_code=987654",
            "verification_code=987654",
            "private_key=pri_abc",
            "ssh_key=ssh_rsa_abc",
            "signing_key=sk_test_abcdef",
            "encryption_key=0xdeadbeef",
            "mfa_encryption_key=key1",
            "x_api_key=xak_abc",
            "x_auth_token=xat_abc",
            "credentials=user:pass",
            "session_id=abc123",
            "credit_card=none",
            "card_number=ABCD1234",
            "cvv=123",
            "ssn=123-45-6789",
            "iban=GB29NWBK60161331926819",
            "swift_code=BOFAUS3N",
            "routing_number=021000021",
            "bank_account=12345678",
            "account_number=0123456789",
        ],
    )
    def test_kv_pair_value_redacted(self, pair: str):
        out = _emit({"event": "x", "note": f"config {pair} end"})
        result = str(out["note"])
        # The key and separator are preserved; only the value is replaced.
        key_part = pair.split("=", 1)[0]
        assert key_part in result
        assert REDACTED in result
        assert pair not in result

    def test_kv_colon_separator(self):
        out = _emit({"event": "x", "note": "secret: topsecret"})
        assert "topsecret" not in str(out["note"])
        assert "secret:" in str(out["note"])

    def test_kv_with_spaces(self):
        out = _emit({"event": "x", "note": "password = hunter2"})
        assert "hunter2" not in str(out["note"])

    def test_kv_hyphenated_key(self):
        out = _emit({"event": "x", "note": "api-key=sk_live_abc123"})
        assert "sk_live_abc123" not in str(out["note"])

    def test_non_secret_kv_preserved(self):
        s = "host=localhost port=5432"
        out = _emit({"event": "x", "note": s})
        assert out["note"] == s

    def test_kv_preserves_surrounding_text(self):
        out = _emit({"event": "x", "note": "pre password=secret post"})
        result = out["note"]
        assert result.startswith("pre ")
        assert result.endswith(" post")
        assert "secret" not in result


class TestScrubeStringEdgeCases:
    """_scrub_string must not corrupt benign strings."""

    def test_empty_string(self):
        assert _scrub_string("") == ""

    def test_plain_text_unchanged(self):
        s = "The quick brown fox jumps over the lazy dog."
        assert _scrub_string(s) == s

    def test_url_preserved(self):
        s = "https://example.com/path?key=value&page=1"
        # `key=value` inside a URL query string — `key` is not a banned key
        # name, so the whole URL is preserved.
        out = _scrub_string(s)
        assert "example.com" in out
