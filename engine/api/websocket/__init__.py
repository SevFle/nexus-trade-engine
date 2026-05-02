"""Real-time WebSocket API (gh#7).

Public surface:

- :class:`ConnectionManager` — per-user fan-out registry. Process-local
  today; future work routes broadcasts through Redis/Valkey for
  multi-replica deployments.
- :class:`Topic` — string-typed broadcast channels: ``portfolio``,
  ``backtest``, ``order``, ``alert``.

The route handler lives at :mod:`engine.api.routes.websocket`.
"""

from engine.api.websocket.manager import ConnectionManager, Topic

__all__ = ["ConnectionManager", "Topic"]
