"""EventBus to WebSocket bridge (SEV-275).

Subscribes to engine EventBus events and fans them out to WebSocket
connections via room-based broadcast.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from engine.api.ws.metrics import ws_metrics
from engine.api.ws.permissions import resolve_room_name
from engine.api.ws.protocol import EventMessage

if TYPE_CHECKING:
    from engine.api.ws.connection_manager import ConnectionManager
    from engine.events.bus import EventBus, EventType

logger = structlog.get_logger()

_EVENT_TO_CHANNEL: dict[str, str] = {
    "portfolio_updated": "portfolio",
    "position_opened": "portfolio",
    "position_closed": "portfolio",
    "order_created": "orders",
    "order_validated": "orders",
    "order_submitted": "orders",
    "order_filled": "orders",
    "order_rejected": "orders",
    "order_failed": "orders",
    "strategy_loaded": "strategies",
    "strategy_unloaded": "strategies",
    "strategy_error": "strategies",
}


class EventBusBridge:
    """Bridges EventBus events to WebSocket connections."""

    def __init__(
        self,
        bus: EventBus,
        manager: ConnectionManager,
        concurrency: int = 32,
    ) -> None:
        self._bus = bus
        self._manager = manager
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._registered: list = []
        self._handler = self._handle

    def start(self, event_types: list[EventType] | None = None) -> None:
        from engine.events.bus import EventType  # noqa: PLC0415

        if event_types is None:
            event_types = [
                EventType.PORTFOLIO_UPDATED,
                EventType.POSITION_OPENED,
                EventType.POSITION_CLOSED,
                EventType.ORDER_CREATED,
                EventType.ORDER_VALIDATED,
                EventType.ORDER_SUBMITTED,
                EventType.ORDER_FILLED,
                EventType.ORDER_REJECTED,
                EventType.ORDER_FAILED,
                EventType.STRATEGY_LOADED,
                EventType.STRATEGY_UNLOADED,
                EventType.STRATEGY_ERROR,
            ]
        for et in event_types:
            self._bus.subscribe(et, self._handler)
            self._registered.append(et)
        logger.info("ws_bridge.started", event_types=len(self._registered))

    def stop(self) -> None:
        for et in self._registered:
            try:
                self._bus.unsubscribe(et, self._handler)
            except Exception as exc:
                logger.warning(
                    "ws_bridge.unsubscribe_failed",
                    event_type=getattr(et, "value", str(et)),
                    error=str(exc),
                )
        self._registered.clear()
        logger.info("ws_bridge.stopped")

    async def _handle(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        mapping = _EVENT_TO_CHANNEL.get(event_type)
        if mapping is None:
            return
        channel = mapping
        raw_data = payload.get("data", {})
        data = raw_data if isinstance(raw_data, dict) else {}
        try:
            resolved = resolve_room_name(channel, data)
            room = resolved if resolved else channel
            task = asyncio.create_task(self._dispatch(room, channel, payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            ws_metrics.metrics.counter(
                "sev_ws_messages_dropped_total", tags={"reason": "dispatch_error"}
            )
            logger.exception("ws_bridge.handle_error", channel=channel)

    async def _dispatch(self, room: str, channel: str, payload: dict[str, Any]) -> None:
        async with self._semaphore:
            t0 = time.monotonic()
            try:
                seq = self._manager.next_seq(room)
                msg = EventMessage(
                    channel=channel,
                    room=room,
                    payload=payload,
                    seq=seq,
                )
                user_id = payload.get("data", {}).get("user_id")
                if user_id:
                    user_room = f"user:{user_id}"
                    await self._manager.broadcast(user_room, msg)
                await self._manager.broadcast(room, msg)
                lag = time.monotonic() - t0
                if lag > 1.0:
                    ws_metrics.metrics.histogram("sev_ws_event_bus_lag_seconds", lag * 1000)
            except Exception:
                ws_metrics.metrics.counter(
                    "sev_ws_messages_dropped_total",
                    tags={"reason": "serialize_error"},
                )
                logger.exception("ws_bridge.dispatch_error", room=room, channel=channel)
