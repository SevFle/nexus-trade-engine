"""Redaction processor: scrubs sensitive keys and value patterns from log
records before they leave the process.

Idempotent. Safe to run after every other context-merger in the structlog
chain, but always run *before* the renderer.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger

REDACTED = "***REDACTED***"

_BANNED_KEYS_LOWER = frozenset(
    {
        # explicit secrets / passwords
        "password",
        "passwd",
        "pw",
        "pwd",
        "passphrase",
        "passcode",
        # tokens
        "token",
        "auth_token",
        "access_token",
        "refresh_token",
        "session_token",
        "id_token",
        "jwt",
        "csrf_token",
        "otp",
        "mfa_code",
        "verification_code",
        # secrets / keys
        "secret",
        "client_secret",
        "webhook_secret",
        "signing_secret",
        "private_key",
        "ssh_key",
        "signing_key",
        "encryption_key",
        "mfa_encryption_key",
        "api_key",
        "apikey",
        "x_api_key",
        "x_auth_token",
        "credentials",
        # auth payloads
        "authorization",
        "proxy_authorization",
        "auth",
        "bearer",
        "cookie",
        "set_cookie",
        "session_id",
        # PII / cards / banking
        "credit_card",
        "creditcard",
        "card_number",
        "cardnumber",
        "cvv",
        "ssn",
        "iban",
        "swift_code",
        "routing_number",
        "bank_account",
        "account_number",
    }
)


def _is_banned(key: object) -> bool:
    if isinstance(key, str):
        return key.lower().replace("-", "_") in _BANNED_KEYS_LOWER
    # non-string keys (Enum, int, …) — coerce and test
    try:
        return str(key).lower().replace("-", "_") in _BANNED_KEYS_LOWER
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Value-level patterns
# ---------------------------------------------------------------------------

_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer / authorization tokens
    re.compile(r"(?i)\bBearer\s+[\w\-._~+/=]+"),
    # JWT-shaped tokens that start with ``eyJ`` (the base64url encoding of
    # ``{"`` — every JSON-object JWT header *and* payload begins with it).
    # Catches shorter tokens that the generic 16-char pattern misses.
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    # Generic 3-segment base64url blobs (each >=16 chars) — catches opaque
    # tokens / dotted secrets that don't start with eyJ.
    re.compile(r"\b[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"),
    # Prefixed secrets (Stripe sk-/pk-, Slack, GitHub, AWS access keys)
    re.compile(r"\b(?:sk|xoxb|xoxp|ghp|ghs|pk|AKIA)[A-Za-z0-9_\-]{16,}\b"),
    # PEM blocks (multi-line). Use [\s\S] so it crosses newlines without
    # needing re.DOTALL on the engine.
    re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]+?-----END [A-Z0-9 ]+-----"),
)

# PAN-shaped digit runs (13-19 digits, optional separators). Each candidate
# is validated against the Luhn checksum so that non-card digit sequences
# (phone numbers, reference IDs, …) are preserved.
_PAN_PATTERN = re.compile(r"\b(?:\d[ \-]?){13,19}\b")

# Inline key=value / key:value secret pairs, e.g. ``password=hunter2`` or
# ``api_key: secret``. Only the *value* is replaced; the key and separator
# are preserved. Keys use ``[_-]?`` so that ``api_key``, ``api-key`` and
# ``apikey`` are all matched by a single alternative.
_KV_KEYS = (
    r"password|passwd|pwd|passphrase|passcode|secret|client[_-]?secret|"
    r"webhook[_-]?secret|signing[_-]?secret|api[_-]?key|apikey|token|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"id[_-]?token|jwt|csrf[_-]?token|otp|mfa[_-]?code|"
    r"verification[_-]?code|private[_-]?key|ssh[_-]?key|signing[_-]?key|"
    r"encryption[_-]?key|mfa[_-]?encryption[_-]?key|x[_-]?api[_-]?key|"
    r"x[_-]?auth[_-]?token|credentials|authorization|proxy[_-]?authorization|auth|bearer|cookie|"
    r"set[_-]?cookie|session[_-]?id|credit[_-]?card|card[_-]?number|cvv|ssn|"
    r"iban|swift[_-]?code|routing[_-]?number|bank[_-]?account|"
    r"account[_-]?number"
)
_KV_PATTERN = re.compile(rf"(?i)\b({_KV_KEYS})(\s*[=:]\s*)(\S+)")


_PAN_MIN_DIGITS = 13
_PAN_MAX_DIGITS = 19
_DIGIT_WRAP = 9


def _luhn_valid(candidate: str) -> bool:
    """Return True if *candidate* passes the Luhn checksum.

    *candidate* is a digit run that may include space/hyphen separators.
    """
    digits = re.sub(r"[^0-9]", "", candidate)
    if len(digits) < _PAN_MIN_DIGITS or len(digits) > _PAN_MAX_DIGITS:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > _DIGIT_WRAP:
                d -= _DIGIT_WRAP
        total += d
    return total % 10 == 0


def _redact_pans(s: str) -> str:
    """Redact Luhn-valid PAN-shaped runs; leave other digit sequences intact."""

    def _maybe(m: re.Match[str]) -> str:
        return REDACTED if _luhn_valid(m.group(0)) else m.group(0)

    return _PAN_PATTERN.sub(_maybe, s)


def _redact_kv(s: str) -> str:
    """Redact values in inline ``key=value`` / ``key: value`` secret pairs."""
    return _KV_PATTERN.sub(rf"\1\2{REDACTED}", s)


def _scrub_string(s: str) -> str:
    out = s
    for pat in _VALUE_PATTERNS:
        out = pat.sub(REDACTED, out)
    out = _redact_pans(out)
    return _redact_kv(out)


def _scrub_value(value: Any) -> Any:  # noqa: PLR0911 - clear branch per type
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8", errors="replace")
        except Exception:
            return REDACTED
        return _scrub_string(decoded)
    if isinstance(value, dict):
        return _scrub_dict(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item) for item in value)
    return value


def _scrub_dict(d: dict[Any, Any]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for k, v in d.items():
        if _is_banned(k):
            out[k] = REDACTED
        else:
            out[k] = _scrub_value(v)
    return out


def scrub_pii(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Public helper: scrub a single event dict in isolation.

    Functionally identical to :func:`redact_processor` (which is the
    structlog-processor wrapper). Provided as a standalone, callable
    entry point for callers that are not inside the structlog pipeline.
    """
    return _scrub_dict(dict(event_dict))


def redact_processor(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """structlog processor entry point. Returns a new dict; input is not mutated."""
    return _scrub_dict(dict(event_dict))


__all__ = ["REDACTED", "redact_processor", "scrub_pii"]
