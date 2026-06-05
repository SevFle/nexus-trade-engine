"""Constants for the WebSocket API (SEV-275).

Defines the canonical channel set and the wire protocol version. The
``WS_VERSION`` is bumped only on backwards-incompatible envelope
changes; additive fields are forward-compatible by design (Pydantic
``extra="allow"`` on the envelope schema).

Channels
--------
Clients subscribe by channel name. Each channel maps to a family of
domain events emitted on the EventBus:

- ``portfolio``   — portfolio.updated, position.opened, position.closed
- ``order``       — order.created / submitted / filled / rejected / …
- ``backtest``    — backtest.started / completed
- ``alert``       — risk.warning, alert.triggered
- ``market_data`` — market.data.update (SEV-275)
"""

from __future__ import annotations

from enum import StrEnum

WS_VERSION: str = "1.0"
"""Wire-protocol version stamped onto every outbound envelope."""

DEFAULT_HEARTBEAT_SECONDS: float = 30.0
"""Default server-side ping interval. Kept conservative to play nicely
with reverse-proxy idle timeouts (nginx default 60s)."""

AUTH_TIMEOUT_SECONDS: float = 10.0
"""How long the server waits for an ``auth`` frame when neither a
query-string token nor a Sec-WebSocket-Protocol token was supplied."""

MAX_PENDING_SENDS_PER_CONN: int = 256
"""Soft cap on the per-connection outbound queue. A connection that
falls behind by more than this is forcibly closed (slow-consumer
defense)."""


class Channel(StrEnum):
    """Broadcast channels addressable by clients."""

    PORTFOLIO = "portfolio"
    ORDER = "order"
    BACKTEST = "backtest"
    ALERT = "alert"
    MARKET_DATA = "market_data"


VALID_CHANNELS: frozenset[str] = frozenset(c.value for c in Channel)
"""Set of valid string channel names; ``O(1)`` membership check."""

# Backwards-compat alias — the pre-SEV-275 code called these "topics".
# Kept so existing imports keep working without a churn-rename.
VALID_TOPICS: frozenset[str] = VALID_CHANNELS


# Close codes - application-level, in the 4xxx range per RFC 6455.
# Avoid 1000-2999 (reserved by spec / extensions).
WS_CLOSE_NORMAL: int = 1000
WS_CLOSE_GOING_AWAY: int = 1001
WS_CLOSE_POLICY_VIOLATION: int = 1008
WS_CLOSE_INTERNAL_ERROR: int = 1011

# Application close codes (4000-4999 are unregistered; we use 44xx
# for WebSocket-API specific outcomes per the SEV-275 spec).
WS_CLOSE_AUTH_TIMEOUT: int = 4401
WS_CLOSE_UNAUTHENTICATED: int = 4401
WS_CLOSE_FORBIDDEN: int = 4403
WS_CLOSE_BAD_REQUEST: int = 4400
WS_CLOSE_RATE_LIMITED: int = 4440
WS_CLOSE_SLOW_CONSUMER: int = 4441


__all__ = [
    "AUTH_TIMEOUT_SECONDS",
    "DEFAULT_HEARTBEAT_SECONDS",
    "MAX_PENDING_SENDS_PER_CONN",
    "VALID_CHANNELS",
    "VALID_TOPICS",
    "WS_CLOSE_AUTH_TIMEOUT",
    "WS_CLOSE_BAD_REQUEST",
    "WS_CLOSE_FORBIDDEN",
    "WS_CLOSE_GOING_AWAY",
    "WS_CLOSE_INTERNAL_ERROR",
    "WS_CLOSE_NORMAL",
    "WS_CLOSE_POLICY_VIOLATION",
    "WS_CLOSE_RATE_LIMITED",
    "WS_CLOSE_SLOW_CONSUMER",
    "WS_CLOSE_UNAUTHENTICATED",
    "WS_VERSION",
    "Channel",
]
