"""
Event Bus — pub/sub system for decoupled communication between engine modules.

Events flow through Redis pub/sub for cross-process communication,
with an in-process fallback for single-instance deployments.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from engine.observability.metrics import MetricsBackend, get_metrics

logger = structlog.get_logger()

# Type alias for event handlers
EventHandler = Callable[[dict], Awaitable[None]]


class EventType(StrEnum):
    """All event types in the system."""

    # Market events
    MARKET_DATA_UPDATE = "market.data.update"
    MARKET_OPEN = "market.open"
    MARKET_CLOSE = "market.close"

    # Signal events
    SIGNAL_EMITTED = "signal.emitted"
    SIGNAL_BATCH = "signal.batch"
    SIGNAL_GENERATED = "signal.generated"

    # Order events
    ORDER_CREATED = "order.created"
    ORDER_VALIDATED = "order.validated"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_PARTIALLY_FILLED = "order.partially_filled"
    ORDER_CANCELLED = "order.cancelled"
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
    BACKTEST_FAILED = "backtest.failed"


class Event:
    """A single event payload."""

    def __init__(
        self, event_type: EventType, data: dict[str, Any] | None = None, source: str = "engine"
    ):
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

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_log_size: int = 10_000,
        *,
        metrics: MetricsBackend | None = None,
    ):
        self.redis_url = redis_url
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._redis = None
        self._event_log: list[dict] = []
        self._max_log_size = max_log_size
        self._metrics = metrics

    @property
    def metrics(self) -> MetricsBackend:
        """Resolve the metrics backend lazily so tests can swap the
        process-wide singleton via :func:`set_metrics` after construction."""
        return self._metrics if self._metrics is not None else get_metrics()

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
        logger.debug(
            "event_bus.subscribed",
            event_type=event_type.value,
            handler=handler.__name__,
        )

    def unsubscribe(self, event_type: EventType, handler: EventHandler):
        if event_type in self._handlers:
            self._handlers[event_type] = [h for h in self._handlers[event_type] if h != handler]

    async def publish(self, event: Event):
        """Publish an event to all subscribers and Redis."""
        metrics = self.metrics
        event_tags = {"event_type": event.event_type.value}
        metrics.counter("event_bus.published", tags=event_tags)

        # In-process handlers
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            t0 = time.monotonic()
            try:
                await handler(event.to_dict())
                metrics.histogram(
                    "event_bus.handler_duration_ms",
                    (time.monotonic() - t0) * 1000.0,
                    tags=event_tags,
                )
            except Exception as e:
                metrics.histogram(
                    "event_bus.handler_duration_ms",
                    (time.monotonic() - t0) * 1000.0,
                    tags=event_tags,
                )
                metrics.counter("event_bus.handler_error", tags=event_tags)
                logger.exception(
                    "event_bus.handler_error",
                    event_type=event.event_type.value,
                    error=str(e),
                )

        # Redis pub/sub for cross-process consumers
        if self._redis:
            try:
                await self._redis.publish(f"nexus:{event.event_type.value}", event.to_json())
            except Exception:
                metrics.counter("event_bus.redis_publish_error", tags=event_tags)

        # Local event log (ring buffer)
        self._event_log.append(event.to_dict())
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size :]

    async def emit(self, event_type: EventType, data: dict | None = None, source: str = "engine"):
        """Convenience method: create and publish an event in one call."""
        await self.publish(Event(event_type, data, source))

    def get_recent_events(self, event_type: EventType = None, limit: int = 100) -> list[dict]:
        """Retrieve recent events from the in-memory log."""
        events = self._event_log
        if event_type:
            events = [e for e in events if e["type"] == event_type.value]
        return events[-limit:]


# ---------------------------------------------------------------------------
# Process-wide lazy singleton
# ---------------------------------------------------------------------------
#
# ``get_event_bus`` provides a lazily-constructed, process-wide
# :class:`EventBus`.  The first caller builds (and connects) the bus; the
# construction is serialized with an :class:`asyncio.Lock` so that, even under
# concurrent access, exactly one instance is created.  Subsequent callers get
# the cached singleton without touching the lock.


class _EventBusState:
    """Mutable holder for the process-wide singleton and its init lock.

    Mutating instance attributes avoids the ``global`` statement while still
    sharing state across module-level helpers.
    """

    bus: EventBus | None
    lock: asyncio.Lock | None


_state = _EventBusState()
_state.bus = None
_state.lock = None


def _get_event_bus_lock() -> asyncio.Lock:
    """Return the singleton init lock, creating it lazily.

    The lock is created on first use (inside an async context) rather than at
    import time so module import never depends on a running event loop.
    """
    if _state.lock is None:
        _state.lock = asyncio.Lock()
    return _state.lock


async def get_event_bus(redis_url: str | None = None) -> EventBus:
    """Return the shared :class:`EventBus` singleton.

    The bus is constructed and connected exactly once.  Concurrent callers are
    serialized through an :class:`asyncio.Lock` (double-checked locking) so the
    singleton is never built twice even when many tasks race on the first call.

    ``redis_url`` is only consulted on the very first construction; later calls
    return the cached instance regardless of the argument.
    """
    if _state.bus is not None:
        return _state.bus
    async with _get_event_bus_lock():
        # Re-check inside the lock — another task may have built it while we
        # were waiting.
        if _state.bus is not None:
            return _state.bus
        from engine.config import settings

        url = redis_url or settings.valkey_url
        bus = EventBus(redis_url=url)
        await bus.connect()
        _state.bus = bus
    return _state.bus


def set_event_bus(bus: EventBus | None) -> None:
    """Inject (or clear) the process-wide singleton.

    Primarily for tests that want to observe emitted events without going
    through the lazy connect path.
    """
    _state.bus = bus


def reset_event_bus_for_tests() -> None:
    """Reset the singleton (and its lock) to a pristine state for tests."""
    _state.bus = None
    _state.lock = None
