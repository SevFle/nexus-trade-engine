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
        "password",
        "passwd",
        "token",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "credit_card",
        "creditcard",
        "card_number",
        "cardnumber",
        "ssn",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "session_token",
        "cookie",
        "set_cookie",
    }
)


def _is_banned(key: str) -> bool:
    return key.lower().replace("-", "_") in _BANNED_KEYS_LOWER


_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bBearer\s+[\w\-._~+/=]+"),
    re.compile(r"\b[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\b(?:\d[ \-]?){13,19}\b"),
    re.compile(r"\b(?:sk|xoxb|xoxp|ghp|ghs|AKIA)[A-Za-z0-9_\-]{16,}\b"),
)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        out = value
        for pat in _VALUE_PATTERNS:
            out = pat.sub(REDACTED, out)
        return out
    if isinstance(value, dict):
        return _scrub_dict(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item) for item in value)
    return value


def _scrub_dict(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(k, str) and _is_banned(k):
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
