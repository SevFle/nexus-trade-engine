"""Engine WebSocket bridge package.

Holds the focused EventBus → WebSocket ``EventBusBridge`` that fans
order/trade and signal events out to connected WebSocket clients via a
``ConnectionManager``. Kept as a top-level ``engine.ws`` package so the
bridge can be imported without pulling in the heavier
``engine.api.ws`` package (FastAPI route handlers, auth middleware,
permission matrix, etc.).

The bridge stamps every envelope with the resolved ``user_id`` /
``tenant_id`` carried in the event payload and routes user-scoped
events to the corresponding ``user:<id>`` room so a broadcast never
leaks across identities (SEV: ws cross-user isolation).
"""

from __future__ import annotations

from engine.ws.bridge import DEFAULT_EVENT_CHANNELS, DEFAULT_EVENT_TYPES, EventBusBridge

__all__ = ["DEFAULT_EVENT_CHANNELS", "DEFAULT_EVENT_TYPES", "EventBusBridge"]
