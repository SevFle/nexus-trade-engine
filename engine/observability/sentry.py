from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import sentry_sdk

from engine.config import settings

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint

REDACTED = "***REDACTED***"

# Keys whose *presence* — matched as a case-insensitive substring — marks
# the associated value as sensitive and therefore redacted in full. The
# substring approach is deliberate so that compound keys such as
# ``user_email``, ``refresh_token`` or ``x-api-key`` are caught without an
# ever-growing allow/deny list.
_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|password|passwd|token|secret|api[_-]?key|apikey|cookie|email)",
    re.IGNORECASE,
)

# Value-level patterns scrubbed out of free-form strings even when the
# surrounding key looks innocuous — e.g. an error message that happened to
# embed a bearer token or a PEM block.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ``Authorization: Bearer <token>``
    re.compile(r"(?i)\bBearer\s+[\w\-._~+/=]+"),
    # JWT-shaped strings: three dot-separated base64url segments, each
    # >= 16 chars so dotted module paths / version strings are untouched.
    re.compile(r"\b[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"),
    # Prefixed provider secrets (Stripe, Slack, GitHub, AWS, …).
    re.compile(r"\b(?:sk|xoxb|xoxp|ghp|ghs|AKIA)[A-Za-z0-9_\-]{16,}\b"),
    # PEM blocks — ``[\s\S]`` lets the match span newlines.
    re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]+?-----END [A-Z0-9 ]+-----"),
)


def _is_sensitive_key(key: Any) -> bool:
    """True when ``key`` contains any sensitive substring."""
    try:
        return bool(_SENSITIVE_KEY_RE.search(str(key)))
    except Exception:  # pragma: no cover - defensive for odd key types
        return False


def _scrub_string(value: str) -> str:
    out = value
    for pattern in _VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def _scrub_value(value: Any) -> Any:  # noqa: PLR0911 - one clear branch per type
    """Recursively scrub sensitive data from any JSON-shaped value."""
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, bytes):
        return _scrub_string(value.decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        return {k: _scrub_entry(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        # Sets are unhashable-after-scrub; fall back to a sorted list so the
        # result stays deterministic and JSON-serialisable.
        return sorted({_scrub_value(item) for item in value}, key=repr)
    return value


def _scrub_entry(key: Any, value: Any) -> Any:
    if _is_sensitive_key(key):
        return REDACTED
    return _scrub_value(value)


def scrub_event(event: Any) -> Any:
    """Walk the entire Sentry event/transaction dict and redact secrets.

    The function is intentionally generic: it recurses through every
    nested mapping, list, tuple and set it encounters, so sensitive data
    is masked regardless of *where* in the event payload it surfaces
    (request headers, breadcrumbs, extra, contexts, tags, …).
    """
    scrubbed = _scrub_value(event)
    return scrubbed if isinstance(scrubbed, dict) else event


def _before_send(event: Event, _hint: Hint) -> Event | None:
    """``before_send`` hook — scrub every error/issue event."""
    return scrub_event(event)


def _before_send_transaction(event: Event, _hint: Hint) -> Event | None:
    """``before_send_transaction`` hook — scrub performance/transaction events."""
    return scrub_event(event)


def setup_sentry() -> None:
    """Initialise the Sentry SDK when a DSN is configured.

    When ``NEXUS_SENTRY_DSN`` is empty (the default in dev/test) this is a
    no-op, allowing the process to start without a Sentry backend.
    """
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
        # Never ship local/stack frame variables — they frequently carry
        # credentials captured into locals at the point of failure.
        include_local_variables=False,
    )


def close_sentry() -> None:
    """Flush the Sentry event queue and close the client.

    Called during application shutdown so that buffered events are delivered
    before the process exits. Safe to call when Sentry was never initialised.
    """
    if not sentry_sdk.is_initialized():
        return

    sentry_sdk.flush(timeout=2)

    client = sentry_sdk.get_client()
    client.close()


__all__ = ["close_sentry", "scrub_event", "setup_sentry"]
