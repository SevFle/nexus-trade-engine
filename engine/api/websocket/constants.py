"""Constants for the WebSocket API (SEV-275).

Holds the wire-protocol version, close codes, channel prefixes, and
default tunables in one place so handlers, the connection manager,
the bridge, and tests agree on the same numbers.
"""

from __future__ import annotations

# Wire-protocol schema version. Bumped on any breaking change to the
# envelope or any of the event payload contracts. Clients MUST echo this
# back in their ``subscribe`` frame so future servers can negotiate
# backwards-compatible behaviour per connection.
WS_PROTOCOL_VERSION: str = "1.0"

# ---------------------------------------------------------------------------
# WebSocket close codes (application-level; 4xxx range is application-defined).
# RFC 6455 reserves 1000-2999 for the protocol and 3000-3999 for libraries /
# frameworks; 4000-4999 is open for application use.
# ---------------------------------------------------------------------------
class CloseCode:
    """Application-defined WebSocket close codes.

    ``4xxx`` codes are application-defined per RFC 6455. Chosen so logs
    can distinguish failure mode at a glance.
    """

    # Authentication & authorization
    AUTH_TIMEOUT = 4401
    AUTH_FAILED = 4001
    FORBIDDEN = 4003

    # Protocol / schema
    MALFORMED = 4400
    PROTOCOL_ERROR = 4402

    # Backpressure / abuse
    RATE_LIMITED = 4008
    POLICY_VIOLATION = 1008  # IANA-assigned "slow consumer" disconnect

    # Lifecycle
    GOING_AWAY = 1001  # graceful, server-initiated shutdown
    INTERNAL_ERROR = 1011  # IANA-assigned "server got an unexpected condition"


# ---------------------------------------------------------------------------
# Redis / EventBus channel prefixes.
# ---------------------------------------------------------------------------
CHANNEL_PORTFOLIO = "portfolio"      # per-user: portfolio:{user_id}
CHANNEL_ORDERS = "orders"            # per-user: orders:{user_id}
CHANNEL_MARKET = "market"            # per-symbol: market:{symbol}
CHANNEL_MARKET_DEPTH = "market_depth"  # per-symbol: market_depth:{symbol}

# Per-connection caps to prevent abuse.
MAX_SYMBOL_SUBS_PER_CONNECTION = 500
MAX_USER_CHANNELS_PER_CONNECTION = 16  # portfolio/orders/etc. — one per family


# ---------------------------------------------------------------------------
# Heartbeat / lifecycle tunables.
# ---------------------------------------------------------------------------
HEARTBEAT_INTERVAL_SECONDS = 20.0
HEARTBEAT_MISS_LIMIT = 2              # disconnect after N missed pongs
AUTH_TIMEOUT_SECONDS = 10.0           # mirror of engine.api.routes.websocket
DRAIN_TIMEOUT_SECONDS = 5.0           # graceful shutdown drain window

# Per-connection outbound queue. Bounded so a slow consumer cannot
# pin an event loop indefinitely; overflow triggers a slow-consumer
# disconnect with close code 1008.
OUTBOUND_QUEUE_CAPACITY = 1024

# After the queue is full, allow this many additional "warning" frames
# to land before tearing the connection down. Lets the client receive
# the explicit ``slow_consumer`` error frame.
SLOW_CONSUMER_GRACE_FRAMES = 3


# ---------------------------------------------------------------------------
# Rate limiting defaults for outbound frames.
# ---------------------------------------------------------------------------
DEFAULT_OUTBOUND_PER_SECOND = 100
DEFAULT_OUTBOUND_BURST = 200


__all__ = [
    "AUTH_TIMEOUT_SECONDS",
    "CHANNEL_MARKET",
    "CHANNEL_MARKET_DEPTH",
    "CHANNEL_ORDERS",
    "CHANNEL_PORTFOLIO",
    "DEFAULT_OUTBOUND_BURST",
    "DEFAULT_OUTBOUND_PER_SECOND",
    "DRAIN_TIMEOUT_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_MISS_LIMIT",
    "MAX_SYMBOL_SUBS_PER_CONNECTION",
    "MAX_USER_CHANNELS_PER_CONNECTION",
    "OUTBOUND_QUEUE_CAPACITY",
    "SLOW_CONSUMER_GRACE_FRAMES",
    "WS_PROTOCOL_VERSION",
    "CloseCode",
]
