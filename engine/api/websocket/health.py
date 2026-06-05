"""Health endpoint for the WebSocket API (SEV-275).

``GET /health/websocket`` returns a snapshot of:

- the :class:`ConnectionManagerV2` (connection/subscription counts,
  per-family breakdown, ``shutting_down`` flag);
- the :class:`WSRedisBridge` (messages seen, errors, dead-letter
  count, lag, last-message timestamp, connected flag).

The route is read-only and never raises — a broken subsystem still
returns a JSON payload with the fields it could populate, so the
health probe can degrade gracefully.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from engine.api.websocket.connection_manager_v2 import get_manager_v2

if TYPE_CHECKING:
    from engine.api.websocket.redis_bridge import WSRedisBridge

router = APIRouter()


@router.get("/health/websocket")
async def websocket_health(request: Request) -> dict[str, Any]:
    manager = getattr(request.app.state, "ws_manager", None) or get_manager_v2()
    bridge: WSRedisBridge | None = getattr(request.app.state, "ws_bridge", None)
    payload: dict[str, Any] = {"manager": manager.snapshot()}
    if bridge is not None:
        payload["bridge"] = bridge.snapshot()
    else:
        payload["bridge"] = {"connected": False, "reason": "not_configured"}
    return payload


__all__ = ["router"]
