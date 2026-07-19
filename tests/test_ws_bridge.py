"""Unit tests for the EventBus → ConnectionManager bridge (gh#7 follow-up).

This file covers two related but distinct bridges:

1. :class:`~engine.api.websocket.bridge.EventToWebSocketBridge` — the
   older, topic-based bridge tied to the legacy ``UserTopicManager``.
2. :class:`~engine.ws.bridge.EventBusBridge` — the focused, dependency-
   light order/signal → WebSocket bridge that is the subject of the
   coverage ramp (lines 95-118, 131-145, 163-165, 232-283, 305-306,
   344-387).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

import pytest

from engine.api.websocket.bridge import (
    EventToWebSocketBridge,
    extract_user_id,
    topic_for_event_type,
)
from engine.api.websocket.manager import Topic, UserTopicManager
from engine.events.bus import EventType
from engine.ws.bridge import (
    DEFAULT_EVENT_CHANNELS,
    DEFAULT_EVENT_TYPES,
    EventBusBridge,
    _extract_identity,
    _json_safe,
    serialize_event,
)

# ---------------------------------------------------------------------------
# topic_for_event_type
# ---------------------------------------------------------------------------


class TestTopicMapping:
    def test_order_prefix(self):
        assert topic_for_event_type("order.created") == Topic.ORDER
        assert topic_for_event_type("order.filled") == Topic.ORDER

    def test_portfolio_prefix(self):
        assert topic_for_event_type("portfolio.updated") == Topic.PORTFOLIO

    def test_backtest_prefix(self):
        assert topic_for_event_type("backtest.completed") == Topic.BACKTEST

    def test_alert_prefix(self):
        assert topic_for_event_type("alert.triggered") == Topic.ALERT

    def test_unknown_prefix_returns_none(self):
        assert topic_for_event_type("system.heartbeat") is None
        assert topic_for_event_type("") is None


# ---------------------------------------------------------------------------
# extract_user_id
# ---------------------------------------------------------------------------


class TestExtractUserId:
    def test_snake_case(self):
        u = uuid.uuid4()
        assert extract_user_id({"user_id": str(u)}) == u

    def test_camel_case(self):
        u = uuid.uuid4()
        assert extract_user_id({"userId": str(u)}) == u

    def test_uuid_object_passes_through(self):
        u = uuid.uuid4()
        assert extract_user_id({"user_id": u}) == u

    def test_missing_returns_none(self):
        assert extract_user_id({}) is None
        assert extract_user_id(None) is None

    def test_unparseable_returns_none(self):
        assert extract_user_id({"user_id": "not-a-uuid"}) is None
        assert extract_user_id({"user_id": 123}) is None


# ---------------------------------------------------------------------------
# Bridge integration with a fake bus
# ---------------------------------------------------------------------------


class _FakeBus:
    """Stand-in for the real EventBus.

    Only models the subscribe / unsubscribe surface the bridge uses.
    Tests can call ``deliver`` to simulate the bus delivering a payload.
    """

    def __init__(self) -> None:
        self._handlers: dict = {}

    def subscribe(self, event_type, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler) -> None:
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h is not handler
            ]

    async def deliver(self, event_type, payload) -> None:
        for h in self._handlers.get(event_type, []):
            await h(payload)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.id = uuid.uuid4()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeWS) and other.id == self.id


@pytest.fixture
async def setup():
    bus = _FakeBus()
    manager = UserTopicManager()
    bridge = EventToWebSocketBridge(bus=bus, manager=manager)
    bridge.attach(["order.filled", "portfolio.updated"])
    return bus, manager, bridge


class TestBridge:
    async def test_delivers_to_subscribed_user(self, setup):
        bus, manager, _bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        await bus.deliver(
            "order.filled",
            {
                "event_type": "order.filled",
                "data": {"user_id": str(user_id), "qty": 10},
            },
        )
        assert len(ws.sent) == 1
        assert ws.sent[0]["topic"] == "order"
        assert ws.sent[0]["data"]["event_type"] == "order.filled"

    async def test_drops_event_without_user_id(self, setup):
        bus, manager, _bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        await bus.deliver(
            "order.filled",
            {"event_type": "order.filled", "data": {"qty": 10}},
        )
        assert ws.sent == []

    async def test_drops_unrouted_event(self, setup):
        bus, manager, _bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        # Bridge is attached only to order.filled / portfolio.updated;
        # delivering a different type via the bus does nothing.
        await bus.deliver(
            "system.heartbeat",
            {"event_type": "system.heartbeat", "data": {"user_id": str(user_id)}},
        )
        assert ws.sent == []

    async def test_only_subscribed_topic_receives(self, setup):
        bus, manager, _bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        # Subscribed to portfolio, not order.
        await manager.subscribe(user_id, ws, ["portfolio"])

        await bus.deliver(
            "order.filled",
            {"event_type": "order.filled", "data": {"user_id": str(user_id)}},
        )
        assert ws.sent == []

    async def test_detach_unsubscribes(self, setup):
        bus, manager, bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        bridge.detach()
        await bus.deliver(
            "order.filled",
            {"event_type": "order.filled", "data": {"user_id": str(user_id)}},
        )
        assert ws.sent == []


# ===========================================================================
# Tests for the EventBusBridge in engine.ws.bridge.
#
# The focused order/trade/signal → ConnectionManager bridge. Distinct from
# the EventToWebSocketBridge above: it stamps identity on every envelope,
# routes user-scoped events to a ``user:<id>`` room, and is duck-typed
# against a minimal bus/manager surface so these tests use light fakes.
# ===========================================================================


class _RecordingBus:
    """Fake EventBus — records subscribe / unsubscribe calls.

    Mirrors the duck-typed surface ``EventBusBridge`` actually uses:
    ``subscribe(event_type, handler)`` / ``unsubscribe(event_type, handler)``.
    Supports injecting per-event-type errors on unsubscribe to exercise the
    bridge's swallow-and-continue teardown path.
    """

    def __init__(self) -> None:
        self.subs: dict[EventType, list] = {}
        self.unsubscribe_errors: dict[EventType, Exception] = {}
        self.subscribe_calls: list[tuple[EventType, object]] = []
        self.unsubscribe_calls: list[tuple[EventType, object]] = []

    def subscribe(self, event_type: EventType, handler: object) -> None:
        self.subscribe_calls.append((event_type, handler))
        self.subs.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: EventType, handler: object) -> None:
        self.unsubscribe_calls.append((event_type, handler))
        if event_type in self.unsubscribe_errors:
            raise self.unsubscribe_errors[event_type]
        if event_type in self.subs:
            self.subs[event_type] = [h for h in self.subs[event_type] if h is not handler]

    def handler_count(self, event_type: EventType) -> int:
        return len(self.subs.get(event_type, []))


class _RecordingManager:
    """Fake ConnectionManager — records every broadcast call.

    The bridge only requires ``await manager.broadcast(room, message) -> int``;
    we record every invocation so tests can assert on room routing and
    envelope shape. ``raise_on`` exercises the bridge's broadcast-failure
    swallow path (lines 378-385).
    """

    def __init__(self, *, raise_on: Exception | None = None, recipients: int = 1) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._raise_on = raise_on
        self._recipients = recipients

    async def broadcast(self, room: str, message: dict) -> int:
        if self._raise_on is not None:
            raise self._raise_on
        self.calls.append((room, message))
        return self._recipients


@pytest.fixture
def ws_bus() -> _RecordingBus:
    return _RecordingBus()


@pytest.fixture
def ws_manager() -> _RecordingManager:
    return _RecordingManager()


@pytest.fixture
def ws_bridge(ws_bus: _RecordingBus, ws_manager: _RecordingManager) -> EventBusBridge:
    return EventBusBridge(bus=ws_bus, manager=ws_manager)


# ---------------------------------------------------------------------------
# Module-level constants & helpers
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_event_channels_maps_expected_types(self):
        assert DEFAULT_EVENT_CHANNELS[EventType.ORDER_FILLED] == "orders"
        assert DEFAULT_EVENT_CHANNELS[EventType.ORDER_PARTIALLY_FILLED] == "orders"
        assert DEFAULT_EVENT_CHANNELS[EventType.ORDER_CANCELLED] == "orders"
        assert DEFAULT_EVENT_CHANNELS[EventType.SIGNAL_GENERATED] == "signals"

    def test_default_event_types_is_tuple_and_matches_channels(self):
        assert isinstance(DEFAULT_EVENT_TYPES, tuple)
        assert set(DEFAULT_EVENT_TYPES) == set(DEFAULT_EVENT_CHANNELS.keys())

    def test_default_event_types_is_immutable(self):
        # Tuple can't be mutated; ensure callers can't append to grow the
        # bridge's defaults accidentally.
        with pytest.raises(AttributeError):
            DEFAULT_EVENT_TYPES.append(EventType.MARKET_OPEN)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _json_safe
# ---------------------------------------------------------------------------


class _NonJsonNative:
    """Stand-in for datetimes / UUIDs / Decimals / Pydantic models.

    Not JSON-native (json.dumps raises TypeError), exercises the
    catch-all ``default=str`` branch of ``_json_safe``.
    """

    def __str__(self) -> str:
        return "<non-json-native>"


class TestJsonSafe:
    def test_primitives_pass_through(self):
        assert _json_safe("foo") == "foo"
        assert _json_safe(42) == 42
        assert _json_safe(3.14) == 3.14
        assert _json_safe(True) is True
        assert _json_safe(False) is False
        assert _json_safe(None) is None

    def test_dict_is_recursed_and_keys_stringified(self):
        out = _json_safe({"a": 1, 2: "b"})
        assert out == {"a": 1, "2": "b"}

    def test_nested_dict_is_recursed(self):
        out = _json_safe({"outer": {"inner": [1, 2]}})
        assert out == {"outer": {"inner": [1, 2]}}

    def test_list_passes_through_recursed(self):
        assert _json_safe([1, "a", None]) == [1, "a", None]

    def test_tuple_becomes_list(self):
        # JSON has no tuple type; the bridge normalizes to list.
        out = _json_safe((1, 2, 3))
        assert out == [1, 2, 3]
        assert isinstance(out, list)

    def test_set_is_sorted_for_determinism(self):
        out = _json_safe({3, 1, 2})
        assert out == [1, 2, 3]
        assert isinstance(out, list)

    def test_set_with_unsortable_mixed_types_falls_back_to_iteration(self):
        # Mixed-type sets raise TypeError on sorted(); the bridge must
        # fall back to iterating rather than crashing the broadcast path.
        out = _json_safe({1, "a", 2})
        assert isinstance(out, list)
        assert set(out) == {1, "a", 2}

    def test_datetime_is_stringified(self):
        dt = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
        out = _json_safe(dt)
        assert isinstance(out, str)
        assert "2025" in out

    def test_uuid_is_stringified(self):
        u = uuid.uuid4()
        out = _json_safe(u)
        assert isinstance(out, str)
        assert out == str(u)

    def test_decimal_is_stringified(self):
        out = _json_safe(Decimal("1.5"))
        assert isinstance(out, str)
        assert out == "1.5"

    def test_custom_object_is_stringified(self):
        obj = _NonJsonNative()
        out = _json_safe(obj)
        assert out == "<non-json-native>"

    def test_plain_enum_is_stringified(self):
        # Plain (non-StrEnum) members aren't JSON-native.
        class Color(Enum):
            RED = 1

        out = _json_safe(Color.RED)
        assert isinstance(out, str)

    def test_nested_non_json_native_in_dict(self):
        u = uuid.uuid4()
        out = _json_safe({"id": u, "items": [1, u]})
        assert out == {"id": str(u), "items": [1, str(u)]}

    def test_bytes_are_stringified(self):
        # bytes raise TypeError under json.dumps → fall through to str().
        out = _json_safe(b"hello")
        assert isinstance(out, str)

    def test_json_dumps_success_path_passes_value_through(self, monkeypatch):
        # Line 118's ``else: return value`` branch is defensive: it covers
        # the (rare) case where a non-primitive, non-container value is
        # nonetheless accepted by ``json.dumps`` — e.g. an object registered
        # with a project-wide custom JSONEncoder. In standard Python this
        # is essentially unreachable, so we verify the intent by forcing the
        # ``json.dumps`` success path.
        import engine.ws.bridge as bridge_mod

        class Custom:
            pass

        monkeypatch.setattr(bridge_mod.json, "dumps", lambda v: "ok")
        obj = Custom()
        # Must return the value unchanged (the else branch).
        assert _json_safe(obj) is obj


# ---------------------------------------------------------------------------
# _extract_identity
# ---------------------------------------------------------------------------


class TestExtractIdentity:
    def test_non_dict_returns_none_none(self):
        assert _extract_identity(None) == (None, None)
        assert _extract_identity("string") == (None, None)
        assert _extract_identity(42) == (None, None)
        assert _extract_identity(["user_id", "x"]) == (None, None)

    def test_empty_dict_returns_none_none(self):
        assert _extract_identity({}) == (None, None)

    def test_snake_case_user_id(self):
        assert _extract_identity({"user_id": "abc"}) == ("abc", None)

    def test_camel_case_user_id(self):
        assert _extract_identity({"userId": "abc"}) == ("abc", None)

    def test_snake_case_tenant_id(self):
        assert _extract_identity({"tenant_id": "t1"}) == (None, "t1")

    def test_camel_case_tenant_id(self):
        assert _extract_identity({"tenantId": "t1"}) == (None, "t1")

    def test_both_identity_fields(self):
        assert _extract_identity({"user_id": "u1", "tenant_id": "t1"}) == ("u1", "t1")

    def test_int_user_id_is_stringified(self):
        assert _extract_identity({"user_id": 123}) == ("123", None)

    def test_float_user_id_is_stringified(self):
        assert _extract_identity({"user_id": 1.5}) == ("1.5", None)

    def test_empty_string_user_id_resolves_to_none(self):
        # Empty string is truthy-checked after str() coercion.
        assert _extract_identity({"user_id": ""}) == (None, None)

    def test_none_user_id_resolves_to_none(self):
        assert _extract_identity({"user_id": None}) == (None, None)

    def test_uuid_object_user_id_resolves_to_none(self):
        # UUID objects are NOT in the (str, int, float) allowlist — the
        # bridge only accepts JSON-native primitives for the room name.
        u = uuid.uuid4()
        assert _extract_identity({"user_id": u}) == (None, None)

    def test_list_user_id_resolves_to_none(self):
        assert _extract_identity({"user_id": ["a", "b"]}) == (None, None)

    def test_dict_user_id_resolves_to_none(self):
        assert _extract_identity({"user_id": {"k": "v"}}) == (None, None)

    def test_camelcase_overrides_when_snake_absent(self):
        # snake_case takes priority; when absent, camelCase is consulted.
        assert _extract_identity({"userId": "u1", "user_id": "u2"}) == ("u2", None)

    def test_empty_string_tenant_resolves_to_none(self):
        assert _extract_identity({"tenant_id": ""}) == (None, None)


# ---------------------------------------------------------------------------
# serialize_event
# ---------------------------------------------------------------------------


class TestSerializeEvent:
    def test_full_payload_stamps_identity_and_envelope(self):
        u = uuid.uuid4()
        payload = {
            "type": "order.filled",
            "data": {"user_id": str(u), "qty": 10},
            "source": "backtest",
            "timestamp": "2025-01-02T03:04:05Z",
        }
        out = serialize_event(payload)
        assert out["type"] == "event"
        assert out["event_type"] == "order.filled"
        assert out["channel"] is None  # filled by the bridge after serialize
        assert out["user_id"] == str(u)
        assert out["tenant_id"] is None
        assert out["data"] == {"user_id": str(u), "qty": 10}
        assert out["source"] == "backtest"
        assert out["timestamp"] == "2025-01-02T03:04:05Z"

    def test_missing_data_defaults_to_empty_dict(self):
        out = serialize_event({"type": "order.filled"})
        assert out["data"] == {}
        assert out["user_id"] is None
        assert out["tenant_id"] is None

    def test_none_data_defaults_to_empty_dict(self):
        out = serialize_event({"type": "order.filled", "data": None})
        assert out["data"] == {}

    def test_event_type_key_is_used_when_type_absent(self):
        out = serialize_event({"event_type": "signal.generated", "data": {}})
        assert out["event_type"] == "signal.generated"

    def test_type_takes_priority_over_event_type(self):
        out = serialize_event(
            {"type": "order.filled", "event_type": "order.cancelled", "data": {}}
        )
        assert out["event_type"] == "order.filled"

    def test_envelope_is_json_serializable_with_non_native_values(self):
        # The whole point of serialize_event: produce a json.dumps-able
        # envelope no matter what the producer stuffed into ``data``.
        import json

        u = uuid.uuid4()
        dt = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
        payload = {
            "type": "order.filled",
            "data": {"id": u, "ts": dt, "qty": Decimal("1.5"), "tags": {"a", "b"}},
            "source": "engine",
            "timestamp": dt,
        }
        out = serialize_event(payload)
        # Must not raise.
        json.dumps(out)

    def test_missing_source_and_timestamp_are_none(self):
        out = serialize_event({"type": "order.filled", "data": {}})
        assert out["source"] is None
        assert out["timestamp"] is None

    def test_camel_case_identity_in_data(self):
        out = serialize_event({"type": "order.filled", "data": {"userId": "abc"}})
        assert out["user_id"] == "abc"


# ---------------------------------------------------------------------------
# EventBusBridge.__init__ & introspection
# ---------------------------------------------------------------------------


class TestBridgeInit:
    def test_default_channels_used_when_none(self, ws_bus, ws_manager):
        bridge = EventBusBridge(bus=ws_bus, manager=ws_manager)
        assert bridge.channels == DEFAULT_EVENT_CHANNELS

    def test_channels_property_returns_defensive_copy(self, ws_bridge):
        # Mutating the returned dict must not affect the bridge's state.
        snapshot = ws_bridge.channels
        snapshot[EventType.MARKET_OPEN] = "market"
        assert EventType.MARKET_OPEN not in ws_bridge.channels

    def test_custom_channels_are_copied_not_referenced(self, ws_bus, ws_manager):
        custom = {EventType.ORDER_FILLED: "orders_v2"}
        bridge = EventBusBridge(bus=ws_bus, manager=ws_manager, channels=custom)
        assert bridge.channels == {EventType.ORDER_FILLED: "orders_v2"}
        # Mutating the caller's dict after construction must not re-route.
        custom[EventType.MARKET_OPEN] = "market"
        assert EventType.MARKET_OPEN not in bridge.channels

    def test_subscribed_event_types_starts_empty(self, ws_bridge):
        assert ws_bridge.subscribed_event_types == ()

    def test_handler_attribute_is_stable_bound_method(self, ws_bridge):
        # The bridge caches the bound handler so subscribe/unsubscribe
        # reference the same object — bus identity bookkeeping depends on it.
        assert ws_bridge._handler is ws_bridge._handler


class TestChannelFor:
    def test_event_type_member_resolves(self, ws_bridge):
        assert ws_bridge.channel_for(EventType.ORDER_FILLED) == "orders"
        assert ws_bridge.channel_for(EventType.SIGNAL_GENERATED) == "signals"

    def test_string_value_resolves(self, ws_bridge):
        # EventType is a StrEnum — emitting raw strings is supported.
        assert ws_bridge.channel_for("order.filled") == "orders"
        assert ws_bridge.channel_for("signal.generated") == "signals"

    def test_unknown_string_returns_none(self, ws_bridge):
        assert ws_bridge.channel_for("not.a.real.event") is None

    def test_empty_string_returns_none(self, ws_bridge):
        assert ws_bridge.channel_for("") is None

    def test_event_type_not_in_map_returns_none(self, ws_bridge):
        assert ws_bridge.channel_for(EventType.MARKET_OPEN) is None

    def test_non_string_non_event_type_returns_none(self, ws_bridge):
        # Anything that's neither an EventType nor a str short-circuits.
        # All inputs here are hashable (the bridge's dict membership check
        # would raise TypeError on an unhashable type, which is outside
        # the documented ``EventType | str`` contract).
        assert ws_bridge.channel_for(123) is None
        assert ws_bridge.channel_for(None) is None
        assert ws_bridge.channel_for(("order.filled",)) is None
        assert ws_bridge.channel_for(True) is None

    def test_custom_channels_override_defaults(self, ws_bus, ws_manager):
        custom = {EventType.MARKET_OPEN: "market"}
        bridge = EventBusBridge(bus=ws_bus, manager=ws_manager, channels=custom)
        assert bridge.channel_for(EventType.MARKET_OPEN) == "market"
        # And the default-routed types are no longer routed.
        assert bridge.channel_for(EventType.ORDER_FILLED) is None


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


class TestBridgeStart:
    def test_start_with_no_args_subscribes_to_all_channels(self, ws_bridge, ws_bus):
        ws_bridge.start()
        assert set(ws_bridge.subscribed_event_types) == set(DEFAULT_EVENT_CHANNELS.keys())
        for et in DEFAULT_EVENT_CHANNELS:
            assert ws_bus.handler_count(et) == 1

    def test_start_with_explicit_subset(self, ws_bridge, ws_bus):
        ws_bridge.start([EventType.ORDER_FILLED, EventType.SIGNAL_GENERATED])
        assert set(ws_bridge.subscribed_event_types) == {
            EventType.ORDER_FILLED,
            EventType.SIGNAL_GENERATED,
        }
        # Unrequested types were NOT subscribed.
        assert ws_bus.handler_count(EventType.ORDER_CANCELLED) == 0

    def test_start_with_empty_list_subscribes_nothing(self, ws_bridge, ws_bus):
        ws_bridge.start([])
        assert ws_bridge.subscribed_event_types == ()
        assert ws_bus.subscribe_calls == []

    def test_start_raises_on_unconfigured_event_type(self, ws_bridge, ws_bus):
        # MARKET_OPEN has no channel — misconfiguration must be loud.
        with pytest.raises(ValueError, match="no channel configured"):
            ws_bridge.start([EventType.MARKET_OPEN])
        # No state mutated on validation failure.
        assert ws_bridge.subscribed_event_types == ()
        assert ws_bus.subscribe_calls == []

    def test_start_raises_when_any_in_mixed_list_unconfigured(self, ws_bridge):
        with pytest.raises(ValueError, match=r"market\.open"):
            ws_bridge.start([EventType.ORDER_FILLED, EventType.MARKET_OPEN])

    def test_start_is_not_idempotent_double_subscribe(self, ws_bridge, ws_bus):
        # Calling start twice double-subscribes — documented behavior.
        ws_bridge.start([EventType.ORDER_FILLED])
        ws_bridge.start([EventType.ORDER_FILLED])
        assert ws_bus.handler_count(EventType.ORDER_FILLED) == 2
        # _registered now lists it twice too.
        assert ws_bridge.subscribed_event_types.count(EventType.ORDER_FILLED) == 2

    def test_start_passes_same_bound_handler_to_bus(self, ws_bridge, ws_bus):
        ws_bridge.start([EventType.ORDER_FILLED])
        _et, handler = ws_bus.subscribe_calls[-1]
        assert handler is ws_bridge._handler


class TestBridgeStop:
    def test_stop_unsubscribes_all_registered(self, ws_bridge, ws_bus):
        ws_bridge.start()
        assert ws_bus.unsubscribe_calls == []
        ws_bridge.stop()
        assert len(ws_bus.unsubscribe_calls) == len(DEFAULT_EVENT_CHANNELS)
        assert ws_bridge.subscribed_event_types == ()
        for et in DEFAULT_EVENT_CHANNELS:
            assert ws_bus.handler_count(et) == 0

    def test_stop_after_start_subset_only_touches_those(self, ws_bridge, ws_bus):
        ws_bridge.start([EventType.ORDER_FILLED])
        ws_bridge.stop()
        assert len(ws_bus.unsubscribe_calls) == 1
        assert ws_bus.unsubscribe_calls[0][0] == EventType.ORDER_FILLED

    def test_stop_when_never_started_is_noop(self, ws_bridge, ws_bus):
        ws_bridge.stop()
        assert ws_bus.unsubscribe_calls == []
        assert ws_bridge.subscribed_event_types == ()

    def test_stop_swallows_unsubscribe_errors_and_continues(self, ws_bus, ws_manager):
        # One event type's unsubscribe blows up; the bridge must still
        # attempt the rest and clear _registered.
        ws_bus.unsubscribe_errors[EventType.ORDER_FILLED] = RuntimeError("boom")
        bridge = EventBusBridge(bus=ws_bus, manager=ws_manager)
        bridge.start()
        # Should not raise despite one failing unsubscribe.
        bridge.stop()
        # Every registered type was attempted (including the failing one).
        assert {et for et, _h in ws_bus.unsubscribe_calls} == set(DEFAULT_EVENT_CHANNELS)
        assert bridge.subscribed_event_types == ()

    def test_stop_passes_same_handler_to_unsubscribe(self, ws_bridge, ws_bus):
        ws_bridge.start([EventType.ORDER_FILLED])
        ws_bridge.stop()
        _et, handler = ws_bus.unsubscribe_calls[-1]
        assert handler is ws_bridge._handler

    def test_start_stop_start_cycle_resubscribes(self, ws_bridge, ws_bus):
        ws_bridge.start([EventType.ORDER_FILLED])
        ws_bridge.stop()
        ws_bridge.start([EventType.ORDER_FILLED])
        assert ws_bridge.subscribed_event_types == (EventType.ORDER_FILLED,)
        assert ws_bus.handler_count(EventType.ORDER_FILLED) == 1


# ---------------------------------------------------------------------------
# _handle (event forwarding & routing)
# ---------------------------------------------------------------------------


class TestBridgeHandle:
    async def test_forwards_event_to_manager_broadcast(self, ws_bridge, ws_manager):
        ws_bridge.start([EventType.ORDER_FILLED])
        payload = {
            "type": "order.filled",
            "data": {"qty": 10},
            "source": "engine",
            "timestamp": "2025-01-02T03:04:05Z",
        }
        await ws_bridge._handle(payload)
        assert len(ws_manager.calls) == 1
        room, envelope = ws_manager.calls[0]
        # No user_id → broadcast to the flat channel.
        assert room == "orders"
        assert envelope["channel"] == "orders"
        assert envelope["event_type"] == "order.filled"
        assert envelope["data"] == {"qty": 10}

    async def test_user_scoped_event_routes_to_user_room(self, ws_bridge, ws_manager):
        ws_bridge.start([EventType.ORDER_FILLED])
        payload = {
            "type": "order.filled",
            "data": {"user_id": "user-123", "qty": 10},
        }
        await ws_bridge._handle(payload)
        room, envelope = ws_manager.calls[0]
        assert room == "user:user-123"
        assert envelope["user_id"] == "user-123"

    async def test_camel_case_user_id_routes_to_user_room(self, ws_bridge, ws_manager):
        ws_bridge.start([EventType.ORDER_FILLED])
        payload = {"type": "order.filled", "data": {"userId": "abc"}}
        await ws_bridge._handle(payload)
        assert ws_manager.calls[0][0] == "user:abc"

    async def test_signal_event_routes_to_signals_channel(self, ws_bridge, ws_manager):
        ws_bridge.start([EventType.SIGNAL_GENERATED])
        await ws_bridge._handle({"type": "signal.generated", "data": {}})
        assert ws_manager.calls[0][0] == "signals"

    async def test_non_dict_payload_is_noop(self, ws_bridge, ws_manager):
        ws_bridge.start()
        await ws_bridge._handle("not-a-dict")  # type: ignore[arg-type]
        await ws_bridge._handle(None)  # type: ignore[arg-type]
        await ws_bridge._handle(["type", "x"])  # type: ignore[arg-type]
        assert ws_manager.calls == []

    async def test_payload_without_type_is_noop(self, ws_bridge, ws_manager):
        ws_bridge.start()
        await ws_bridge._handle({"data": {"user_id": "abc"}})
        assert ws_manager.calls == []

    async def test_payload_with_empty_type_is_noop(self, ws_bridge, ws_manager):
        ws_bridge.start()
        await ws_bridge._handle({"type": "", "data": {}})
        await ws_bridge._handle({"type": None, "data": {}})
        assert ws_manager.calls == []

    async def test_unrouted_event_type_is_noop(self, ws_bridge, ws_manager):
        # Bridge is started for all defaults, but MARKET_OPEN isn't routed.
        ws_bridge.start()
        await ws_bridge._handle({"type": "market.open", "data": {"user_id": "abc"}})
        assert ws_manager.calls == []

    async def test_unknown_string_event_type_is_noop(self, ws_bridge, ws_manager):
        ws_bridge.start()
        await ws_bridge._handle({"type": "totally.unknown", "data": {}})
        assert ws_manager.calls == []

    async def test_broadcast_exception_is_swallowed(self, ws_bus):
        # A misbehaving manager must not poison the EventBus dispatch loop.
        failing = _RecordingManager(raise_on=RuntimeError("ws dead"))
        bridge = EventBusBridge(bus=ws_bus, manager=failing)
        bridge.start([EventType.ORDER_FILLED])
        # Must not raise.
        await bridge._handle({"type": "order.filled", "data": {}})
        assert failing.calls == []

    async def test_envelope_carries_stamped_identity_for_consumer(self, ws_bridge, ws_manager):
        ws_bridge.start([EventType.ORDER_FILLED])
        await ws_bridge._handle(
            {
                "type": "order.filled",
                "data": {"user_id": "u1", "tenant_id": "t1", "qty": 1},
            }
        )
        _room, envelope = ws_manager.calls[0]
        assert envelope["user_id"] == "u1"
        assert envelope["tenant_id"] == "t1"

    async def test_envelope_serializes_non_native_payload_values(
        self, ws_bridge, ws_manager
    ):
        import json

        ws_bridge.start([EventType.ORDER_FILLED])
        await ws_bridge._handle(
            {
                "type": "order.filled",
                "data": {
                    "id": uuid.uuid4(),
                    "ts": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
                    "amount": Decimal("1.5"),
                },
            }
        )
        _room, envelope = ws_manager.calls[0]
        # Must be JSON-safe — the manager hands this straight to ws clients.
        json.dumps(envelope)

    async def test_event_type_key_works_when_type_absent(self, ws_bridge, ws_manager):
        ws_bridge.start([EventType.ORDER_FILLED])
        await ws_bridge._handle({"event_type": "order.filled", "data": {}})
        assert len(ws_manager.calls) == 1

    async def test_recipients_returned_by_manager_ignored_gracefully(self, ws_bus):
        # The bridge logs recipients but doesn't depend on the int value;
        # zero / large values must not change control flow.
        mgr = _RecordingManager(recipients=0)
        bridge = EventBusBridge(bus=ws_bus, manager=mgr)
        bridge.start([EventType.ORDER_FILLED])
        await bridge._handle({"type": "order.filled", "data": {}})
        assert len(mgr.calls) == 1


# ---------------------------------------------------------------------------
# End-to-end: bus delivery → bridge → manager (integration sanity)
# ---------------------------------------------------------------------------


class TestBridgeEndToEnd:
    async def test_bus_delivery_reaches_manager_via_subscribed_handler(
        self, ws_bus, ws_manager
    ):
        bridge = EventBusBridge(bus=ws_bus, manager=ws_manager)
        bridge.start()

        # Simulate the real EventBus invoking the registered handler.
        handler = ws_bus.subs[EventType.ORDER_FILLED][0]
        await handler(
            {
                "type": "order.filled",
                "data": {"user_id": "u1", "qty": 5},
                "source": "engine",
                "timestamp": "2025-01-02T03:04:05Z",
            }
        )
        assert ws_manager.calls[0][0] == "user:u1"

    async def test_concurrent_events_all_forwarded(self, ws_bus, ws_manager):
        # Many events fired in parallel must all reach the manager — the
        # handler is awaitable and the bridge holds no global lock.
        bridge = EventBusBridge(bus=ws_bus, manager=ws_manager)
        bridge.start()
        handler = ws_bus.subs[EventType.ORDER_FILLED][0]

        await asyncio.gather(
            *(
                handler({"type": "order.filled", "data": {"user_id": f"u{i}"}})
                for i in range(50)
            )
        )
        assert len(ws_manager.calls) == 50
        rooms = {room for room, _env in ws_manager.calls}
        assert rooms == {f"user:u{i}" for i in range(50)}

    async def test_concurrent_mixed_event_types(self, ws_bus, ws_manager):
        bridge = EventBusBridge(bus=ws_bus, manager=ws_manager)
        bridge.start()
        order_handler = ws_bus.subs[EventType.ORDER_FILLED][0]
        signal_handler = ws_bus.subs[EventType.SIGNAL_GENERATED][0]

        await asyncio.gather(
            order_handler({"type": "order.filled", "data": {"user_id": "u1"}}),
            signal_handler({"type": "signal.generated", "data": {}}),
            order_handler({"type": "order.filled", "data": {}}),
        )
        rooms = [room for room, _env in ws_manager.calls]
        assert "user:u1" in rooms
        assert "signals" in rooms
        assert "orders" in rooms

    async def test_concurrent_broadcast_failures_do_not_raise(self, ws_bus):
        # Every broadcast fails; none should propagate out of the handler,
        # so the EventBus dispatch loop keeps draining.
        mgr = _RecordingManager(raise_on=RuntimeError("dead"))
        bridge = EventBusBridge(bus=ws_bus, manager=mgr)
        bridge.start()
        handler = ws_bus.subs[EventType.ORDER_FILLED][0]

        await asyncio.gather(
            *(handler({"type": "order.filled", "data": {}}) for _ in range(20))
        )
        assert mgr.calls == []

    async def test_full_lifecycle_start_handle_stop(self, ws_bus, ws_manager):
        bridge = EventBusBridge(bus=ws_bus, manager=ws_manager)
        bridge.start()
        handler = ws_bus.subs[EventType.ORDER_CANCELLED][0]
        await handler({"type": "order.cancelled", "data": {"user_id": "u9"}})
        bridge.stop()

        assert ws_manager.calls[0][0] == "user:u9"
        # After stop, the bus has no handlers registered.
        assert ws_bus.handler_count(EventType.ORDER_CANCELLED) == 0
        assert bridge.subscribed_event_types == ()
