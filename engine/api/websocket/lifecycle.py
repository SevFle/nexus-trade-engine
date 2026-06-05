"""FastAPI lifespan integration for the WebSocket API (SEV-275).

Two functions, both async, both meant to be called from the
application's lifespan context manager:

- :func:`startup` — instantiate (or reuse) the
  :class:`~engine.api.websocket.connection_manager_v2.ConnectionManagerV2`
  and start the :class:`~engine.api.websocket.redis_bridge.WSRedisBridge`
  background task. Idempotent — safe to call multiple times.
- :func:`shutdown` — stop the bridge, broadcast ``server_shutdown`` to
  every open connection, drain queues within the configured timeout,
  close connections cleanly.

The bridge and manager live on ``app.state`` so request handlers can
introspect them via dependency injection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from engine.api.websocket.connection_manager_v2 import (
    ConnectionManagerV2,
    get_manager_v2,
    set_manager_v2,
)
from engine.api.websocket.redis_bridge import WSRedisBridge

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger()


async def startup(
    app: FastAPI,
    *,
    redis_url: str | None = None,
    manager: ConnectionManagerV2 | None = None,
) -> WSRedisBridge | None:
    """Initialise the WebSocket subsystem.

    Returns the started :class:`WSRedisBridge` (or ``None`` if Redis
    is disabled). The same instance is also stored on
    ``app.state.ws_bridge`` for the health endpoint to introspect.
    """
    if manager is not None:
        set_manager_v2(manager)
    manager_v2 = get_manager_v2()
    app.state.ws_manager = manager_v2

    if redis_url is None:
        # Bridge is optional — tests can run without Redis.
        app.state.ws_bridge = None
        return None

    bridge = WSRedisBridge(manager_v2, redis_url=redis_url)
    app.state.ws_bridge = bridge
    bridge.start()
    logger.info("ws_v2.bridge_started", redis_url=redis_url)
    return bridge


async def shutdown(app: FastAPI) -> None:
    """Drain the manager and stop the bridge."""
    manager: ConnectionManagerV2 | None = getattr(app.state, "ws_manager", None)
    bridge: WSRedisBridge | None = getattr(app.state, "ws_bridge", None)

    if bridge is not None:
        try:
            await bridge.stop(timeout=5.0)
        except Exception:
            logger.exception("ws_v2.bridge_stop_failed")

    if manager is not None:
        try:
            await manager.shutdown_all(reason="shutdown")
        except Exception:
            logger.exception("ws_v2.shutdown_all_failed")


__all__ = ["shutdown", "startup"]
