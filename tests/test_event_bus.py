"""Tests for the EventBus pub/sub system."""

from __future__ import annotations

from engine.events.bus import Event, EventBus, EventType


def _make_handler(results: list):
    async def handler(event: dict) -> None:
        results.append(event)

    handler.__name__ = f"handler_{id(results)}"
    return handler


class TestSubscribeAndPublish:
    async def test_subscribe_and_publish_calls_handler(self):
        bus = EventBus()
        results: list[dict] = []
        handler = _make_handler(results)
        bus.subscribe(EventType.ORDER_CREATED, handler)

        event = Event(EventType.ORDER_CREATED, data={"order_id": "abc"})
        await bus.publish(event)

        assert len(results) == 1
        assert results[0]["data"]["order_id"] == "abc"

    async def test_unsubscribe_stops_handler(self):
        bus = EventBus()
        results: list[dict] = []
        handler = _make_handler(results)
        bus.subscribe(EventType.ORDER_CREATED, handler)
        bus.unsubscribe(EventType.ORDER_CREATED, handler)

        await bus.publish(Event(EventType.ORDER_CREATED))
        assert len(results) == 0

    async def test_multiple_handlers_all_called(self):
        bus = EventBus()
        r1: list[dict] = []
        r2: list[dict] = []
        bus.subscribe(EventType.ORDER_CREATED, _make_handler(r1))
        bus.subscribe(EventType.ORDER_CREATED, _make_handler(r2))

        await bus.publish(Event(EventType.ORDER_CREATED))
        assert len(r1) == 1
        assert len(r2) == 1

    async def test_handler_for_different_event_not_called(self):
        bus = EventBus()
        results: list[dict] = []
        bus.subscribe(EventType.ORDER_CREATED, _make_handler(results))

        await bus.publish(Event(EventType.ENGINE_STARTED))
        assert len(results) == 0


class TestErrorHandlerResilience:
    async def test_error_in_handler_does_not_stop_others(self):
        bus = EventBus()
        r_ok: list[dict] = []

        async def bad_handler(event: dict) -> None:
            raise RuntimeError("boom")

        bad_handler.__name__ = "bad_handler"

        bus.subscribe(EventType.ORDER_CREATED, bad_handler)
        bus.subscribe(EventType.ORDER_CREATED, _make_handler(r_ok))

        await bus.publish(Event(EventType.ORDER_CREATED))
        assert len(r_ok) == 1


class TestEventLog:
    async def test_events_logged_to_ring_buffer(self):
        bus = EventBus()
        await bus.publish(Event(EventType.ORDER_CREATED, data={"x": 1}))
        await bus.publish(Event(EventType.SIGNAL_EMITTED, data={"y": 2}))

        recent = bus.get_recent_events()
        assert len(recent) == 2

    async def test_ring_buffer_evicts_oldest(self):
        bus = EventBus(max_log_size=5)

        for i in range(10):
            await bus.publish(Event(EventType.ORDER_CREATED, data={"i": i}))

        recent = bus.get_recent_events()
        assert len(recent) <= 5
        assert recent[0]["data"]["i"] == 5

    async def test_get_recent_events_filter_by_type(self):
        bus = EventBus()
        await bus.publish(Event(EventType.ORDER_CREATED))
        await bus.publish(Event(EventType.SIGNAL_EMITTED))
        await bus.publish(Event(EventType.ORDER_CREATED))

        order_events = bus.get_recent_events(event_type=EventType.ORDER_CREATED)
        assert len(order_events) == 2

    async def test_get_recent_events_limit(self):
        bus = EventBus()
        for i in range(20):
            await bus.publish(Event(EventType.ORDER_CREATED, data={"i": i}))

        recent = bus.get_recent_events(limit=3)
        assert len(recent) == 3


class TestEmit:
    async def test_emit_creates_and_publishes(self):
        bus = EventBus()
        results: list[dict] = []
        bus.subscribe(EventType.ENGINE_STARTED, _make_handler(results))

        await bus.emit(EventType.ENGINE_STARTED, data={"msg": "hello"}, source="test")
        assert len(results) == 1
        assert results[0]["data"]["msg"] == "hello"
        assert results[0]["source"] == "test"


class TestStructlogKwargRegression:
    """Regression: structlog reserves the ``event`` kwarg for the
    positional event-name argument. Earlier the bus passed
    ``event=event_type.value`` to the logger and that raised TypeError
    whenever the configured wrapper actually processed the call (e.g.
    debug at NOTSET filtering). The field is now ``event_type=`` so
    subscribe / publish cannot raise from this clash."""

    async def test_subscribe_does_not_raise_with_default_structlog(self):
        bus = EventBus()
        bus.subscribe(EventType.ORDER_CREATED, _make_handler([]))

    async def test_publish_does_not_raise_when_handler_errors(self):
        bus = EventBus()

        async def boom(_):
            raise RuntimeError("nope")

        bus.subscribe(EventType.STRATEGY_ERROR, boom)
        # Must NOT raise — handler errors are logged via event_type=,
        # never propagated.
        await bus.publish(Event(EventType.STRATEGY_ERROR))


class TestEventSerialization:
    def test_event_to_dict(self):
        event = Event(EventType.ORDER_CREATED, data={"order_id": "123"}, source="om")
        d = event.to_dict()
        assert d["type"] == "order.created"
        assert d["data"]["order_id"] == "123"
        assert d["source"] == "om"
        assert "timestamp" in d

    def test_event_to_json(self):
        event = Event(EventType.ORDER_CREATED, data={"key": "val"})
        j = event.to_json()
        assert '"order.created"' in j
        assert '"key"' in j
