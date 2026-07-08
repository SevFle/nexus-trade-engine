"""Wire protocol and Pydantic message schemas for WebSocket API (SEV-275).

Inbound messages (client -> server):
  - AuthMessage: authenticate with JWT token
  - SubscribeMessage: subscribe to a channel with params
  - UnsubscribeMessage: unsubscribe from a channel
  - PingMessage: keepalive ping

Outbound messages (server -> client):
  - AckMessage: acknowledgement for subscribe/unsubscribe
  - ErrorMessage: error notification
  - EventMessage: event payload delivery
  - PongMessage: keepalive pong
  - CloseMessage: server-initiated close notification

Channel taxonomy:
  - 'portfolio': sub-keyed by account_id / strategy_id
  - 'orders': sub-keyed by symbol / status
  - 'strategies': sub-keyed by strategy_id

Room naming convention: '<channel>:<scope>'
  e.g. 'portfolio:account:42', 'user:123'
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AuthMessage(BaseModel):
    type: Literal["auth"] = "auth"
    token: str = Field(min_length=1)
    ref: str | None = None


class SubscribeMessage(BaseModel):
    type: Literal["subscribe"] = "subscribe"
    channel: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    ref: str | None = None


class UnsubscribeMessage(BaseModel):
    type: Literal["unsubscribe"] = "unsubscribe"
    channel: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    ref: str | None = None


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"
    ref: str | None = None


InboundMessage = AuthMessage | SubscribeMessage | UnsubscribeMessage | PingMessage


class AckMessage(BaseModel):
    type: Literal["ack"] = "ack"
    ref: str | None = None
    status: Literal["ok", "error"] = "ok"
    error_code: str | None = None
    message: str | None = None


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    ref: str | None = None


class EventMessage(BaseModel):
    type: Literal["event"] = "event"
    channel: str
    room: str
    payload: dict[str, Any] = Field(default_factory=dict)
    seq: int = Field(default=0, ge=0)
    ts: str | None = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class PongMessage(BaseModel):
    type: Literal["pong"] = "pong"
    ref: str | None = None


class CloseMessage(BaseModel):
    type: Literal["close"] = "close"
    code: int
    reason: str


OutboundMessage = AckMessage | ErrorMessage | EventMessage | PongMessage | CloseMessage

VALID_CHANNELS: frozenset[str] = frozenset({"portfolio", "orders", "strategies"})

WS_CLOSE_NORMAL = 1000
WS_CLOSE_POLICY = 1008
WS_CLOSE_SERVER_ERROR = 1011
WS_CLOSE_AUTH_INVALID = 4401
WS_CLOSE_AUTH_TIMEOUT = 4402
WS_CLOSE_TOKEN_EXPIRED = 4403
# Mirrors HTTP 403 — token decoded but lacks required scope.
WS_CLOSE_AUTH_FORBIDDEN = 4404
# Mirrors HTTP 451 — pending legal re-acceptance blocks the session.
WS_CLOSE_LEGAL_REACCEPT = 4451


def parse_inbound(raw) -> tuple:
    """Parse a raw dict into an InboundMessage.

    Returns (message, error_description). If parsing succeeds, error is None.
    If parsing fails, message is None and error describes the issue.
    """
    if not isinstance(raw, dict):
        return None, "missing or invalid 'type' field"
    msg_type = raw.get("type")
    if not isinstance(msg_type, str):
        return None, "missing or invalid 'type' field"
    parsers = {
        "auth": AuthMessage,
        "subscribe": SubscribeMessage,
        "unsubscribe": UnsubscribeMessage,
        "ping": PingMessage,
    }
    parser = parsers.get(msg_type)
    if parser is None:
        return None, f"unknown message type: {msg_type}"
    try:
        msg = parser.model_validate(raw)
    except Exception as exc:
        return None, f"validation error: {exc}"
    else:
        return msg, None


def parse_room_name(room: str) -> tuple[str, str]:
    """Parse a room name into (channel, scope).

    Room format: '<channel>:<scope>' or '<channel>:<key>:<value>'
    Returns (channel, full_scope_string).
    """
    _expected_parts = 2
    parts = room.split(":", _expected_parts)
    if len(parts) < _expected_parts:
        return parts[0] if parts else "", ""
    return parts[0], parts[1] if len(parts) == _expected_parts else f"{parts[1]}:{parts[2]}"
