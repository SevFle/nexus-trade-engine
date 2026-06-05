"""Real-time WebSocket API.

Public surface (SEV-275 and earlier gh#7 helpers):

- :class:`ConnectionManager` — per-user fan-out registry with envelope
  wrapping, monotonic per-connection ``seq`` counters, and a
  ``market_data`` channel in addition to the original four topics.
- :class:`Topic` — string-typed broadcast channels: ``portfolio``,
  ``backtest``, ``order``, ``alert``, ``market_data``.
- :class:`Channel` — same as :class:`Topic`; preferred name going
  forward.
- :class:`WSMessage` — Pydantic envelope schema for outbound events.
- :class:`ClientFrame` and friends — typed inbound control frames.

The route handler lives at :mod:`engine.api.routes.websocket`.
"""

from engine.api.websocket.constants import (
    VALID_CHANNELS,
    VALID_TOPICS,
    WS_VERSION,
    Channel,
)
from engine.api.websocket.manager import (
    ConnectionManager,
    Topic,
    get_manager,
    reset_manager,
)
from engine.api.websocket.schemas import (
    AuthFrame,
    AuthOkFrame,
    ClientFrame,
    ConnectionReadyFrame,
    ErrorFrame,
    PongFrame,
    ServerFrame,
    SubscribedFrame,
    SubscribeFrame,
    UnsubscribedFrame,
    UnsubscribeFrame,
    WSMessage,
    new_correlation_id,
    parse_client_frame,
)

__all__ = [
    "VALID_CHANNELS",
    "VALID_TOPICS",
    "WS_VERSION",
    "AuthFrame",
    "AuthOkFrame",
    "Channel",
    "ClientFrame",
    "ConnectionManager",
    "ConnectionReadyFrame",
    "ErrorFrame",
    "PongFrame",
    "ServerFrame",
    "SubscribeFrame",
    "SubscribedFrame",
    "Topic",
    "UnsubscribeFrame",
    "UnsubscribedFrame",
    "WSMessage",
    "get_manager",
    "new_correlation_id",
    "parse_client_frame",
    "reset_manager",
]
