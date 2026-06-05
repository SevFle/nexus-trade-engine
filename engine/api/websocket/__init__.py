"""Real-time WebSocket API.

Public surface:

- :class:`ConnectionManager` — legacy per-user fan-out registry for the
  ``/api/v1/ws`` endpoint (gh#7). Process-local.
- :class:`Topic` — legacy broadcast channels (``portfolio``, ``backtest``,
  ``order``, ``alert``) used by the gh#7 endpoint and its bridge.
- :class:`ConnectionManagerV2` — SEV-275 manager with per-connection
  bounded queues, backpressure disconnects, and per-symbol fan-out.
- :class:`WSRedisBridge` — Redis pub/sub → ConnectionManagerV2 dispatcher
  with reconnect, dead-letter, and health snapshot.

The new endpoints live at ``/api/v1/ws/v2`` (multiplexed),
``/api/v1/ws/portfolio``, ``/api/v1/ws/orders``, and ``/api/v1/ws/market``.
The legacy route at :mod:`engine.api.routes.websocket` is unchanged.
"""

from engine.api.websocket.connection_manager_v2 import (
    ConnectionManagerV2,
    get_manager_v2,
)
from engine.api.websocket.manager import ConnectionManager, Topic
from engine.api.websocket.redis_bridge import WSRedisBridge

__all__ = [
    "ConnectionManager",
    "ConnectionManagerV2",
    "Topic",
    "WSRedisBridge",
    "get_manager_v2",
]
