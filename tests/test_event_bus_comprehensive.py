"""Comprehensive tests for engine/events/bus.py — targeting uncovered lines 119-127, 130-132."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from engine.events.bus import Event, EventBus, EventType


@pytest.fixture
def bus():
    return EventBus()


class TestEvent:
    def test_to_dict_shape(self):
        e = Event(EventType.ORDER_CREATED, {"symbol": "AAPL"}, source="test")
        d = e.to_dict()
        assert d["type"] == "order.created"
        assert d["data"] == {"symbol": "AAPL"}
        assert d["source"] == "test"
        assert "timestamp" in d

    def test_to_json_roundtrip(self):
        e = Event(EventType.BACKTEST_STARTED, {"id": 42})
        raw = e.to_json()
        parsed = json.loads(raw)
        assert parsed["type"] == "backtest.started"
        assert parsed["data"]["id"] == 42

    def test_default_data_empty_dict(self):
        e = Event(EventType.ENGINE_STARTED)
        assert e.data == {}
        assert e.source == "engine"

    def test_custom_source(self):
        e = Event(EventType.ENGINE_STARTED, source="custom")
        assert e.source == "custom"


class TestEventBusConnect:
    async def test_connect_success(self, bus):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock()
        with patch("engine.events.bus.aioredis", create=True) as _:
            import redis.asyncio as aioredis

            with patch.object(aioredis, "from_url", return_value=mock_redis):
                await bus.connect()
                assert bus._redis is mock_redis

    async def test_connect_failure_falls_back(self, bus):
        with patch("engine.events.bus.aioredis", create=True):
            import redis.asyncio as aioredis

            with patch.object(aioredis, "from_url", side_effect=Exception("no redis")):
                await bus.connect()
                assert bus._redis is None


class TestEventBusDisconnect:
    async def test_disconnect_closes_redis(self, bus):
        mock_redis = AsyncMock()
        bus._redis = mock_redis
        await bus.disconnect()
        mock_redis.close.assert_awaited_once()
        assert bus._redis is None

    async def test_disconnect_noop_when_no_redis(self, bus):
        bus._redis = None
        await bus.disconnect()
        assert bus._redis is None


class TestSubscribe:
    def test_subscribe_adds_handler(self, bus):
        handler = AsyncMock()
        bus.subscribe(EventType.ORDER_CREATED, handler)
        assert handler in bus._handlers[EventType.ORDER_CREATED]

    def test_subscribe_multiple_handlers_same_type(self, bus):
        h1, h2 = AsyncMock(), AsyncMock()
        bus.subscribe(EventType.ORDER_CREATED, h1)
        bus.subscribe(EventType.ORDER_CREATED, h2)
        assert len(bus._handlers[EventType.ORDER_CREATED]) == 2

    def test_subscribe_different_types(self, bus):
        h1, h2 = AsyncMock(), AsyncMock()
        bus.subscribe(EventType.ORDER_CREATED, h1)
        bus.subscribe(EventType.SIGNAL_EMITTED, h2)
        assert EventType.ORDER_CREATED in bus._handlers
        assert EventType.SIGNAL_EMITTED in bus._handlers


class TestUnsubscribe:
    def test_unsubscribe_removes_handler(self, bus):
        handler = AsyncMock()
        bus.subscribe(EventType.ORDER_CREATED, handler)
        bus.unsubscribe(EventType.ORDER_CREATED, handler)
        assert handler not in bus._handlers.get(EventType.ORDER_CREATED, [])

    def test_unsubscribe_noop_for_missing_type(self, bus):
        handler = AsyncMock()
        bus.unsubscribe(EventType.ORDER_CREATED, handler)

    def test_unsubscribe_preserves_other_handlers(self, bus):
        h1, h2 = AsyncMock(), AsyncMock()
        bus.subscribe(EventType.ORDER_CREATED, h1)
        bus.subscribe(EventType.ORDER_CREATED, h2)
        bus.unsubscribe(EventType.ORDER_CREATED, h1)
        assert h2 in bus._handlers[EventType.ORDER_CREATED]


class TestPublish:
    async def test_publish_calls_handlers(self, bus):
        handler = AsyncMock()
        bus.subscribe(EventType.ORDER_CREATED, handler)
        event = Event(EventType.ORDER_CREATED, {"order_id": "abc"})
        await bus.publish(event)
        handler.assert_awaited_once()
        payload = handler.call_args[0][0]
        assert payload["type"] == "order.created"
        assert payload["data"]["order_id"] == "abc"

    async def test_publish_no_handlers_no_error(self, bus):
        event = Event(EventType.ORDER_CREATED)
        await bus.publish(event)

    async def test_publish_handler_exception_does_not_break_others(self, bus):
        bad = AsyncMock(side_effect=RuntimeError("boom"))
        good = AsyncMock()
        bus.subscribe(EventType.ORDER_CREATED, bad)
        bus.subscribe(EventType.ORDER_CREATED, good)
        await bus.publish(Event(EventType.ORDER_CREATED))
        bad.assert_awaited_once()
        good.assert_awaited_once()

    async def test_publish_appends_to_event_log(self, bus):
        event = Event(EventType.ORDER_CREATED, {"x": 1})
        await bus.publish(event)
        assert len(bus._event_log) == 1
        assert bus._event_log[0]["type"] == "order.created"

    async def test_publish_trims_log_at_max_size(self, bus):
        bus._max_log_size = 3
        for i in range(5):
            await bus.publish(Event(EventType.ORDER_CREATED, {"i": i}))
        assert len(bus._event_log) == 3

    async def test_publish_to_redis(self, bus):
        mock_redis = AsyncMock()
        bus._redis = mock_redis
        event = Event(EventType.ORDER_CREATED, {"x": 1})
        await bus.publish(event)
        mock_redis.publish.assert_awaited_once()
        call_args = mock_redis.publish.call_args
        assert "order.created" in call_args[0][0]

    async def test_publish_redis_error_does_not_crash(self, bus):
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(side_effect=Exception("redis down"))
        bus._redis = mock_redis
        event = Event(EventType.ORDER_CREATED)
        await bus.publish(event)


class TestEmit:
    async def test_emit_creates_and_publishes(self, bus):
        handler = AsyncMock()
        bus.subscribe(EventType.ORDER_CREATED, handler)
        await bus.emit(EventType.ORDER_CREATED, {"symbol": "AAPL"}, source="test_source")
        handler.assert_awaited_once()
        payload = handler.call_args[0][0]
        assert payload["data"]["symbol"] == "AAPL"
        assert payload["source"] == "test_source"

    async def test_emit_defaults(self, bus):
        handler = AsyncMock()
        bus.subscribe(EventType.ENGINE_STARTED, handler)
        await bus.emit(EventType.ENGINE_STARTED)
        payload = handler.call_args[0][0]
        assert payload["data"] == {}
        assert payload["source"] == "engine"


class TestGetRecentEvents:
    async def test_returns_all_by_default(self, bus):
        await bus.emit(EventType.ORDER_CREATED, {"i": 1})
        await bus.emit(EventType.ORDER_CREATED, {"i": 2})
        events = bus.get_recent_events()
        assert len(events) == 2

    async def test_filters_by_type(self, bus):
        await bus.emit(EventType.ORDER_CREATED)
        await bus.emit(EventType.SIGNAL_EMITTED)
        events = bus.get_recent_events(event_type=EventType.ORDER_CREATED)
        assert len(events) == 1
        assert events[0]["type"] == "order.created"

    async def test_respects_limit(self, bus):
        for i in range(10):
            await bus.emit(EventType.ORDER_CREATED, {"i": i})
        events = bus.get_recent_events(limit=3)
        assert len(events) == 3
        assert events[-1]["data"]["i"] == 9

    async def test_empty_log(self, bus):
        events = bus.get_recent_events()
        assert events == []


class TestEventTypeEnum:
    def test_all_event_types_are_strings(self):
        for et in EventType:
            assert isinstance(et.value, str)

    def test_market_event_types_exist(self):
        assert EventType.MARKET_DATA_UPDATE
        assert EventType.MARKET_OPEN
        assert EventType.MARKET_CLOSE

    def test_order_lifecycle_events(self):
        assert EventType.ORDER_CREATED.value == "order.created"
        assert EventType.ORDER_FILLED.value == "order.filled"
        assert EventType.ORDER_REJECTED.value == "order.rejected"
