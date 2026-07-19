"""Unit tests for ``engine.ws.bridge`` — the focused EventBus →
WebSocket bridge for order, trade, and signal events.

These tests deliberately use light fakes for both the EventBus and the
ConnectionManager. The bridge is duck-typed against their interfaces
(``subscribe`` / ``unsubscribe`` on the bus, async ``broadcast`` on the
manager), so the fakes only need to model the surface the bridge
touches — no Redis, no FastAPI, no Pydantic, no real WebSocket.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

import pytest

from engine.events.bus import EventType
from engine.ws.bridge import (
    DEFAULT_EVENT_CHANNELS,
    DEFAULT_EVENT_TYPES,
    EventBusBridge,
    serialize_event,
)

# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class FakeEventBus:
    """Minimal stand-in for :class:`~engine.events.bus.EventBus`.

    Records every (event_type, handler) pair the bridge subscribes and
    lets a test deliver a payload by calling :meth:`deliver`. The
    ``unsubscribe`` bookkeeping is identity-based, mirroring the real
    bus, so a bridge that re-uses its cached bound method round-trips
    cleanly.
    """

    def __init__(self) -> None:
        self._handlers: dict[EventType, list] = {}
        self.subscribe_calls: list[tuple[EventType, object]] = []
        self.unsubscribe_calls: list[tuple[EventType, object]] = []

    def subscribe(self, event_type: EventType, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)
        self.subscribe_calls.append((event_type, handler))

    def unsubscribe(self, event_type: EventType, handler) -> None:
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h is not handler
            ]
        self.unsubscribe_calls.append((event_type, handler))

    async def deliver(self, event_type: EventType, payload: dict) -> None:
        """Simulate the bus delivering a payload to each subscriber."""
        for handler in list(self._handlers.get(event_type, [])):
            await handler(payload)

    def handler_count(self, event_type: EventType) -> int:
        return len(self._handlers.get(event_type, []))


class RecordingManager:
    """Records every ``broadcast`` call so a test can assert on it.

    Returns a configurable recipient count from ``broadcast`` so the
    bridge's debug log line gets a realistic value. Raising on broadcast
    is also supported to exercise the error-swallowing path.
    """

    def __init__(self, *, recipients: int = 1) -> None:
        self.broadcasts: list[tuple[str, dict]] = []
        self._recipients = recipients
        self.broadcast_exception: BaseException | None = None

    async def broadcast(self, channel: str, message: dict) -> int:
        if self.broadcast_exception is not None:
            raise self.broadcast_exception
        self.broadcasts.append((channel, message))
        return self._recipients


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bus() -> FakeEventBus:
    return FakeEventBus()


@pytest.fixture
def manager() -> RecordingManager:
    return RecordingManager()


@pytest.fixture
def bridge(bus: FakeEventBus, manager: RecordingManager) -> EventBusBridge:
    """A started bridge wired into the fakes.

    Tests that exercise ``start`` / ``stop`` lifecycle directly should
    build their own bridge rather than reuse this fixture.
    """
    b = EventBusBridge(bus=bus, manager=manager)
    b.start()
    return b


# ---------------------------------------------------------------------------
# Defaults & channel mapping
# ---------------------------------------------------------------------------


class TestDefaultMapping:
    """The brief lists ORDER_FILLED, ORDER_PARTIALLY_FILLED,
    ORDER_CANCELLED, and SIGNAL_GENERATED as the subscribed event types.
    Pin each one's presence and channel."""

    def test_order_filled_routes_to_orders(self):
        assert DEFAULT_EVENT_CHANNELS[EventType.ORDER_FILLED] == "orders"

    def test_order_partially_filled_routes_to_orders(self):
        assert DEFAULT_EVENT_CHANNELS[EventType.ORDER_PARTIALLY_FILLED] == "orders"

    def test_order_cancelled_routes_to_orders(self):
        assert DEFAULT_EVENT_CHANNELS[EventType.ORDER_CANCELLED] == "orders"

    def test_signal_generated_routes_to_signals(self):
        assert DEFAULT_EVENT_CHANNELS[EventType.SIGNAL_GENERATED] == "signals"

    def test_default_event_types_match_channels(self):
        assert set(DEFAULT_EVENT_TYPES) == set(DEFAULT_EVENT_CHANNELS.keys())

    def test_default_set_is_immutable_tuple(self):
        assert isinstance(DEFAULT_EVENT_TYPES, tuple)
        # All four event types from the brief are present.
        assert EventType.ORDER_FILLED in DEFAULT_EVENT_TYPES
        assert EventType.ORDER_PARTIALLY_FILLED in DEFAULT_EVENT_TYPES
        assert EventType.ORDER_CANCELLED in DEFAULT_EVENT_TYPES
        assert EventType.SIGNAL_GENERATED in DEFAULT_EVENT_TYPES


class TestChannelFor:
    """``channel_for`` resolves an event type (member or dotted string)
    to its channel, returning ``None`` for unrouted types."""

    def test_member_resolves(self, bridge: EventBusBridge):
        assert bridge.channel_for(EventType.ORDER_FILLED) == "orders"
        assert bridge.channel_for(EventType.SIGNAL_GENERATED) == "signals"

    def test_dotted_string_resolves(self, bridge: EventBusBridge):
        # StrEnum members hash equal to their .value, so a producer that
        # hands the bridge a raw dotted string still routes correctly.
        assert bridge.channel_for("order.filled") == "orders"
        assert bridge.channel_for("order.partially_filled") == "orders"
        assert bridge.channel_for("order.cancelled") == "orders"
        assert bridge.channel_for("signal.generated") == "signals"

    def test_unrouted_event_type_returns_none(self, bridge: EventBusBridge):
        assert bridge.channel_for(EventType.MARKET_DATA_UPDATE) is None
        assert bridge.channel_for(EventType.PORTFOLIO_UPDATED) is None

    def test_unknown_string_returns_none(self, bridge: EventBusBridge):
        assert bridge.channel_for("not.an.event") is None
        assert bridge.channel_for("") is None


# ---------------------------------------------------------------------------
# Lifecycle — start and stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_subscribes_all_default_event_types(
        self, bus: FakeEventBus, manager: RecordingManager
    ):
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()

        assert set(bridge.subscribed_event_types) == set(DEFAULT_EVENT_TYPES)
        # One subscribe per event type, each with the same handler
        # reference (cached bound method).
        assert len(bus.subscribe_calls) == len(DEFAULT_EVENT_TYPES)
        handlers = {h for _, h in bus.subscribe_calls}
        assert len(handlers) == 1
        # No unsubscribes yet.
        assert bus.unsubscribe_calls == []

    def test_start_with_explicit_subset(self, bus: FakeEventBus, manager: RecordingManager):
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start([EventType.ORDER_FILLED, EventType.SIGNAL_GENERATED])

        assert set(bridge.subscribed_event_types) == {
            EventType.ORDER_FILLED,
            EventType.SIGNAL_GENERATED,
        }

    def test_start_rejects_event_type_without_channel(
        self, bus: FakeEventBus, manager: RecordingManager
    ):
        bridge = EventBusBridge(bus=bus, manager=manager)
        # PORTFOLIO_UPDATED has no channel in the default mapping.
        with pytest.raises(ValueError, match="no channel configured"):
            bridge.start([EventType.ORDER_FILLED, EventType.PORTFOLIO_UPDATED])
        # Nothing was subscribed — validation runs before mutating state.
        assert bus.subscribe_calls == []
        assert bridge.subscribed_event_types == ()

    def test_stop_unsubscribes_every_attached_type(
        self, bus: FakeEventBus, manager: RecordingManager
    ):
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()
        bridge.stop()

        assert bus.unsubscribe_calls
        # Every subscribed type was unsubscribed with the same handler.
        subscribed_types = {et for et, _ in bus.subscribe_calls}
        unsubscribed_types = {et for et, _ in bus.unsubscribe_calls}
        assert subscribed_types == unsubscribed_types
        handlers = {h for _, h in bus.unsubscribe_calls}
        assert len(handlers) == 1
        assert bridge.subscribed_event_types == ()

    def test_stop_swallows_unsubscribe_errors(
        self, bus: FakeEventBus, manager: RecordingManager
    ):
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()
        # Poison unsubscribe after start — every call should raise.
        bus.unsubscribe = lambda et, h: (_ for _ in ()).throw(RuntimeError("nope"))
        # Must not raise.
        bridge.stop()
        assert bridge.subscribed_event_types == ()

    def test_stop_is_idempotent(self, bus: FakeEventBus, manager: RecordingManager):
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()
        bridge.stop()
        bridge.stop()  # second stop is a no-op
        assert bridge.subscribed_event_types == ()

    def test_channels_mapping_is_defensively_copied(
        self, bus: FakeEventBus, manager: RecordingManager
    ):
        custom = {EventType.ORDER_FILLED: "my_orders"}
        bridge = EventBusBridge(bus=bus, manager=manager, channels=custom)
        # Mutating the caller's dict after construction must not leak in.
        custom[EventType.SIGNAL_GENERATED] = "my_signals"
        assert bridge.channels == {EventType.ORDER_FILLED: "my_orders"}

    def test_channels_property_returns_copy(self, bridge: EventBusBridge):
        snapshot = bridge.channels
        snapshot[EventType.MARKET_OPEN] = "hax"
        # The bridge's internal mapping is unchanged.
        assert EventType.MARKET_OPEN not in bridge.channels


# ---------------------------------------------------------------------------
# Event forwarding (one test per subscribed event type)
# ---------------------------------------------------------------------------


def _event_payload(event_type: EventType, data: dict | None = None) -> dict:
    """Build a payload shaped like ``Event.to_dict()``."""
    return {
        "type": event_type.value,
        "data": data or {},
        "source": "test",
        "timestamp": datetime.now(UTC).isoformat(),
    }


class TestEventForwarding:
    """One forwarding test per subscribed event type, plus the no-op
    contract for unrouted types."""

    @pytest.mark.parametrize(
        ("event_type", "expected_channel"),
        [
            (EventType.ORDER_FILLED, "orders"),
            (EventType.ORDER_PARTIALLY_FILLED, "orders"),
            (EventType.ORDER_CANCELLED, "orders"),
            (EventType.SIGNAL_GENERATED, "signals"),
        ],
    )
    async def test_each_event_type_forwards_to_its_channel(
        self,
        bus: FakeEventBus,
        manager: RecordingManager,
        bridge: EventBusBridge,
        event_type: EventType,
        expected_channel: str,
    ) -> None:
        payload = _event_payload(event_type, {"order_id": "abc", "qty": 10})
        await bus.deliver(event_type, payload)

        assert len(manager.broadcasts) == 1
        channel, message = manager.broadcasts[0]
        assert channel == expected_channel
        # Envelope preserves the EventBus payload shape.
        assert message["type"] == "event"
        assert message["event_type"] == event_type.value
        assert message["channel"] == expected_channel
        assert message["data"] == {"order_id": "abc", "qty": 10}
        assert message["source"] == "test"
        assert message["timestamp"] == payload["timestamp"]

    async def test_signal_generated_forwards_to_signals_channel(
        self,
        bus: FakeEventBus,
        manager: RecordingManager,
        bridge: EventBusBridge,
    ) -> None:
        payload = _event_payload(
            EventType.SIGNAL_GENERATED,
            {"symbol": "AAPL", "side": "buy", "strategy_id": "mom-1"},
        )
        await bus.deliver(EventType.SIGNAL_GENERATED, payload)

        assert len(manager.broadcasts) == 1
        channel, message = manager.broadcasts[0]
        assert channel == "signals"
        assert message["data"]["symbol"] == "AAPL"

    async def test_each_event_serializes_to_valid_json(
        self,
        bus: FakeEventBus,
        manager: RecordingManager,
        bridge: EventBusBridge,
    ) -> None:
        # Every event in the default map must produce a JSON-encodable
        # envelope — this is the "serialize event payloads to JSON"
        # contract from the brief.
        for event_type in DEFAULT_EVENT_TYPES:
            await bus.deliver(event_type, _event_payload(event_type))

        assert len(manager.broadcasts) == len(DEFAULT_EVENT_TYPES)
        for _channel, message in manager.broadcasts:
            # Must not raise.
            json.dumps(message)


class TestNoOpEvents:
    """The brief calls out a test that no-op events are ignored."""

    async def test_unrouted_event_type_is_not_broadcast(
        self,
        bus: FakeEventBus,
        manager: RecordingManager,
        bridge: EventBusBridge,
    ) -> None:
        # The bridge is subscribed to a fixed set; delivering an event
        # the bridge did NOT subscribe to never even reaches the handler.
        # The stronger guarantee is that even an event *with a handler*
        # but no channel mapping is a no-op — cover both.
        await bus.deliver(
            EventType.MARKET_DATA_UPDATE,
            _event_payload(EventType.MARKET_DATA_UPDATE),
        )
        assert manager.broadcasts == []

    async def test_unrouted_event_reaching_handler_is_noop(
        self,
        bus: FakeEventBus,
        manager: RecordingManager,
    ) -> None:
        # If the bus delivers a payload whose type isn't in the channel
        # map (e.g. the bridge shares a handler with another subscriber,
        # or the bus mis-routes), the bridge must drop it silently.
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()
        # Invoke the handler directly with an unmapped type.
        await bridge._handle(
            _event_payload(EventType.PORTFOLIO_UPDATED, {"nav": 1.0})
        )
        assert manager.broadcasts == []

    async def test_payload_missing_type_is_noop(
        self,
        bus: FakeEventBus,
        manager: RecordingManager,
        bridge: EventBusBridge,
    ) -> None:
        await bridge._handle({"data": {"foo": "bar"}})
        assert manager.broadcasts == []

    async def test_non_dict_payload_is_noop(
        self,
        bus: FakeEventBus,
        manager: RecordingManager,
        bridge: EventBusBridge,
    ) -> None:
        # Defensive: a buggy producer that pushes a non-dict payload
        # must not crash the handler.
        await bridge._handle("not a dict")
        await bridge._handle(None)
        assert manager.broadcasts == []


class TestBroadcastErrorHandling:
    """A misbehaving ConnectionManager must not poison the dispatch loop."""

    async def test_broadcast_exception_is_swallowed(
        self,
        bus: FakeEventBus,
        manager: RecordingManager,
        bridge: EventBusBridge,
    ) -> None:
        manager.broadcast_exception = RuntimeError("manager down")
        # Must not raise.
        await bus.deliver(
            EventType.ORDER_FILLED,
            _event_payload(EventType.ORDER_FILLED, {"order_id": "x"}),
        )
        # No broadcast was recorded because the manager raised before
        # the append.
        assert manager.broadcasts == []


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


class _IntEnum(StrEnum):
    """Stand-in for an arbitrary StrEnum a producer might embed."""

    FOO = "foo"
    BAR = "bar"


class TestSerializeEvent:
    """``serialize_event`` must produce a JSON-safe envelope regardless
    of the producer-side value types."""

    def test_preserves_event_to_dict_shape(self):
        payload = {
            "type": "order.filled",
            "data": {"order_id": "o-1"},
            "source": "order_manager",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }
        envelope = serialize_event(payload)
        assert envelope["type"] == "event"
        assert envelope["event_type"] == "order.filled"
        assert envelope["channel"] is None  # bridge stamps this
        assert envelope["data"] == {"order_id": "o-1"}
        assert envelope["source"] == "order_manager"
        assert envelope["timestamp"] == "2024-01-01T00:00:00+00:00"

    def test_normalizes_non_json_native_values(self):
        u = uuid.uuid4()
        now = datetime.now(UTC)
        payload = {
            "type": EventType.ORDER_FILLED.value,
            "data": {
                "uuid": u,
                "datetime": now,
                "decimal": Decimal("3.14"),
                "enum": _IntEnum.FOO,
                "set": {1, 2, 3},
                "nested": {"a": [Decimal("1"), Decimal("2")]},
            },
            "source": "test",
            "timestamp": now.isoformat(),
        }
        envelope = serialize_event(payload)
        # The entire envelope must be JSON-serializable.
        json.dumps(envelope)
        # And specific coercions are deterministic.
        assert envelope["data"]["uuid"] == str(u)
        assert envelope["data"]["decimal"] == "3.14"
        assert envelope["data"]["enum"] == "foo"
        assert envelope["data"]["set"] == [1, 2, 3]
        assert envelope["data"]["nested"] == {"a": ["1", "2"]}

    def test_missing_fields_default_to_safe_values(self):
        envelope = serialize_event({})
        assert envelope == {
            "type": "event",
            "event_type": None,
            "channel": None,
            "data": {},
            "source": None,
            "timestamp": None,
        }
        # And is still JSON-safe.
        json.dumps(envelope)

    def test_accepts_event_type_key_as_alias_for_type(self):
        # Some payloads use ``event_type`` instead of ``type``. The
        # serializer must accept either.
        envelope = serialize_event({"event_type": "order.filled", "data": {}})
        assert envelope["event_type"] == "order.filled"
