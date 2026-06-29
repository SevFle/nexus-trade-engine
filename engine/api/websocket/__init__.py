"""Real-time WebSocket API (gh#7, SEV-298).

Public surface:

- :class:`ConnectionManager` — the primary **channel-based pub/sub**
  registry (SEV-298). Tracks connections by string id and routes
  messages to channel subscribers with concurrent fan-out and automatic
  cleanup of dead connections.
- :class:`UserTopicManager` — legacy per-user, topic-scoped registry
  (gh#7) backing the authenticated ``/ws`` route and the EventBus bridge.
- :class:`Topic` — string-typed broadcast channels for the per-user
  manager: ``portfolio``, ``backtest``, ``order``, ``alert``.

The channel-based route handler lives at :mod:`engine.api.routes.websocket`.
"""

from engine.api.websocket.manager import (
    ConnectionManager,
    Topic,
    UserTopicManager,
)

__all__ = ["ConnectionManager", "Topic", "UserTopicManager"]
