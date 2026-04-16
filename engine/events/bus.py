"""
Event Bus — pub/sub system for decoupled communication between engine modules.

Events flow through Redis pub/sub for cross-process communication,
with an in-process fallback for single-instance deployments.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()

# Type alias for event handlers
EventHandler = Callable[[dict], Awaitable[None]]


class EventType(str, Enum):
    """All event types in the system."""

    # Market events
    MARKET_DATA_UPDATE = "market.data.update"
    MARKET_OPEN = "market.open"
    MARKET_CLOSE = "market.close"

    # Signal events
    SIGNAL_EMITTED = "signal.emitted"
    SIGNAL_BATCH = "signal.batch"

    # Order events
    ORDER_CREATED = "order.created"
    ORDER_VALIDATED = "order.validated"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_REJECTED = "order.rejected"
    ORDER_FAILED = "order.failed"

    # Portfolio events
    PORTFOLIO_UPDATED = "portfolio.updated"
    POSITION_OPENED = "position.opened"
    POSITION_CLOSED = "position.closed"

    # Strategy events
    STRATEGY_LOADED = "strategy.loaded"
    STRATEGY_UNLOADED = "strategy.unloaded"
    STRATEGY_ERROR = "strategy.error"

    # Risk events
    RISK_WARNING = "risk.warning"
    CIRCUIT_BREAKER = "risk.circuit_breaker"

    # System events
    ENGINE_STARTED = "engine.started"
    ENGINE_STOPPED = "engine.stopped"
    BACKTEST_STARTED = "backtest.started"
    BACKTEST_COMPLETED = "backtest.completed"


class Event:
    """A single event payload."""

    def __init__(self, event_type: EventType, data: dict[str, Any] = None, source: str = "engine"):
        self.event_type = event_type
        self.data = data or {}
        self.source = source
        self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "type": self.event_type.value,
            "data": self.data,
            "source": self.source,
            "timestamp": self.timestamp,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class EventBus:
    """
    Pub/sub event bus with Redis backend.

    Modules subscribe to event types and receive async callbacks.
    Events are also persisted to Redis for cross-process consumers (e.g. frontend WebSocket).
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self.redis_url = redis_url
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._redis = None
        self._event_log: list[dict] = []
        self._max_log_size = 10_000

    async def connect(self):
        """Initialize Redis connection for cross-process pub/sub."""
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self.redis_url)
            await self._redis.ping()
            logger.info("event_bus.redis_connected")
        except Exception as e:
            logger.warning("event_bus.redis_unavailable", error=str(e), fallback="in-process only")
            self._redis = None

    async def disconnect(self):
        if self._redis:
            await self._redis.close()
            self._redis = None

    def subscribe(self, event_type: EventType, handler: EventHandler):
        """Register a handler for an event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.debug("event_bus.subscribed", event=event_type.value, handler=handler.__name__)

    def unsubscribe(self, event_type: EventType, handler: EventHandler):
        if event_type in self._handlers:
            self._handlers[event_type] = [h for h in self._handlers[event_type] if h != handler]

    async def publish(self, event: Event):
        """Publish an event to all subscribers and Redis."""
        # In-process handlers
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                await handler(event.to_dict())
            except Exception as e:
                logger.error("event_bus.handler_error", event=event.event_type.value, error=str(e))

        # Redis pub/sub for cross-process consumers
        if self._redis:
            try:
                await self._redis.publish(f"nexus:{event.event_type.value}", event.to_json())
            except Exception:
                pass  # Non-critical — log but don't crash

        # Local event log (ring buffer)
        self._event_log.append(event.to_dict())
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size:]

    async def emit(self, event_type: EventType, data: dict = None, source: str = "engine"):
        """Convenience method: create and publish an event in one call."""
        await self.publish(Event(event_type, data, source))

    def get_recent_events(self, event_type: EventType = None, limit: int = 100) -> list[dict]:
        """Retrieve recent events from the in-memory log."""
        events = self._event_log
        if event_type:
            events = [e for e in events if e["type"] == event_type.value]
        return events[-limit:]
