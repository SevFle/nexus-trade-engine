"""Pydantic v2 schemas for the WebSocket wire protocol (SEV-275).

Defines a strongly-typed envelope plus a discriminated union over the
client-bound event families (portfolio / order / market_data) and the
client-to-server control frames (subscribe / unsubscribe / ping /
auth). The envelope carries a semver ``v`` field so future versions
can negotiate behaviour without breaking older clients.

Round-trip safety
-----------------
Every public model is expected to satisfy
``Model.model_validate(obj.model_dump()) == obj``. Unit tests pin that
contract for every variant of the discriminated union.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, Tag

from engine.api.websocket.constants import WS_PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Shared base — the envelope that wraps every frame on the wire.
# ---------------------------------------------------------------------------
class _BaseFrame(BaseModel):
    """Every frame carries the envelope fields: ``v`` (wire-protocol
    version, semver) and an optional ``correlation_id`` for end-to-end
    tracing. Concrete subclasses add the type-discriminated payload.

    ``extra=forbid`` rejects unknown fields so wire-level typos surface
    at the validation boundary rather than being silently dropped.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    v: str = Field(default=WS_PROTOCOL_VERSION, description="Wire-protocol version")
    correlation_id: str | None = Field(default=None, description="Optional tracing id")


# Kept as an alias for callers that want a generic envelope shape.
Envelope = _BaseFrame


# ---------------------------------------------------------------------------
# Client → server control frames
# ---------------------------------------------------------------------------
class AuthFrame(_BaseFrame):
    type: Literal["auth"] = "auth"
    token: str = Field(..., min_length=1, description="JWT or nxs_* API key")


class SubscribeFrame(_BaseFrame):
    """Subscribe to one or more channels.

    For user-scoped channels (``portfolio``, ``orders``) the user is
    inferred from the authenticated principal. For symbol-scoped
    channels (``market``) ``symbols`` carries the tickers.
    """

    type: Literal["subscribe"] = "subscribe"
    channel: Literal["portfolio", "orders", "market", "market_depth"]
    symbols: list[str] = Field(default_factory=list)


class UnsubscribeFrame(_BaseFrame):
    type: Literal["unsubscribe"] = "unsubscribe"
    channel: Literal["portfolio", "orders", "market", "market_depth"]
    symbols: list[str] = Field(default_factory=list)


class PingFrame(_BaseFrame):
    type: Literal["ping"] = "ping"
    ts: datetime | None = None


# ---------------------------------------------------------------------------
# Server → client control frames
# ---------------------------------------------------------------------------
class AuthOkFrame(_BaseFrame):
    type: Literal["auth.ok"] = "auth.ok"
    user_id: str
    scopes: list[str] = Field(default_factory=list)


class AuthFailedFrame(_BaseFrame):
    type: Literal["auth.failed"] = "auth.failed"
    reason: Literal[
        "timeout", "missing_token", "invalid_token", "inactive_user", "forbidden"
    ]


class SubscribedFrame(_BaseFrame):
    type: Literal["subscribed"] = "subscribed"
    channel: Literal["portfolio", "orders", "market", "market_depth"]
    symbols: list[str] = Field(default_factory=list)


class UnsubscribedFrame(_BaseFrame):
    type: Literal["unsubscribed"] = "unsubscribed"
    channel: Literal["portfolio", "orders", "market", "market_depth"]
    symbols: list[str] = Field(default_factory=list)


class PongFrame(_BaseFrame):
    type: Literal["pong"] = "pong"
    server_ts: datetime
    client_ts: datetime | None = None


class ServerShutdownFrame(_BaseFrame):
    type: Literal["server_shutdown"] = "server_shutdown"
    reason: str = "shutdown"
    drain_seconds: float = 5.0


class ErrorFrame(_BaseFrame):
    """Generic error frame.

    Used for unknown message types, malformed payloads, server errors,
    etc. Close codes for hard failures are sent via the WebSocket close
    handshake; this frame is the human-readable explanation that lands
    *before* the close so well-behaved clients can surface it.
    """

    type: Literal["error"] = "error"
    code: Literal[
        "malformed",
        "unknown_message_type",
        "rate_limited",
        "server_error",
        "too_many_subscriptions",
        "slow_consumer_warning",
    ]
    detail: str = ""
    recoverable: bool = True


class SlowConsumerWarningFrame(_BaseFrame):
    """Sent before a slow-consumer disconnect so the client can react."""

    type: Literal["slow_consumer_warning"] = "slow_consumer_warning"
    queue_depth: int
    capacity: int


# ---------------------------------------------------------------------------
# Server → client event payloads (the three families)
# ---------------------------------------------------------------------------
class PortfolioUpdatedEvent(_BaseFrame):
    type: Literal["portfolio.updated"] = "portfolio.updated"
    user_id: str
    portfolio_id: str
    timestamp: datetime
    nav: Decimal | None = None
    cash: Decimal | None = None
    positions: dict[str, Any] = Field(default_factory=dict)
    source: str = "engine"


class OrderEvent(_BaseFrame):
    type: Literal["order.created", "order.filled", "order.rejected", "order.failed"]
    user_id: str
    order_id: str
    symbol: str
    timestamp: datetime
    qty: Decimal | None = None
    price: Decimal | None = None
    status: str
    source: str = "engine"


class MarketTickEvent(_BaseFrame):
    type: Literal["market.tick"] = "market.tick"
    symbol: str = Field(..., min_length=1, max_length=32)
    timestamp: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    volume: int | None = None
    source: str = "engine"


class MarketDepthEvent(_BaseFrame):
    type: Literal["market.depth"] = "market.depth"
    symbol: str = Field(..., min_length=1, max_length=32)
    timestamp: datetime
    bids: list[list[Decimal]] = Field(default_factory=list)  # [[price, qty], ...]
    asks: list[list[Decimal]] = Field(default_factory=list)
    source: str = "engine"


# ---------------------------------------------------------------------------
# Discriminated unions
# ---------------------------------------------------------------------------
ClientFrame = Annotated[
    Annotated[AuthFrame, Tag("auth")] | Annotated[SubscribeFrame, Tag("subscribe")] | Annotated[UnsubscribeFrame, Tag("unsubscribe")] | Annotated[PingFrame, Tag("ping")],
    Field(discriminator="type"),
]


ServerControlFrame = Annotated[
    Annotated[AuthOkFrame, Tag("auth.ok")] | Annotated[AuthFailedFrame, Tag("auth.failed")] | Annotated[SubscribedFrame, Tag("subscribed")] | Annotated[UnsubscribedFrame, Tag("unsubscribed")] | Annotated[PongFrame, Tag("pong")] | Annotated[ServerShutdownFrame, Tag("server_shutdown")] | Annotated[ErrorFrame, Tag("error")] | Annotated[SlowConsumerWarningFrame, Tag("slow_consumer_warning")],
    Field(discriminator="type"),
]


ServerEvent = Annotated[
    Union[
        Annotated[PortfolioUpdatedEvent, Tag("portfolio.updated")],
        Annotated[OrderEvent, Tag("order")],  # discriminator sees order.* variants
        Annotated[MarketTickEvent, Tag("market.tick")],
        Annotated[MarketDepthEvent, Tag("market.depth")],
    ],
    Field(discriminator="type"),
]


__all__ = [
    "AuthFailedFrame",
    "AuthFrame",
    "AuthOkFrame",
    "ClientFrame",
    "Envelope",
    "ErrorFrame",
    "MarketDepthEvent",
    "MarketTickEvent",
    "OrderEvent",
    "PingFrame",
    "PongFrame",
    "PortfolioUpdatedEvent",
    "ServerControlFrame",
    "ServerEvent",
    "ServerShutdownFrame",
    "SlowConsumerWarningFrame",
    "SubscribeFrame",
    "SubscribedFrame",
    "UnsubscribeFrame",
    "UnsubscribedFrame",
]
