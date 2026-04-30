"""Tests for engine.api.auth.mfa — TOTP enrollment + verification."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlparse

import pytest

from engine.api.auth.mfa import (
    MFAError,
    _hotp,
    generate_totp_secret,
    totp_uri,
    verify_totp,
)

_RFC_SECRET_B32 = base64.b32encode(b"12345678901234567890").decode("ascii")


class TestSecretGeneration:
    def test_generated_secret_is_base32(self):
        s = generate_totp_secret()
        decoded = base64.b32decode(s, casefold=False)
        assert len(decoded) >= 16

    def test_each_call_unique(self):
        a = generate_totp_secret()
        b = generate_totp_secret()
        assert a != b


class TestHOTP:
    def test_rfc_4226_vectors(self):
        # RFC 4226 Appendix D published HOTP values for secret
        # "12345678901234567890". Counters 0..9 -> these 6-digit codes.
        expected = [
            "755224",
            "287082",
            "359152",
            "969429",
            "338314",
            "254676",
            "287922",
            "162583",
            "399871",
            "520489",
        ]
        for counter, code in enumerate(expected):
            assert _hotp(_RFC_SECRET_B32, counter, digits=6) == code


class TestVerifyTOTP:
    def test_correct_code_within_window(self):
        secret = _RFC_SECRET_B32
        ok = verify_totp(secret, "287082", now=59, step=30, window=0)
        assert ok is True

    def test_wrong_code_rejected(self):
        secret = _RFC_SECRET_B32
        ok = verify_totp(secret, "000000", now=59, step=30, window=0)
        assert ok is False

    def test_window_accepts_previous_code(self):
        secret = _RFC_SECRET_B32
        ok = verify_totp(secret, "287082", now=89, step=30, window=1)
        assert ok is True

    def test_window_zero_strict(self):
        secret = _RFC_SECRET_B32
        ok = verify_totp(secret, "287082", now=89, step=30, window=0)
        assert ok is False

    def test_non_digit_code_rejected(self):
        secret = _RFC_SECRET_B32
        ok = verify_totp(secret, "abcdef", now=59, step=30, window=1)
        assert ok is False

    def test_wrong_length_code_rejected(self):
        secret = _RFC_SECRET_B32
        ok = verify_totp(secret, "12345", now=59, step=30, window=1)
        assert ok is False
        ok = verify_totp(secret, "1234567", now=59, step=30, window=1)
        assert ok is False


class TestURI:
    def test_uri_has_otpauth_scheme(self):
        uri = totp_uri(
            secret="JBSWY3DPEHPK3PXP",
            account="alice@example.com",
            issuer="Nexus",
        )
        assert uri.startswith("otpauth://totp/")

    def test_uri_includes_issuer_and_account(self):
        uri = totp_uri(
            secret="JBSWY3DPEHPK3PXP",
            account="alice@example.com",
            issuer="Nexus",
        )
        parsed = urlparse(uri)
        assert "Nexus" in parsed.path
        assert (
            "alice%40example.com" in parsed.path
            or "alice@example.com" in parsed.path
        )
        params = parse_qs(parsed.query)
        assert params["secret"] == ["JBSWY3DPEHPK3PXP"]
        assert params["issuer"] == ["Nexus"]


class TestValidation:
    def test_invalid_secret_b32_rejected(self):
        with pytest.raises(MFAError):
            verify_totp("not-base32!!!", "123456", now=0, step=30, window=0)

    def test_negative_window_rejected(self):
        with pytest.raises(MFAError):
            verify_totp(
                _RFC_SECRET_B32, "123456", now=0, step=30, window=-1
            )

    def test_zero_step_rejected(self):
        with pytest.raises(MFAError):
            verify_totp(_RFC_SECRET_B32, "123456", now=0, step=0, window=1)

    def test_uri_rejects_empty_account(self):
        with pytest.raises(MFAError):
            totp_uri(secret="JBSWY3DPEHPK3PXP", account="", issuer="Nexus")
