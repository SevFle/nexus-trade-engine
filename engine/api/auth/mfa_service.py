"""MFA service: enroll / verify / disable / backup codes (gh#126).

Wraps the pure-stdlib TOTP primitives in :mod:`engine.api.auth.mfa`
with encryption-at-rest for the per-user secret, hashed backup codes,
and a short-lived signed challenge token issued after the password
step succeeds so the client can complete login with a TOTP.

The TOTP secret is encrypted with Fernet (AES-128-CBC + HMAC-SHA-256)
under ``settings.mfa_encryption_key`` so a database leak alone does
not yield an attacker the OTP-stream. Backup codes are stored only as
bcrypt hashes; the plaintext is shown to the user once at enrollment
and again on regeneration, never persisted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from engine.api.auth.mfa import (
    MFAError,
    generate_totp_secret,
    totp_uri,
    verify_totp,
)
from engine.config import settings


_BACKUP_CODE_BYTES = 5  # 10 hex chars per code


class MFAServiceError(Exception):
    """Surface to API layer; never leak crypto-level details."""


@dataclass(frozen=True)
class EnrollmentArtifact:
    secret_b32: str
    otpauth_uri: str


@dataclass(frozen=True)
class ConfirmedEnrollment:
    encrypted_secret: str
    backup_codes_plaintext: list[str]
    backup_codes_storage: dict


def _fernet() -> Fernet:
    key = settings.mfa_encryption_key.encode("ascii") if settings.mfa_encryption_key else b""
    if not key:
        raise MFAServiceError("MFA encryption key is not configured")
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise MFAServiceError("MFA encryption key is invalid") from exc


def encrypt_secret(secret_b32: str) -> str:
    return _fernet().encrypt(secret_b32.encode("ascii")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise MFAServiceError("MFA secret decryption failed") from exc


def generate_backup_codes(count: int | None = None) -> list[str]:
    n = count if count is not None else settings.mfa_backup_codes_count
    if n <= 0:
        raise MFAServiceError(f"backup_codes_count must be > 0; got {n}")
    return [secrets.token_hex(_BACKUP_CODE_BYTES) for _ in range(n)]


def hash_backup_codes(codes: list[str]) -> dict:
    return {
        "version": 1,
        "codes": [
            {
                "hash": bcrypt.hashpw(c.encode("ascii"), bcrypt.gensalt()).decode(
                    "ascii"
                ),
                "used_at": None,
            }
            for c in codes
        ],
    }


def verify_backup_code(storage: dict | None, code: str) -> tuple[bool, dict | None]:
    """Constant-time check across stored hashes; on match, return
    updated storage with the matched entry marked used. Single-use.

    Returns ``(matched, new_storage)``. ``new_storage`` is None when no
    match (so the caller can avoid an UPDATE on miss).
    """
    if not storage or "codes" not in storage:
        return (False, None)
    entries = list(storage.get("codes", []))
    matched_idx: int | None = None
    candidate = code.encode("ascii")
    for i, entry in enumerate(entries):
        if entry.get("used_at") is not None:
            continue
        try:
            if bcrypt.checkpw(candidate, entry["hash"].encode("ascii")):
                matched_idx = i
                break
        except (ValueError, KeyError):
            continue
    if matched_idx is None:
        return (False, None)
    entries[matched_idx] = {
        **entries[matched_idx],
        "used_at": int(time.time()),
    }
    return (True, {"version": storage.get("version", 1), "codes": entries})


def begin_enrollment(*, account: str, issuer: str = "Nexus Trade Engine") -> EnrollmentArtifact:
    """Generate a fresh TOTP secret + otpauth URI for QR rendering.

    The secret is *not* persisted here; the caller stores the
    encrypted form only after the user confirms enrollment with a
    valid TOTP code via :func:`confirm_enrollment`.
    """
    if not settings.mfa_encryption_key:
        raise MFAServiceError("MFA is not configured on this deployment")
    secret = generate_totp_secret()
    uri = totp_uri(secret=secret, account=account, issuer=issuer)
    return EnrollmentArtifact(secret_b32=secret, otpauth_uri=uri)


def confirm_enrollment(*, secret_b32: str, code: str) -> ConfirmedEnrollment:
    """Validate the user can produce a current TOTP for the proposed
    secret, then return ciphertext + freshly generated backup codes."""
    try:
        ok = verify_totp(secret_b32, code)
    except MFAError as exc:
        raise MFAServiceError(f"invalid TOTP input: {exc}") from exc
    if not ok:
        raise MFAServiceError("TOTP code does not match secret")
    plaintext_codes = generate_backup_codes()
    storage = hash_backup_codes(plaintext_codes)
    encrypted = encrypt_secret(secret_b32)
    return ConfirmedEnrollment(
        encrypted_secret=encrypted,
        backup_codes_plaintext=plaintext_codes,
        backup_codes_storage=storage,
    )


def verify_login_code(
    *, encrypted_secret: str, code: str, backup_codes: dict | None
) -> tuple[bool, dict | None]:
    """Verify a TOTP code OR a single-use backup code.

    Returns ``(ok, new_backup_codes)``. ``new_backup_codes`` is non-None
    only when a backup code was consumed and the caller must persist
    the updated storage."""
    if not code:
        return (False, None)
    cleaned = code.strip().replace(" ", "").replace("-", "")
    secret_b32 = decrypt_secret(encrypted_secret)
    if cleaned.isdigit() and len(cleaned) == 6:
        try:
            if verify_totp(secret_b32, cleaned):
                return (True, None)
        except MFAError:
            return (False, None)
        return (False, None)
    return verify_backup_code(backup_codes, cleaned)


# --- Challenge tokens --------------------------------------------------
_CHALLENGE_VERSION = "v1"


def _challenge_key() -> bytes:
    if not settings.secret_key:
        raise MFAServiceError("secret_key is not configured")
    return hashlib.sha256(settings.secret_key.encode("utf-8")).digest()


def issue_challenge(user_id: str) -> str:
    payload = {
        "sub": str(user_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + settings.mfa_challenge_ttl_seconds,
        "v": _CHALLENGE_VERSION,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
    sig = hmac.new(_challenge_key(), body, hashlib.sha256).digest()
    return f"{body.decode('ascii')}.{base64.urlsafe_b64encode(sig).decode('ascii')}"


def verify_challenge(token: str) -> str:
    try:
        body_b64, sig_b64 = token.split(".", 1)
    except ValueError as exc:
        raise MFAServiceError("malformed challenge token") from exc
    expected_sig = hmac.new(
        _challenge_key(), body_b64.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided_sig = base64.urlsafe_b64decode(sig_b64)
    except (ValueError, TypeError) as exc:
        raise MFAServiceError("malformed challenge token") from exc
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise MFAServiceError("invalid challenge signature")
    try:
        payload = json.loads(base64.urlsafe_b64decode(body_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise MFAServiceError("malformed challenge payload") from exc
    if payload.get("v") != _CHALLENGE_VERSION:
        raise MFAServiceError("unsupported challenge version")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise MFAServiceError("challenge token expired")
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise MFAServiceError("missing subject in challenge")
    return sub


__all__ = [
    "ConfirmedEnrollment",
    "EnrollmentArtifact",
    "MFAServiceError",
    "begin_enrollment",
    "confirm_enrollment",
    "decrypt_secret",
    "encrypt_secret",
    "generate_backup_codes",
    "hash_backup_codes",
    "issue_challenge",
    "verify_backup_code",
    "verify_challenge",
    "verify_login_code",
]
