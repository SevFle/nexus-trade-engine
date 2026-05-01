"""Tests for MFA service: enroll/verify/backup-codes (gh#126)."""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from engine.api.auth import mfa_service


@pytest.fixture(autouse=True)
def _configure_mfa_settings(monkeypatch):
    monkeypatch.setattr(
        mfa_service.settings, "mfa_encryption_key", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(mfa_service.settings, "secret_key", "x" * 64)
    monkeypatch.setattr(mfa_service.settings, "mfa_challenge_ttl_seconds", 300)
    monkeypatch.setattr(mfa_service.settings, "mfa_backup_codes_count", 5)


class TestEncryption:
    def test_encrypt_decrypt_round_trip(self):
        from engine.api.auth.mfa import generate_totp_secret

        secret = generate_totp_secret()
        ct = mfa_service.encrypt_secret(secret)
        assert ct != secret
        assert mfa_service.decrypt_secret(ct) == secret

    def test_decrypt_rejects_tampered_token(self):
        from engine.api.auth.mfa import generate_totp_secret

        ct = mfa_service.encrypt_secret(generate_totp_secret())
        with pytest.raises(mfa_service.MFAServiceError):
            mfa_service.decrypt_secret(ct[:-4] + "AAAA")

    def test_encrypt_requires_key(self, monkeypatch):
        monkeypatch.setattr(mfa_service.settings, "mfa_encryption_key", "")
        with pytest.raises(mfa_service.MFAServiceError):
            mfa_service.encrypt_secret("JBSWY3DPEHPK3PXP")


class TestBackupCodes:
    def test_generate_returns_unique_strings(self):
        codes = mfa_service.generate_backup_codes(5)
        assert len(set(codes)) == 5
        for c in codes:
            assert len(c) == 10
            assert all(ch in "0123456789abcdef" for ch in c)

    def test_hash_returns_bcrypt_storage(self):
        plaintext = mfa_service.generate_backup_codes(3)
        storage = mfa_service.hash_backup_codes(plaintext)
        assert storage["version"] == 1
        assert len(storage["codes"]) == 3
        for entry in storage["codes"]:
            assert entry["hash"].startswith("$2")
            assert entry["used_at"] is None

    def test_verify_consumes_single_use(self):
        plaintext = mfa_service.generate_backup_codes(2)
        storage = mfa_service.hash_backup_codes(plaintext)
        ok, new_storage = mfa_service.verify_backup_code(storage, plaintext[0])
        assert ok
        assert new_storage is not None
        assert new_storage["codes"][0]["used_at"] is not None
        ok2, _ = mfa_service.verify_backup_code(new_storage, plaintext[0])
        assert ok2 is False

    def test_verify_rejects_unknown_code(self):
        storage = mfa_service.hash_backup_codes(
            mfa_service.generate_backup_codes(1)
        )
        ok, _ = mfa_service.verify_backup_code(storage, "deadbeef00")
        assert ok is False

    def test_verify_rejects_empty_storage(self):
        ok, _ = mfa_service.verify_backup_code(None, "deadbeef00")
        assert ok is False


class TestEnrollment:
    def test_begin_enrollment_returns_uri_and_secret(self):
        artifact = mfa_service.begin_enrollment(account="alice@example.com")
        assert artifact.secret_b32
        assert "otpauth://totp/" in artifact.otpauth_uri
        assert "alice@example.com" in artifact.otpauth_uri

    def test_confirm_enrollment_round_trip(self):
        artifact = mfa_service.begin_enrollment(account="alice@example.com")
        from engine.api.auth.mfa import _hotp

        counter = int(time.time() // 30)
        code = _hotp(artifact.secret_b32, counter)
        confirmed = mfa_service.confirm_enrollment(
            secret_b32=artifact.secret_b32, code=code
        )
        assert (
            mfa_service.decrypt_secret(confirmed.encrypted_secret)
            == artifact.secret_b32
        )
        assert len(confirmed.backup_codes_plaintext) == 5
        assert confirmed.backup_codes_storage["version"] == 1

    def test_confirm_rejects_wrong_code(self):
        artifact = mfa_service.begin_enrollment(account="alice@example.com")
        with pytest.raises(mfa_service.MFAServiceError):
            mfa_service.confirm_enrollment(
                secret_b32=artifact.secret_b32, code="000000"
            )


class TestVerifyLoginCode:
    def _enroll(self):
        artifact = mfa_service.begin_enrollment(account="alice@example.com")
        from engine.api.auth.mfa import _hotp

        counter = int(time.time() // 30)
        code = _hotp(artifact.secret_b32, counter)
        confirmed = mfa_service.confirm_enrollment(
            secret_b32=artifact.secret_b32, code=code
        )
        return artifact, confirmed

    def test_totp_path_succeeds(self):
        artifact, confirmed = self._enroll()
        from engine.api.auth.mfa import _hotp

        counter = int(time.time() // 30)
        live_code = _hotp(artifact.secret_b32, counter)
        ok, new_codes = mfa_service.verify_login_code(
            encrypted_secret=confirmed.encrypted_secret,
            code=live_code,
            backup_codes=confirmed.backup_codes_storage,
        )
        assert ok is True
        assert new_codes is None

    def test_backup_code_path_succeeds_and_consumes(self):
        _, confirmed = self._enroll()
        backup = confirmed.backup_codes_plaintext[0]
        ok, new_codes = mfa_service.verify_login_code(
            encrypted_secret=confirmed.encrypted_secret,
            code=backup,
            backup_codes=confirmed.backup_codes_storage,
        )
        assert ok is True
        assert new_codes is not None
        ok2, _ = mfa_service.verify_login_code(
            encrypted_secret=confirmed.encrypted_secret,
            code=backup,
            backup_codes=new_codes,
        )
        assert ok2 is False

    def test_invalid_code_rejected(self):
        _, confirmed = self._enroll()
        ok, _ = mfa_service.verify_login_code(
            encrypted_secret=confirmed.encrypted_secret,
            code="000000",
            backup_codes=confirmed.backup_codes_storage,
        )
        assert ok is False

    def test_empty_code_rejected(self):
        _, confirmed = self._enroll()
        ok, _ = mfa_service.verify_login_code(
            encrypted_secret=confirmed.encrypted_secret,
            code="",
            backup_codes=confirmed.backup_codes_storage,
        )
        assert ok is False


class TestChallenge:
    def test_round_trip(self):
        token = mfa_service.issue_challenge("user-id-123")
        assert mfa_service.verify_challenge(token) == "user-id-123"

    def test_tampered_signature_rejected(self):
        token = mfa_service.issue_challenge("user-id-123")
        body, sig = token.split(".", 1)
        tampered = f"{body}.{'A' * len(sig)}"
        with pytest.raises(mfa_service.MFAServiceError):
            mfa_service.verify_challenge(tampered)

    def test_expired_token_rejected(self, monkeypatch):
        monkeypatch.setattr(mfa_service.settings, "mfa_challenge_ttl_seconds", -1)
        token = mfa_service.issue_challenge("user-id-123")
        with pytest.raises(mfa_service.MFAServiceError):
            mfa_service.verify_challenge(token)

    def test_malformed_token_rejected(self):
        with pytest.raises(mfa_service.MFAServiceError):
            mfa_service.verify_challenge("not-a-valid-token")
