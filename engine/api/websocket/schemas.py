"""Pydantic schemas for the WebSocket wire protocol (SEV-275).

Every event delivered to a client is wrapped in a :class:`WSMessage`
envelope. The envelope carries routing metadata that the client uses
to demux events back to the right handler:

- ``event``     — the canonical event name (e.g. ``order.filled``).
- ``channel``   — which channel this came through.
- ``ts``        — ISO-8601 UTC timestamp the server emitted the envelope.
- ``seq``       — monotonically increasing per-connection counter; gaps
                  signal a lost event and trigger a client-side refresh.
- ``correlation_id`` — propagates a client-supplied id through the
                  request/event cycle for tracing and de-duplication.
- ``version``   — wire-protocol version (see :data:`WS_VERSION`).
- ``data``      — domain-specific payload (free-form dict).

Client → server frames are validated against :class:`ClientFrame`'s
discriminated union so the route handler gets a typed object back
rather than a ``dict`` it has to re-pick apart.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.api.websocket.constants import WS_VERSION

# ---------------------------------------------------------------------------
# Server → client envelope
# ---------------------------------------------------------------------------


class WSMessage(BaseModel):
    """Outbound event envelope.

    All events delivered to a WebSocket client are wrapped in this
    schema. The schema is intentionally permissive (``extra="allow"``)
    so future additive fields don't break older clients.
    """

    model_config = ConfigDict(extra="allow")

    event: str = Field(
        ..., description="Canonical event name, e.g. 'order.filled'."
    )
    channel: str = Field(
        ..., description="Channel the event was delivered through."
    )
    ts: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Server-side timestamp (ISO-8601 UTC).",
    )
    seq: int = Field(
        ...,
        ge=0,
        description="Per-connection monotonic sequence number; gaps signal a lost event.",
    )
    correlation_id: str | None = Field(
        default=None,
        description="Client-supplied correlation id, propagated through the engine.",
    )
    version: str = Field(
        default=WS_VERSION,
        description="Wire-protocol version (semver-ish).",
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Domain-specific event payload.",
    )


# ---------------------------------------------------------------------------
# Client → server frames
# ---------------------------------------------------------------------------


class AuthFrame(BaseModel):
    """First-frame authentication.

    Sent immediately after the WebSocket handshake when the client did
    not provide a token via query string or Sec-WebSocket-Protocol.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["auth"]
    token: str = Field(..., min_length=1)


class SubscribeFrame(BaseModel):
    """Subscribe to one or more channels."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["subscribe"]
    channels: list[str] = Field(default_factory=list)
    correlation_id: str | None = None


class UnsubscribeFrame(BaseModel):
    """Stop receiving events on the listed channels."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["unsubscribe"]
    channels: list[str] = Field(default_factory=list)
    correlation_id: str | None = None


class PingFrame(BaseModel):
    """Client-initiated heartbeat.

    The server replies with a :class:`PongFrame` echoing the optional
    correlation id back so the client can round-trip a latency probe.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["ping"]
    correlation_id: str | None = None


class AckFrame(BaseModel):
    """Client acknowledges a previous event (optional flow-control).

    Currently logged but not enforced; future work may use this to
    bound the server-side replay window.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["ack"]
    seq: int = Field(..., ge=0)
    correlation_id: str | None = None


ClientFrame = AuthFrame | SubscribeFrame | UnsubscribeFrame | PingFrame | AckFrame
"""Discriminated union over all valid client→server frames."""


def parse_client_frame(raw: Any) -> ClientFrame | None:
    """Best-effort parse of a client frame.

    Returns ``None`` if ``raw`` is not a dict or doesn't carry a valid
    ``type`` discriminator. Raises :class:`pydantic.ValidationError` if
    the shape matches a known ``type`` but a field is malformed; the
    route handler turns that into a ``400``-style error frame back to
    the client so a typo doesn't silently drop the message.
    """
    if not isinstance(raw, dict):
        return None
    ftype = raw.get("type")
    if not isinstance(ftype, str):
        return None

    cls_map: dict[str, type[BaseModel]] = {
        "auth": AuthFrame,
        "subscribe": SubscribeFrame,
        "unsubscribe": UnsubscribeFrame,
        "ping": PingFrame,
        "ack": AckFrame,
    }
    cls = cls_map.get(ftype)
    if cls is None:
        return None
    return cls.model_validate(raw)  # may raise ValidationError


# ---------------------------------------------------------------------------
# Server → client control frames (not events)
# ---------------------------------------------------------------------------


class ServerFrame(BaseModel):
    """Base class for server-initiated control frames.

    Control frames are not envelope-wrapped — they have a fixed
    ``type`` and don't carry a per-connection ``seq``.
    """

    model_config = ConfigDict(extra="forbid")


class AuthOkFrame(ServerFrame):
    type: Literal["auth.ok"] = "auth.ok"
    user_id: str


class SubscribedFrame(ServerFrame):
    type: Literal["subscribed"] = "subscribed"
    channels: list[str]
    correlation_id: str | None = None


class UnsubscribedFrame(ServerFrame):
    type: Literal["unsubscribed"] = "unsubscribed"
    channels: list[str]
    correlation_id: str | None = None


class PongFrame(ServerFrame):
    type: Literal["pong"] = "pong"
    correlation_id: str | None = None


class ErrorFrame(ServerFrame):
    type: Literal["error"] = "error"
    code: str
    detail: str
    correlation_id: str | None = None


class ConnectionReadyFrame(ServerFrame):
    """Sent immediately after auth succeeds, before any other frames."""

    type: Literal["connection.ready"] = "connection.ready"
    user_id: str
    heartbeat_seconds: float


def new_correlation_id() -> str:
    """Generate a new correlation id (UUID4, hex form)."""
    return uuid.uuid4().hex


__all__ = [
    "AckFrame",
    "AuthFrame",
    "AuthOkFrame",
    "ClientFrame",
    "ConnectionReadyFrame",
    "ErrorFrame",
    "PongFrame",
    "ServerFrame",
    "SubscribeFrame",
    "SubscribedFrame",
    "UnsubscribeFrame",
    "UnsubscribedFrame",
    "ValidationError",
    "WSMessage",
    "new_correlation_id",
    "parse_client_frame",
]
