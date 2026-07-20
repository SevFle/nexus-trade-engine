"""Unit tests for ``engine.ws.bridge`` — the focused EventBus →
WebSocket bridge for order, trade, and signal events.

These tests deliberately use light fakes for both the EventBus and the
ConnectionManager. The bridge is duck-typed against their interfaces
(``subscribe`` / ``unsubscribe`` on the bus, async ``broadcast`` on the
manager), so the fakes only need to model the surface the bridge
touches — no Redis, no FastAPI, no Pydantic, no real WebSocket.

Coverage targets the previously-uncovered branches:
``_json_safe`` (all type arms + the json.dumps catch-all), the
identity extractor, ``serialize_event`` envelope building, the
``channel_for`` resolver, the ``start``/``stop`` lifecycle (including
the loud-fail ValueError and the exception-swallowing teardown), and
the full ``_handle`` dispatch matrix (non-dict payload, missing type,
unrouted event, identity-aware room routing, and broadcast failure).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from engine.events.bus import EventType
from engine.ws import bridge as bridge_mod
from engine.ws.bridge import (
    DEFAULT_EVENT_CHANNELS,
    DEFAULT_EVENT_TYPES,
    EventBusBridge,
    _extract_identity,
    _json_safe,
    serialize_event,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBus:
    """Minimal stand-in for :class:`~engine.events.bus.EventBus`.

    Records every (event_type, handler) pair the bridge subscribes and
    lets a test deliver a payload by calling :meth:`deliver`. The
    ``unsubscribe`` bookkeeping is identity-based, mirroring the real
    bus, so a bridge that re-uses its cached bound method round-trips
    cleanly.

    ``unsubscribe`` can be configured to raise (via
    :meth:`fail_on_unsubscribe`) to exercise the bridge's
    exception-swallowing teardown path.
    """

    def __init__(self) -> None:
        self.subscribed: list[tuple[Any, Any]] = []
        self.unsubscribed: list[tuple[Any, Any]] = []
        self._handlers: dict[Any, list] = {}
        self._unsubscribe_error: Exception | None = None

    def subscribe(self, event_type: Any, handler: Any) -> None:
        self.subscribed.append((event_type, handler))
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: Any, handler: Any) -> None:
        self.unsubscribed.append((event_type, handler))
        if self._unsubscribe_error is not None:
            raise self._unsubscribe_error
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h is not handler
            ]

    def fail_on_unsubscribe(self, exc: Exception) -> None:
        self._unsubscribe_error = exc

    async def deliver(self, event_type: Any, payload: Any) -> None:
        for handler in list(self._handlers.get(event_type, [])):
            await handler(payload)


class _FakeManager:
    """Records every ``broadcast`` call so a test can assert on it.

    Returns a configurable recipient count from ``broadcast`` so the
    bridge's debug log line gets a realistic value. Raising on broadcast
    is also supported to exercise the error-swallowing path.
    """

    def __init__(self, *, recipients: int = 1) -> None:
        self.broadcasts: list[tuple[str, dict[str, Any]]] = []
        self._recipients = recipients
        self._broadcast_error: Exception | None = None

    def fail_on_broadcast(self, exc: Exception) -> None:
        self._broadcast_error = exc

    async def broadcast(self, room: str, message: dict[str, Any]) -> int:
        self.broadcasts.append((room, message))
        if self._broadcast_error is not None:
            raise self._broadcast_error
        return self._recipients


@pytest.fixture
def bridge() -> EventBusBridge:
    """A started bridge wired into the fakes.

    Tests that exercise ``start`` / ``stop`` lifecycle directly should
    build their own bridge rather than reuse this fixture.
    """
    bus = _FakeBus()
    manager = _FakeManager()
    b = EventBusBridge(bus=bus, manager=manager)
    b.start()
    b._bus = bus  # type: ignore[attr-defined]  # expose for tests
    b._manager = manager  # type: ignore[attr-defined]
    return b


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestDefaults:
    """The brief lists ORDER_FILLED, ORDER_PARTIALLY_FILLED,
    ORDER_CANCELLED, and SIGNAL_GENERATED as the subscribed event types.
    Pin each one's presence and channel."""

    def test_default_event_channels_membership(self):
        assert DEFAULT_EVENT_CHANNELS == {
            EventType.ORDER_FILLED: "orders",
            EventType.ORDER_PARTIALLY_FILLED: "orders",
            EventType.ORDER_CANCELLED: "orders",
            EventType.SIGNAL_GENERATED: "signals",
        }

    def test_default_event_types_is_tuple_of_channels_keys(self):
        assert isinstance(DEFAULT_EVENT_TYPES, tuple)
        assert set(DEFAULT_EVENT_TYPES) == set(DEFAULT_EVENT_CHANNELS.keys())
        # Exposed as a tuple specifically so callers can't mutate the
        # bridge's defaults via the alias.
        with pytest.raises(AttributeError):
            DEFAULT_EVENT_TYPES.append(EventType.ORDER_CREATED)  # type: ignore[attr-defined]

    def test_default_event_channels_values_are_strings(self):
        for channel in DEFAULT_EVENT_CHANNELS.values():
            assert isinstance(channel, str)
            assert channel  # non-empty


# ---------------------------------------------------------------------------
# _json_safe
# ---------------------------------------------------------------------------


class TestJsonSafe:
    """``_json_safe`` must recursively coerce every value into something
    :func:`json.dumps` can serialize, without ever raising."""

    @pytest.mark.parametrize(
        "value",
        [
            "hello",
            "",
            42,
            -7,
            0,
            3.14,
            -0.5,
            True,
            False,
            None,
        ],
    )
    def test_primitives_pass_through(self, value):
        assert _json_safe(value) == value

    def test_dict_is_recursed_and_keys_stringified(self):
        out = _json_safe({"a": 1, 2: "two", None: "null_key"})
        assert out == {"a": 1, "2": "two", "None": "null_key"}

    def test_nested_dict_is_recursed(self):
        assert _json_safe({"outer": {"inner": [1, 2]}}) == {"outer": {"inner": [1, 2]}}

    def test_list_is_recursed(self):
        assert _json_safe([1, "x", None, True]) == [1, "x", None, True]

    def test_tuple_becomes_list(self):
        out = _json_safe((1, 2, 3))
        assert out == [1, 2, 3]
        assert isinstance(out, list)

    def test_set_is_sorted_and_becomes_list(self):
        out = _json_safe({3, 1, 2})
        assert out == [1, 2, 3]
        assert isinstance(out, list)

    def test_set_with_unsortable_elements_falls_back_to_iteration(self):
        out = _json_safe({1, "a", 2})  # type: ignore[arg-type]
        assert isinstance(out, list)
        assert sorted(out, key=str) == sorted([1, "a", 2], key=str)

    def test_datetime_is_stringified(self):
        dt = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
        assert _json_safe(dt) == str(dt)

    def test_uuid_is_stringified(self):
        u = uuid.uuid4()
        assert _json_safe(u) == str(u)

    def test_decimal_is_stringified(self):
        assert _json_safe(Decimal("3.14")) == "3.14"

    def test_strenum_is_stringified(self):
        out = _json_safe(EventType.ORDER_FILLED)
        assert isinstance(out, str)

    def test_plain_enum_is_stringified(self):
        class Color(Enum):
            RED = "red"

        assert _json_safe(Color.RED) == str(Color.RED)

    def test_arbitrary_object_is_stringified(self):
        class _Opaque:
            def __str__(self) -> str:
                return "opaque-repr"

        assert _json_safe(_Opaque()) == "opaque-repr"

    def test_passthrough_when_already_json_serializable(self):
        # The catch-all branch has an ``else`` arm that returns the value
        # unchanged when ``json.dumps`` already accepts it. Standard types
        # never reach it (caught earlier), so patch ``json.dumps`` to
        # simulate a custom-serializable object the bridge doesn't know.
        sentinel = object()
        original = bridge_mod.json.dumps

        def _accept(v: Any) -> str:
            if v is sentinel:
                return "ok"
            return original(v)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(bridge_mod.json, "dumps", _accept)
            assert _json_safe(sentinel) is sentinel


# ---------------------------------------------------------------------------
# _extract_identity
# ---------------------------------------------------------------------------


class TestExtractIdentity:
    """``_extract_identity`` must pull ``user_id`` / ``tenant_id`` out of
    a producer's ``data`` dict, accepting snake_case and camelCase aliases
    and coercing to strings (``None`` for missing / empty / unstringable)."""

    def test_non_dict_returns_none_pair(self):
        assert _extract_identity(None) == (None, None)
        assert _extract_identity("user_id=1") == (None, None)
        assert _extract_identity([("user_id", 1)]) == (None, None)
        assert _extract_identity(42) == (None, None)

    def test_missing_keys_returns_none_pair(self):
        assert _extract_identity({}) == (None, None)
        assert _extract_identity({"unrelated": "x"}) == (None, None)

    def test_snake_case_user_id(self):
        assert _extract_identity({"user_id": "u1"}) == ("u1", None)

    def test_camel_case_user_id_alias(self):
        assert _extract_identity({"userId": "u1"}) == ("u1", None)

    def test_snake_case_takes_precedence_over_camel(self):
        assert _extract_identity({"user_id": "snake", "userId": "camel"}) == (
            "snake",
            None,
        )

    def test_tenant_id_snake_and_camel(self):
        assert _extract_identity({"tenant_id": "t1"}) == (None, "t1")
        assert _extract_identity({"tenantId": "t1"}) == (None, "t1")
        assert _extract_identity({"tenant_id": "snake", "tenantId": "camel"}) == (
            None,
            "snake",
        )

    def test_both_identity_fields(self):
        assert _extract_identity({"user_id": "u1", "tenant_id": "t1"}) == ("u1", "t1")

    def test_int_user_id_is_stringified(self):
        assert _extract_identity({"user_id": 123}) == ("123", None)

    def test_float_user_id_is_stringified(self):
        assert _extract_identity({"user_id": 1.5})[0] == "1.5"

    def test_empty_string_resolves_to_none(self):
        # Empty strings are falsy → must normalize to None so room routing
        # never builds ``"user:"``.
        assert _extract_identity({"user_id": ""}) == (None, None)
        assert _extract_identity({"tenant_id": ""}) == (None, None)

    def test_none_value_resolves_to_none(self):
        assert _extract_identity({"user_id": None, "userId": None}) == (None, None)

    def test_uncoercible_value_resolves_to_none(self):
        assert _extract_identity({"user_id": ["a", "b"]}) == (None, None)
        assert _extract_identity({"user_id": {"nested": 1}}) == (None, None)
        assert _extract_identity({"tenant_id": object()}) == (None, None)


# ---------------------------------------------------------------------------
# serialize_event
# ---------------------------------------------------------------------------


class TestSerializeEvent:
    """``serialize_event`` builds the JSON-safe broadcast envelope and
    stamps identity at the top level."""

    def test_basic_envelope_shape(self):
        out = serialize_event({"type": "order.filled", "data": {"symbol": "AAPL"}})
        assert out["type"] == "event"
        assert out["event_type"] == "order.filled"
        assert out["channel"] is None  # bridge fills this in after resolving
        assert out["data"] == {"symbol": "AAPL"}

    def test_identity_stamped_from_data(self):
        out = serialize_event(
            {
                "type": "order.filled",
                "data": {"user_id": "u1", "tenant_id": "t1"},
            }
        )
        assert out["user_id"] == "u1"
        assert out["tenant_id"] == "t1"

    def test_identity_stamped_none_when_absent(self):
        out = serialize_event({"type": "order.filled", "data": {}})
        assert out["user_id"] is None
        assert out["tenant_id"] is None

    def test_identity_stamped_when_data_missing_entirely(self):
        out = serialize_event({"type": "order.filled"})
        assert out["data"] == {}
        assert out["user_id"] is None
        assert out["tenant_id"] is None

    def test_event_type_alias_used_when_type_absent(self):
        out = serialize_event({"event_type": "signal.generated", "data": {}})
        assert out["event_type"] == "signal.generated"

    def test_event_type_falsy_falls_back_to_alias(self):
        # ``type`` present but falsy → ``or`` falls through to event_type.
        out = serialize_event({"type": "", "event_type": "signal.generated"})
        assert out["event_type"] == "signal.generated"

    def test_source_and_timestamp_carried_through(self):
        out = serialize_event(
            {
                "type": "order.filled",
                "data": {},
                "source": "backtest",
                "timestamp": "2024-01-01T00:00:00+00:00",
            }
        )
        assert out["source"] == "backtest"
        assert out["timestamp"] == "2024-01-01T00:00:00+00:00"

    def test_source_and_timestamp_default_to_none(self):
        out = serialize_event({"type": "order.filled"})
        assert out["source"] is None
        assert out["timestamp"] is None

    def test_nested_non_json_values_are_normalized(self):
        u = uuid.uuid4()
        dt = datetime(2024, 1, 1, tzinfo=UTC)
        out = serialize_event(
            {
                "type": "order.filled",
                "data": {
                    "order_id": u,
                    "filled_at": dt,
                    "tags": {"urgent", "low"},
                    "meta": {"ts": dt, "qty": 3.0},
                },
            }
        )
        data = out["data"]
        assert data["order_id"] == str(u)
        assert data["filled_at"] == str(dt)
        assert isinstance(data["tags"], list)
        assert data["meta"]["ts"] == str(dt)
        assert data["meta"]["qty"] == 3.0

    def test_result_is_json_serializable(self):
        out = serialize_event(
            {
                "type": "order.filled",
                "data": {
                    "id": uuid.uuid4(),
                    "ts": datetime.now(UTC),
                    "amt": Decimal("9.99"),
                    "set": {1, 2},
                    "nested": {"k": (1, 2, 3)},
                },
            }
        )
        json.dumps(out)  # must not raise


# ---------------------------------------------------------------------------
# EventBusBridge — construction & introspection
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_defaults_channels_when_none(self):
        bridge = EventBusBridge(bus=_FakeBus(), manager=_FakeManager())
        assert bridge.channels == DEFAULT_EVENT_CHANNELS

    def test_custom_channels_override(self):
        custom = {EventType.ORDER_CREATED: "orders"}
        bridge = EventBusBridge(bus=_FakeBus(), manager=_FakeManager(), channels=custom)
        assert bridge.channels == custom

    def test_channels_returns_defensive_copy(self):
        bridge = EventBusBridge(bus=_FakeBus(), manager=_FakeManager())
        snapshot = bridge.channels
        snapshot[EventType.ORDER_CREATED] = "evil"
        assert EventType.ORDER_CREATED not in bridge.channels

    def test_subscribed_event_types_empty_before_start(self):
        bridge = EventBusBridge(bus=_FakeBus(), manager=_FakeManager())
        assert bridge.subscribed_event_types == ()

    def test_handler_is_cached_bound_method(self):
        # Each ``self._handle`` access produces a fresh wrapper, so the
        # bridge caches one bound method in ``self._handler`` for the
        # bus's identity-based subscribe/unsubscribe to round-trip.
        import types

        bridge = EventBusBridge(bus=_FakeBus(), manager=_FakeManager())
        assert isinstance(bridge._handler, types.MethodType)
        assert bridge._handler.__func__ is EventBusBridge._handle
        assert bridge._handler.__self__ is bridge
        # Accessing ``bridge._handle`` again yields a *different* object,
        # which is exactly why the cached attribute is necessary.
        assert bridge._handler is not bridge._handle


class TestChannelFor:
    """``channel_for`` resolves an event type (member or dotted string)
    to its channel, returning ``None`` for unrouted types."""

    @pytest.fixture
    def bridge(self):
        return EventBusBridge(bus=_FakeBus(), manager=_FakeManager())

    def test_known_member_returns_channel(self, bridge):
        assert bridge.channel_for(EventType.ORDER_FILLED) == "orders"
        assert bridge.channel_for(EventType.SIGNAL_GENERATED) == "signals"

    def test_dotted_string_value_resolves(self, bridge):
        assert bridge.channel_for("order.filled") == "orders"
        assert bridge.channel_for("signal.generated") == "signals"
        assert bridge.channel_for("order.cancelled") == "orders"
        assert bridge.channel_for("order.partially_filled") == "orders"

    def test_unknown_member_returns_none(self, bridge):
        assert bridge.channel_for(EventType.ORDER_CREATED) is None
        assert bridge.channel_for(EventType.ENGINE_STARTED) is None

    def test_unrecognized_string_returns_none(self, bridge):
        assert bridge.channel_for("not.a.real.event") is None
        assert bridge.channel_for("") is None

    def test_non_string_non_member_returns_none(self, bridge):
        assert bridge.channel_for(123) is None  # type: ignore[arg-type]
        assert bridge.channel_for(None) is None
        assert bridge.channel_for(object()) is None

    def test_custom_channels_are_resolvable(self):
        custom = {EventType.ORDER_CREATED: "orders"}
        bridge = EventBusBridge(bus=_FakeBus(), manager=_FakeManager(), channels=custom)
        assert bridge.channel_for(EventType.ORDER_CREATED) == "orders"
        assert bridge.channel_for("order.created") == "orders"
        assert bridge.channel_for(EventType.ORDER_FILLED) is None


# ---------------------------------------------------------------------------
# EventBusBridge — start / stop lifecycle
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_defaults_subscribes_all_channels(self):
        bus = _FakeBus()
        bridge = EventBusBridge(bus=bus, manager=_FakeManager())
        bridge.start()
        assert set(bridge.subscribed_event_types) == set(DEFAULT_EVENT_CHANNELS.keys())
        assert {et for et, _h in bus.subscribed} == set(DEFAULT_EVENT_CHANNELS.keys())
        for _et, handler in bus.subscribed:
            assert handler is bridge._handler

    def test_start_with_explicit_subset(self):
        bus = _FakeBus()
        bridge = EventBusBridge(bus=bus, manager=_FakeManager())
        bridge.start(event_types=[EventType.ORDER_FILLED, EventType.SIGNAL_GENERATED])
        assert set(bridge.subscribed_event_types) == {
            EventType.ORDER_FILLED,
            EventType.SIGNAL_GENERATED,
        }

    def test_start_raises_on_unrouted_event_type(self):
        bus = _FakeBus()
        bridge = EventBusBridge(bus=bus, manager=_FakeManager())
        with pytest.raises(ValueError, match="no channel configured"):
            bridge.start(event_types=[EventType.ORDER_CREATED])

    def test_start_with_mixed_raises_naming_offenders(self):
        bridge = EventBusBridge(bus=_FakeBus(), manager=_FakeManager())
        with pytest.raises(ValueError) as exc_info:
            bridge.start(
                event_types=[
                    EventType.ORDER_FILLED,
                    EventType.ENGINE_STARTED,
                    EventType.BACKTEST_STARTED,
                ]
            )
        msg = str(exc_info.value)
        assert "engine.started" in msg
        assert "backtest.started" in msg
        assert "order.filled" not in msg  # the valid one isn't listed

    def test_start_validates_before_mutating_state(self):
        bus = _FakeBus()
        bridge = EventBusBridge(bus=bus, manager=_FakeManager())
        with pytest.raises(ValueError):
            bridge.start(event_types=[EventType.ORDER_FILLED, EventType.ORDER_CREATED])
        assert bridge.subscribed_event_types == ()
        assert bus.subscribed == []


class TestStop:
    def test_stop_unsubscribes_all_registered(self):
        bus = _FakeBus()
        bridge = EventBusBridge(bus=bus, manager=_FakeManager())
        bridge.start()
        bridge.stop()
        assert bridge.subscribed_event_types == ()
        assert {et for et, _h in bus.unsubscribed} == set(DEFAULT_EVENT_CHANNELS.keys())
        for _et, handler in bus.unsubscribed:
            assert handler is bridge._handler

    def test_stop_with_explicit_subset_only_unsubscribes_those(self):
        bus = _FakeBus()
        bridge = EventBusBridge(bus=bus, manager=_FakeManager())
        bridge.start(event_types=[EventType.ORDER_FILLED])
        bridge.stop()
        assert {et for et, _h in bus.unsubscribed} == {EventType.ORDER_FILLED}

    def test_stop_is_idempotent_on_empty_registered(self):
        bridge = EventBusBridge(bus=_FakeBus(), manager=_FakeManager())
        # Nothing subscribed → stop is a no-op, must not raise.
        bridge.stop()
        assert bridge.subscribed_event_types == ()

    def test_stop_swallows_unsubscribe_exception_and_continues(self):
        # A single stuck unsubscribe must not prevent the rest of the
        # teardown (or a graceful shutdown) from completing.
        bus = _FakeBus()
        bridge = EventBusBridge(bus=bus, manager=_FakeManager())
        bridge.start()
        bus.fail_on_unsubscribe(RuntimeError("stuck"))

        # Must not raise despite every unsubscribe blowing up.
        bridge.stop()

        assert bridge.subscribed_event_types == ()
        # Every type was *attempted*.
        assert {et for et, _h in bus.unsubscribed} == set(DEFAULT_EVENT_CHANNELS.keys())

    def test_stop_clears_registered_even_when_one_unsubscribe_fails(self):
        # Even mid-failure, the registered list is fully cleared.
        bus = _FakeBus()
        bridge = EventBusBridge(bus=bus, manager=_FakeManager())
        bridge.start(event_types=[EventType.ORDER_FILLED])
        bus.fail_on_unsubscribe(ValueError("nope"))
        bridge.stop()
        assert bridge.subscribed_event_types == ()


# ---------------------------------------------------------------------------
# EventBusBridge — _handle dispatch
# ---------------------------------------------------------------------------


class TestHandle:
    """``_handle`` is the single bus-handler entry point. It must:

    1. Ignore non-dict payloads (warn + return).
    2. Ignore dict payloads missing a type (warn + return).
    3. No-op for events with no configured channel (debug + return).
    4. Serialize + stamp the channel, resolve the delivery room
       (``user:<id>`` when an identity is present, else the flat
       channel), and forward to ``manager.broadcast``.
    5. Swallow any broadcast-side exception so a misbehaving manager
       can't poison the EventBus dispatch loop.
    """

    async def test_non_dict_payload_is_ignored(self):
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        # str, list, None — all non-dict → warn + early return.
        for bad in ("not-a-dict", ["also", "not"], None, 42):
            await bridge._handle(bad)  # type: ignore[arg-type]
        assert manager.broadcasts == []

    async def test_missing_type_is_ignored(self):
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle({"data": {"x": 1}})
        await bridge._handle({"type": "", "data": {}})  # falsy type
        await bridge._handle({"event_type": None})
        assert manager.broadcasts == []

    async def test_unrouted_event_type_is_noop(self):
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        # ORDER_CREATED is a valid event type but not in the default map.
        await bridge._handle({"type": EventType.ORDER_CREATED, "data": {}})
        await bridge._handle({"type": "totally.unknown.event", "data": {}})
        assert manager.broadcasts == []

    async def test_type_key_resolves_channel(self):
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle({"type": "order.filled", "data": {"symbol": "AAPL"}})
        assert len(manager.broadcasts) == 1
        room, envelope = manager.broadcasts[0]
        # No user_id → flat channel as the room.
        assert room == "orders"
        assert envelope["channel"] == "orders"
        assert envelope["event_type"] == "order.filled"
        assert envelope["type"] == "event"
        assert envelope["data"] == {"symbol": "AAPL"}

    async def test_event_type_key_alias_resolves_channel(self):
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle({"event_type": "signal.generated", "data": {}})
        assert len(manager.broadcasts) == 1
        _room, envelope = manager.broadcasts[0]
        assert envelope["channel"] == "signals"

    async def test_member_event_type_resolves_channel(self):
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle({"type": EventType.ORDER_CANCELLED, "data": {}})
        assert len(manager.broadcasts) == 1
        assert manager.broadcasts[0][1]["channel"] == "orders"

    async def test_user_id_routes_to_user_room(self):
        # Identity-aware delivery: a user-scoped event must land only on
        # the owner's ``user:<id>`` room, never on the flat channel.
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle(
            {
                "type": "order.filled",
                "data": {"user_id": "u42", "symbol": "AAPL"},
            }
        )
        assert len(manager.broadcasts) == 1
        room, envelope = manager.broadcasts[0]
        assert room == "user:u42"
        # Channel is still stamped on the envelope for client-side routing.
        assert envelope["channel"] == "orders"
        assert envelope["user_id"] == "u42"

    async def test_camelcase_user_id_routes_to_user_room(self):
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle(
            {
                "type": "order.filled",
                "data": {"userId": "u99"},
            }
        )
        assert manager.broadcasts[0][0] == "user:u99"

    async def test_empty_user_id_falls_back_to_flat_channel(self):
        # An empty / missing user_id must NOT build ``"user:"`` — it
        # falls back to the flat channel (system-wide broadcast).
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle({"type": "order.filled", "data": {"user_id": ""}})
        assert manager.broadcasts[0][0] == "orders"

    async def test_tenant_id_does_not_change_room(self):
        # tenant_id is stamped on the envelope but does NOT change the
        # delivery room — only user_id scopes the room.
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle(
            {
                "type": "order.filled",
                "data": {"tenant_id": "t1"},
            }
        )
        room, envelope = manager.broadcasts[0]
        assert room == "orders"  # no user_id → flat channel
        assert envelope["tenant_id"] == "t1"

    async def test_broadcast_failure_is_swallowed(self):
        # A misbehaving manager must not break the EventBus dispatch loop.
        manager = _FakeManager()
        manager.fail_on_broadcast(RuntimeError("manager down"))
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        # Must not raise.
        await bridge._handle({"type": "order.filled", "data": {}})
        assert len(manager.broadcasts) == 1  # broadcast was attempted

    async def test_broadcast_failure_with_user_room_is_swallowed(self):
        manager = _FakeManager()
        manager.fail_on_broadcast(ConnectionError("gone"))
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle({"type": "order.filled", "data": {"user_id": "u1"}})
        assert len(manager.broadcasts) == 1

    async def test_envelope_is_json_serializable_for_complex_payload(self):
        manager = _FakeManager()
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle(
            {
                "type": "order.filled",
                "data": {
                    "user_id": "u1",
                    "order_id": uuid.uuid4(),
                    "ts": datetime.now(UTC),
                    "qty": Decimal("1.5"),
                    "tags": {"a", "b"},
                    "nested": {"k": (1, 2)},
                },
                "source": "backtest",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        _room, envelope = manager.broadcasts[0]
        # The whole envelope must be JSON-safe — broadcast never raises.
        json.dumps(envelope)

    async def test_recipient_count_returned_is_ignored_gracefully(self):
        # The bridge only logs the recipient count; any int is fine.
        manager = _FakeManager(recipients=7)
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle({"type": "order.filled", "data": {}})
        assert len(manager.broadcasts) == 1

    async def test_broadcast_zero_recipients_is_not_an_error(self):
        manager = _FakeManager(recipients=0)
        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        bridge.start()
        await bridge._handle({"type": "order.filled", "data": {}})
        assert len(manager.broadcasts) == 1


# ---------------------------------------------------------------------------
# EventBusBridge — end-to-end via the fake bus deliver()
# ---------------------------------------------------------------------------


class TestBridgeEndToEnd:
    """Drive the bridge through the fake bus's ``deliver`` to confirm the
    whole subscribe → handle → broadcast path works as wired."""

    async def test_deliver_routed_event_reaches_manager(self):
        bus = _FakeBus()
        manager = _FakeManager()
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()
        await bus.deliver(
            EventType.ORDER_FILLED,
            {"type": "order.filled", "data": {"symbol": "AAPL"}},
        )
        assert len(manager.broadcasts) == 1
        assert manager.broadcasts[0][0] == "orders"

    async def test_deliver_user_scoped_event_routes_to_user_room(self):
        bus = _FakeBus()
        manager = _FakeManager()
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()
        await bus.deliver(
            EventType.SIGNAL_GENERATED,
            {
                "type": "signal.generated",
                "data": {"user_id": "u7", "symbol": "MSFT"},
            },
        )
        assert manager.broadcasts[0][0] == "user:u7"
        assert manager.broadcasts[0][1]["channel"] == "signals"

    async def test_deliver_to_unsubscribed_event_does_nothing(self):
        bus = _FakeBus()
        manager = _FakeManager()
        bridge = EventBusBridge(bus=bus, manager=manager)
        # Only subscribe to signals — orders should not be delivered.
        bridge.start(event_types=[EventType.SIGNAL_GENERATED])
        await bus.deliver(
            EventType.ORDER_FILLED,
            {"type": "order.filled", "data": {}},
        )
        assert manager.broadcasts == []

    async def test_stop_blocks_further_delivery(self):
        bus = _FakeBus()
        manager = _FakeManager()
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()
        bridge.stop()
        await bus.deliver(
            EventType.ORDER_FILLED,
            {"type": "order.filled", "data": {}},
        )
        assert manager.broadcasts == []

    async def test_start_stop_start_round_trips(self):
        bus = _FakeBus()
        manager = _FakeManager()
        bridge = EventBusBridge(bus=bus, manager=manager)
        bridge.start()
        assert bridge.subscribed_event_types
        bridge.stop()
        assert not bridge.subscribed_event_types
        bridge.start()
        assert set(bridge.subscribed_event_types) == set(DEFAULT_EVENT_CHANNELS.keys())
        await bus.deliver(
            EventType.ORDER_FILLED,
            {"type": "order.filled", "data": {}},
        )
        assert len(manager.broadcasts) == 1
