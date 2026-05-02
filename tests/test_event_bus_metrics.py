"""Tests for EventBus metrics emission (gh#34 follow-up).

The bus emits the following metrics through the active
``MetricsBackend``:

- ``event_bus.published`` — counter, exactly once per ``publish`` call
  regardless of subscribers (tags: ``event_type``).
- ``event_bus.handler_duration_ms`` — histogram, one observation per
  handler invocation (success or failure) (tags: ``event_type``).
- ``event_bus.handler_error`` — counter, once per handler that raised
  (tags: ``event_type``).
- ``event_bus.redis_publish_error`` — counter, once per Redis publish
  that raised (tags: ``event_type``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.events.bus import Event, EventBus, EventType
from engine.observability.metrics import RecordingBackend


def _counter_total(backend: RecordingBackend, name: str) -> float:
    return sum(v for (n, _t), v in backend.counters.items() if n == name)


def _counter_with(
    backend: RecordingBackend, name: str, tags: dict[str, str]
) -> float:
    expected = tuple(sorted(tags.items()))
    return sum(
        v
        for (n, t), v in backend.counters.items()
        if n == name and all(item in t for item in expected)
    )


def _histogram_observations(
    backend: RecordingBackend, name: str
) -> list[tuple[tuple[tuple[str, str], ...], list[float]]]:
    return [(t, vs) for (n, t), vs in backend.histograms.items() if n == name]


@pytest.fixture
def metrics() -> RecordingBackend:
    return RecordingBackend()


class TestPublished:
    async def test_published_counter_increments_with_no_subscribers(self, metrics):
        bus = EventBus(metrics=metrics)
        await bus.publish(Event(EventType.MARKET_DATA_UPDATE, {"sym": "AAPL"}))

        assert (
            _counter_with(
                metrics,
                "event_bus.published",
                {"event_type": "market.data.update"},
            )
            == 1
        )

    async def test_published_counts_per_event_type(self, metrics):
        bus = EventBus(metrics=metrics)
        await bus.publish(Event(EventType.ORDER_CREATED))
        await bus.publish(Event(EventType.ORDER_FILLED))
        await bus.publish(Event(EventType.ORDER_FILLED))

        assert (
            _counter_with(
                metrics,
                "event_bus.published",
                {"event_type": "order.created"},
            )
            == 1
        )
        assert (
            _counter_with(
                metrics,
                "event_bus.published",
                {"event_type": "order.filled"},
            )
            == 2
        )


class TestHandlerDurationAndErrors:
    async def test_successful_handler_records_duration_only(self, metrics):
        bus = EventBus(metrics=metrics)
        handler = AsyncMock()
        bus.subscribe(EventType.SIGNAL_EMITTED, handler)

        await bus.publish(Event(EventType.SIGNAL_EMITTED))

        observations = _histogram_observations(
            metrics, "event_bus.handler_duration_ms"
        )
        assert len(observations) == 1
        tags, values = observations[0]
        assert ("event_type", "signal.emitted") in tags
        assert len(values) == 1
        # No error counter bumped on the success path.
        assert _counter_total(metrics, "event_bus.handler_error") == 0

    async def test_handler_raising_records_error_counter_and_duration(self, metrics):
        bus = EventBus(metrics=metrics)

        async def boom(_):
            raise RuntimeError("nope")

        bus.subscribe(EventType.STRATEGY_ERROR, boom)

        # publish must NOT propagate handler errors.
        await bus.publish(Event(EventType.STRATEGY_ERROR))

        assert (
            _counter_with(
                metrics,
                "event_bus.handler_error",
                {"event_type": "strategy.error"},
            )
            == 1
        )
        # Histogram still gets the (failed) attempt.
        observations = _histogram_observations(
            metrics, "event_bus.handler_duration_ms"
        )
        assert len(observations) == 1

    async def test_one_failing_handler_does_not_block_other_handlers(self, metrics):
        bus = EventBus(metrics=metrics)

        async def boom(_):
            raise RuntimeError("nope")

        good = AsyncMock()
        bus.subscribe(EventType.SIGNAL_EMITTED, boom)
        bus.subscribe(EventType.SIGNAL_EMITTED, good)

        await bus.publish(Event(EventType.SIGNAL_EMITTED))

        good.assert_awaited_once()
        # Two histogram observations (one per handler), one error counter.
        observations = _histogram_observations(
            metrics, "event_bus.handler_duration_ms"
        )
        assert sum(len(vs) for _, vs in observations) == 2
        assert _counter_total(metrics, "event_bus.handler_error") == 1


class TestRedisErrors:
    async def test_redis_publish_error_increments_counter(self, metrics):
        bus = EventBus(metrics=metrics)
        # Inject a redis double whose publish raises.
        bus._redis = MagicMock()
        bus._redis.publish = AsyncMock(side_effect=ConnectionError("down"))

        await bus.publish(Event(EventType.MARKET_OPEN))

        assert (
            _counter_with(
                metrics,
                "event_bus.redis_publish_error",
                {"event_type": "market.open"},
            )
            == 1
        )


class TestDefaultBackend:
    async def test_resolves_get_metrics_when_not_injected(self):
        from engine.observability.metrics import NullBackend, set_metrics

        recording = RecordingBackend()
        set_metrics(recording)
        try:
            bus = EventBus()
            await bus.publish(Event(EventType.ENGINE_STARTED))
            assert _counter_total(recording, "event_bus.published") == 1
        finally:
            set_metrics(NullBackend())
