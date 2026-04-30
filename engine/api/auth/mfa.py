"""Multi-factor authentication primitives.

Pure-stdlib RFC 6238 (TOTP) / RFC 4226 (HOTP) implementation. No
external pyotp dependency.

PR1 ships TOTP enrollment + verification. WebAuthn / FIDO2 lands in a
follow-up that pulls in a webauthn library at the API layer.

Threat model:
- Constant-time comparison on the verification path so timing leaks
  cannot reveal partial code matches.
- Codes outside [0, 10^digits) are rejected before any HMAC work.
- ``window`` permits clock-drift tolerance symmetrically (±N steps);
  caller must persist the last-accepted counter to defeat replay.

Caller responsibilities:
- Persist the base32 secret encrypted at rest.
- Track last-accepted counter alongside the secret to reject replays.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode


class MFAError(Exception):
    """Raised on malformed inputs to MFA primitives."""


_DEFAULT_DIGITS = 6
_DEFAULT_STEP_SEC = 30
_MIN_SECRET_BYTES = 16
_MIN_DIGITS = 6
_MAX_DIGITS = 10


def generate_totp_secret(*, n_bytes: int = 20) -> str:
    """Return a fresh base32-encoded TOTP secret (>=128 bits by default)."""
    if n_bytes < _MIN_SECRET_BYTES:
        msg = f"n_bytes must be >= {_MIN_SECRET_BYTES}; got {n_bytes}"
        raise MFAError(msg)
    raw = secrets.token_bytes(n_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    padding = (-len(secret_b32)) % 8
    try:
        return base64.b32decode(
            secret_b32.upper() + "=" * padding, casefold=False
        )
    except (ValueError, TypeError) as exc:
        msg = f"secret is not valid base32: {exc}"
        raise MFAError(msg) from exc


def _hotp(
    secret_b32: str, counter: int, *, digits: int = _DEFAULT_DIGITS
) -> str:
    """RFC 4226 HMAC-One-Time-Password."""
    if digits < _MIN_DIGITS or digits > _MAX_DIGITS:
        msg = f"digits must be in [{_MIN_DIGITS}, {_MAX_DIGITS}]; got {digits}"
        raise MFAError(msg)
    if counter < 0:
        msg = f"counter must be non-negative; got {counter}"
        raise MFAError(msg)
    key = _decode_secret(secret_b32)
    counter_bytes = struct.pack(">Q", counter)
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    code = truncated % (10**digits)
    return f"{code:0{digits}d}"


def verify_totp(
    secret_b32: str,
    code: str,
    *,
    now: float | None = None,
    step: int = _DEFAULT_STEP_SEC,
    window: int = 1,
    digits: int = _DEFAULT_DIGITS,
) -> bool:
    """Verify a TOTP ``code`` against ``secret_b32``.

    ``window`` permits +/- N step accept-windows for clock drift.
    ``now`` defaults to the current Unix epoch (seconds).
    """
    if step <= 0:
        msg = f"step must be > 0; got {step}"
        raise MFAError(msg)
    if window < 0:
        msg = f"window must be >= 0; got {window}"
        raise MFAError(msg)
    if not code or not code.isdigit() or len(code) != digits:
        return False
    _decode_secret(secret_b32)  # surface bad-secret errors before time math
    t = time.time() if now is None else now
    counter = int(t // step)
    for offset in range(-window, window + 1):
        candidate = _hotp(secret_b32, counter + offset, digits=digits)
        if hmac.compare_digest(candidate, code):
            return True
    return False


def totp_uri(
    *,
    secret: str,
    account: str,
    issuer: str,
    digits: int = _DEFAULT_DIGITS,
    step: int = _DEFAULT_STEP_SEC,
    algorithm: str = "SHA1",
) -> str:
    """Build an ``otpauth://`` URI for QR-code enrollment."""
    if not account.strip():
        msg = "account must be non-empty"
        raise MFAError(msg)
    if not issuer.strip():
        msg = "issuer must be non-empty"
        raise MFAError(msg)
    label = f"{quote(issuer, safe='')}:{quote(account, safe='@')}"
    params = {
        "secret": secret,
        "issuer": issuer,
        "algorithm": algorithm,
        "digits": str(digits),
        "period": str(step),
    }
    return f"otpauth://totp/{label}?{urlencode(params)}"


__all__ = [
    "MFAError",
    "generate_totp_secret",
    "totp_uri",
    "verify_totp",
]
