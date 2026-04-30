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
        # explicit secrets
        "password",
        "passwd",
        "pw",
        "pass",
        "token",
        "auth_token",
        "access_token",
        "refresh_token",
        "session_token",
        "id_token",
        "secret",
        "client_secret",
        "private_key",
        "ssh_key",
        "api_key",
        "apikey",
        "x_api_key",
        "x_auth_token",
        # auth payloads
        "authorization",
        "auth",
        "bearer",
        "cookie",
        "set_cookie",
        # PII / cards
        "credit_card",
        "creditcard",
        "card_number",
        "cardnumber",
        "cvv",
        "ssn",
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


_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer / authorization tokens
    re.compile(r"(?i)\bBearer\s+[\w\-._~+/=]+"),
    # JWT-shaped: 3 dot-separated base64url segments, each ≥16 chars to
    # avoid matching dotted module paths or version strings
    re.compile(r"\b[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"),
    # 13-19 digit blocks with optional separators (PAN-shaped). Anchored
    # to non-word boundaries so it survives most word-boundary edge cases.
    re.compile(r"\b(?:\d[ \-]?){13,19}\b"),
    # Prefixed secrets (Slack, Stripe, GitHub, AWS access keys)
    re.compile(r"\b(?:sk|xoxb|xoxp|ghp|ghs|AKIA)[A-Za-z0-9_\-]{16,}\b"),
    # PEM blocks (multi-line). Use [\s\S] so it crosses newlines without
    # needing re.DOTALL on the engine.
    re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]+?-----END [A-Z0-9 ]+-----"),
)


def _scrub_string(s: str) -> str:
    out = s
    for pat in _VALUE_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


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


def redact_processor(
    _logger: WrappedLogger, _name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor entry point. Returns a new dict; input is not mutated."""
    return _scrub_dict(dict(event_dict))


__all__ = ["REDACTED", "redact_processor"]
