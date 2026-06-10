"""WebSocket health endpoint (SEV-275)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.api.ws.connection_manager import ConnectionManager


def ws_health_snapshot(manager: ConnectionManager) -> dict[str, Any]:
    stats = manager.stats()
    stats["status"] = "healthy" if stats["active_connections"] >= 0 else "unhealthy"
    return {"status": "healthy", "websocket": stats}
