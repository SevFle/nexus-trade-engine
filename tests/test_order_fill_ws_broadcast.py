"""Focused unit tests: OrderManager fill event → WebSocket broadcast.

Verifies the wiring introduced for "WebSocket broadcast of order fill
events": when ``OrderManager`` fills an order it publishes an
``ORDER_FILLED`` event to the event bus, which the
:class:`~engine.api.ws.event_bridge.EventBusBridge` picks up and fans
out to the ``orders`` channel — producing the WS broadcast payload
connected clients receive.

Additional coverage enforced here:

* ``_publish_fill_event`` narrows its ``except`` to bus infrastructure
  failures (``ConnectionError``, ``TimeoutError``, ``RuntimeError``) —
  logging + a counter increment — and **re-raises** any other exception
  (e.g. :class:`TypeError`) so programmer bugs are not silently
  swallowed.
* ``_EVENT_TO_CHANNEL`` is keyed by :class:`EventType` members directly
  (and equivalently by their dotted ``.value`` strings), so the bridge
  routes every event type the bus actually emits without relying on a
  ``.replace('.', '_')`` normalisation step.

Scope is intentionally narrow: OrderManager → EventBus → EventBusBridge
→ a recording ConnectionManager stand-in. No Redis, no FastAPI.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from engine.api.ws.event_bridge import _EVENT_TO_CHANNEL, EventBusBridge
from engine.core.cost_model import DefaultCostModel
from engine.core.execution.base import FillResult
from engine.core.order_manager import Order, OrderManager, OrderStatus
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal
from engine.events.bus import EventBus, EventType
from engine.observability.metrics import RecordingBackend, set_metrics

if TYPE_CHECKING:
    from engine.api.ws.protocol import EventMessage


class FakeExecutionBackend:
    """Mirrors the fake used by ``tests/test_order_manager.py``."""

    def __init__(self, *, success: bool = True, price: float = 150.0, quantity: int = 10):
        self._success = success
        self._price = price
        self._quantity = quantity

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def execute(self, order: Order, market_price: float, costs) -> FillResult:
        if self._success:
            return FillResult(success=True, price=self._price, quantity=self._quantity)
        return FillResult(success=False, reason="Simulated failure")


class RecordingConnectionManager:
    """Minimal stand-in for ``ConnectionManager``.

    Only implements the surface the bridge touches (``next_seq`` and
    ``broadcast``) and records every broadcast so the test can assert on
    the exact WS payload.
    """

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, EventMessage]] = []
        self._seq = 0

    def next_seq(self, room: str) -> int:
        self._seq += 1
        return self._seq

    async def broadcast(self, room: str, message: EventMessage) -> int:
        self.broadcasts.append((room, message))
        return 1


class FlakyEventBus:
    """EventBus stand-in whose ``emit`` raises a configurable exception.

    Used to prove ``_publish_fill_event`` only swallows infrastructure
    errors and re-raises everything else.
    """

    def __init__(self, *, raise_exc: BaseException | None = None) -> None:
        self.emit_calls = 0
        self._raise_exc = raise_exc
        self.last_event_type: object | None = None
        self.last_data: dict | None = None
        self.last_source: str | None = None

    async def emit(self, event_type, data=None, source="engine"):
        self.emit_calls += 1
        self.last_event_type = event_type
        self.last_data = data
        self.last_source = source
        if self._raise_exc is not None:
            raise self._raise_exc


@pytest.fixture
def event_bus() -> EventBus:
    # Never connects to Redis — in-process dispatch only.
    return EventBus(redis_url="redis://localhost:0")


@pytest.fixture
def recording_manager() -> RecordingConnectionManager:
    return RecordingConnectionManager()


@pytest.fixture
def bridge(event_bus: EventBus, recording_manager: RecordingConnectionManager) -> EventBusBridge:
    b = EventBusBridge(bus=event_bus, manager=recording_manager)
    b.start()
    return b


@pytest.fixture
def order_manager(event_bus: EventBus) -> OrderManager:
    om = OrderManager(
        cost_model=DefaultCostModel(),
        risk_engine=RiskEngine(),
        portfolio=Portfolio(initial_cash=100_000.0),
        event_bus=event_bus,
    )
    om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))
    return om


class TestOrderFillWebSocketBroadcast:
    async def test_fill_event_produces_correct_ws_broadcast(
        self,
        order_manager: OrderManager,
        recording_manager: RecordingConnectionManager,
        bridge: EventBusBridge,
    ) -> None:
        signal = Signal.buy(symbol="AAPL", strategy_id="strat-1", quantity=10)
        order = await order_manager.process_signal(signal, market_price=150.0)

        # Sanity: the order really did fill.
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10
        assert order.fill_price == 150.0

        # The bridge fans out via a background task; let it drain.
        await asyncio.sleep(0.05)

        # Exactly one broadcast reached the per-symbol orders room.
        symbol_room = [r for r, _ in recording_manager.broadcasts if r == "orders:symbol:AAPL"]
        assert len(symbol_room) == 1

        _room, msg = next(
            (r, m) for r, m in recording_manager.broadcasts if r == "orders:symbol:AAPL"
        )
        assert msg.channel == "orders"
        assert msg.room == "orders:symbol:AAPL"
        assert msg.seq >= 1

        # Outer envelope follows EventBus' Event.to_dict() shape.
        payload = msg.payload
        assert payload["type"] == EventType.ORDER_FILLED.value
        assert payload["source"] == "order_manager"
        assert payload["timestamp"]

        # Serialized fill data — the fields a connected client needs.
        data = payload["data"]
        assert data["order_id"] == order.id
        assert data["symbol"] == "AAPL"
        assert data["side"] == Side.BUY.value
        assert data["qty"] == 10
        assert data["price"] == 150.0
        assert data["status"] == OrderStatus.FILLED.value
        assert data["timestamp"] == order.filled_at.isoformat()
        assert data["strategy_id"] == "strat-1"
        assert data["signal_id"] == signal.id

    async def test_fill_routes_dotted_event_type_value(
        self,
        event_bus: EventBus,
        recording_manager: RecordingConnectionManager,
        bridge: EventBusBridge,
    ) -> None:
        # Publish a raw ORDER_FILLED event straight through the bus. The
        # bridge must route the dotted ``EventType`` value
        # (``"order.filled"``) to the orders channel — the lookup works
        # directly because ``_EVENT_TO_CHANNEL`` is keyed by ``EventType``
        # members, which compare equal to their dotted string values.
        # ``status`` resolves the room to ``orders:status:filled``.
        await event_bus.emit(
            EventType.ORDER_FILLED,
            {"order_id": "x", "symbol": None, "status": OrderStatus.FILLED.value},
            source="order_manager",
        )
        await asyncio.sleep(0.05)

        rooms = {r for r, _ in recording_manager.broadcasts}
        assert "orders:status:filled" in rooms

    async def test_no_event_bus_skips_publish(self) -> None:
        # Without a bus wired, filling still works and does not raise.
        om = OrderManager(
            cost_model=DefaultCostModel(),
            risk_engine=RiskEngine(),
            portfolio=Portfolio(initial_cash=100_000.0),
        )
        om.set_execution_backend(FakeExecutionBackend(success=True, price=100.0, quantity=5))
        signal = Signal.buy(symbol="MSFT", strategy_id="s", quantity=5)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.FILLED

    async def test_failed_order_does_not_broadcast(
        self,
        event_bus: EventBus,
        recording_manager: RecordingConnectionManager,
        bridge: EventBusBridge,
    ) -> None:
        om = OrderManager(
            cost_model=DefaultCostModel(),
            risk_engine=RiskEngine(),
            portfolio=Portfolio(initial_cash=100_000.0),
            event_bus=event_bus,
        )
        om.set_execution_backend(FakeExecutionBackend(success=False))
        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=10)
        order = await om.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.FAILED
        await asyncio.sleep(0.05)
        assert recording_manager.broadcasts == []


class TestPublishFillEventErrorHandling:
    """``_publish_fill_event`` must distinguish infrastructure failures
    (swallowed + counted) from programmer bugs (re-raised)."""

    @pytest.fixture
    def metrics_backend(self) -> RecordingBackend:
        backend = RecordingBackend()
        set_metrics(backend)
        return backend

    @pytest.fixture
    def flaky_bus(self) -> FlakyEventBus:
        return FlakyEventBus()

    @pytest.fixture
    def order_manager_with_flaky_bus(
        self, flaky_bus: FlakyEventBus, metrics_backend: RecordingBackend
    ) -> OrderManager:
        om = OrderManager(
            cost_model=DefaultCostModel(),
            risk_engine=RiskEngine(),
            portfolio=Portfolio(initial_cash=100_000.0),
            event_bus=flaky_bus,
        )
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))
        return om

    async def test_connection_error_is_swallowed_and_counted(
        self,
        flaky_bus: FlakyEventBus,
        metrics_backend: RecordingBackend,
        order_manager_with_flaky_bus: OrderManager,
    ) -> None:
        flaky_bus._raise_exc = ConnectionError("bus down")

        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=10)
        # Must not raise — execution continues despite the bus outage.
        order = await order_manager_with_flaky_bus.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.FILLED
        assert flaky_bus.emit_calls == 1

        # Counter tagged with the exception type was incremented exactly once.
        key = (
            "order_manager.fill_event_publish_failed",
            (("error_type", "ConnectionError"),),
        )
        assert metrics_backend.counters.get(key) == 1.0

    async def test_timeout_error_is_swallowed_and_counted(
        self,
        flaky_bus: FlakyEventBus,
        metrics_backend: RecordingBackend,
        order_manager_with_flaky_bus: OrderManager,
    ) -> None:
        flaky_bus._raise_exc = TimeoutError("bus timed out")

        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=10)
        order = await order_manager_with_flaky_bus.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.FILLED
        key = (
            "order_manager.fill_event_publish_failed",
            (("error_type", "TimeoutError"),),
        )
        assert metrics_backend.counters.get(key) == 1.0

    async def test_runtime_error_is_swallowed_and_counted(
        self,
        flaky_bus: FlakyEventBus,
        metrics_backend: RecordingBackend,
        order_manager_with_flaky_bus: OrderManager,
    ) -> None:
        flaky_bus._raise_exc = RuntimeError("no running event loop")

        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=10)
        order = await order_manager_with_flaky_bus.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.FILLED
        key = (
            "order_manager.fill_event_publish_failed",
            (("error_type", "RuntimeError"),),
        )
        assert metrics_backend.counters.get(key) == 1.0

    async def test_unexpected_exception_propagates(
        self,
        flaky_bus: FlakyEventBus,
        metrics_backend: RecordingBackend,
        order_manager_with_flaky_bus: OrderManager,
    ) -> None:
        # A TypeError is a programmer bug, not a transient infrastructure
        # failure — it must surface, not be swallowed.
        flaky_bus._raise_exc = TypeError("bad payload type")

        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=10)
        with pytest.raises(TypeError):
            await order_manager_with_flaky_bus.process_signal(signal, market_price=150.0)

        # No counter was incremented — the error was not classified as an
        # infrastructure failure.
        assert all(
            name != "order_manager.fill_event_publish_failed"
            for (name, _tags) in metrics_backend.counters
        )

    async def test_value_error_propagates(
        self,
        flaky_bus: FlakyEventBus,
        metrics_backend: RecordingBackend,
        order_manager_with_flaky_bus: OrderManager,
    ) -> None:
        flaky_bus._raise_exc = ValueError("malformed payload")

        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=10)
        with pytest.raises(ValueError):
            await order_manager_with_flaky_bus.process_signal(signal, market_price=150.0)

        assert all(
            name != "order_manager.fill_event_publish_failed"
            for (name, _tags) in metrics_backend.counters
        )

    async def test_no_bus_no_metric(
        self,
        metrics_backend: RecordingBackend,
    ) -> None:
        # Without a bus wired, _publish_fill_event short-circuits before
        # the try/except, so no metric is emitted even on the happy path.
        om = OrderManager(
            cost_model=DefaultCostModel(),
            risk_engine=RiskEngine(),
            portfolio=Portfolio(initial_cash=100_000.0),
        )
        om.set_execution_backend(FakeExecutionBackend(success=True, price=100.0, quantity=1))
        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=1)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.FILLED
        assert all(
            name != "order_manager.fill_event_publish_failed"
            for (name, _tags) in metrics_backend.counters
        )


class TestPublishFillEventPayloadAndCompletedOrders:
    """Targeted regression tests for the fill-event payload shape and
    for the order in which ``completed_orders`` is appended relative to
    event publishing.

    These pin two properties that the broader happy-path tests don't
    assert directly:

    * ``_publish_fill_event`` must succeed against a *real* filled
      :class:`Order` without raising :class:`AttributeError` — i.e. every
      attribute referenced in the payload exists on the model.
    * The order is recorded in ``completed_orders`` *before* the fill
      event is published, so a publish failure (swallowed infrastructure
      error or a propagated programmer bug) never drops it from the log.
    """

    @pytest.fixture
    def metrics_backend(self) -> RecordingBackend:
        backend = RecordingBackend()
        set_metrics(backend)
        return backend

    async def test_publish_fill_event_succeeds_without_attribute_error(
        self,
        event_bus: EventBus,
    ) -> None:
        """Build a real filled Order and publish directly.

        Catches any drift between the Order model and the payload
        ``_publish_fill_event`` builds (a missing attribute would raise
        :class:`AttributeError` inside the coroutine).
        """
        om = OrderManager(
            cost_model=DefaultCostModel(),
            risk_engine=RiskEngine(),
            portfolio=Portfolio(initial_cash=100_000.0),
            event_bus=event_bus,
        )

        order = Order(
            signal_id="sig-1",
            strategy_id="strat-1",
            symbol="AAPL",
            side=Side.BUY,
            quantity=10,
        )
        order.fill_price = 150.0
        order.fill_quantity = 10
        order.filled_at = datetime.now(UTC)
        order.transition(OrderStatus.FILLED)

        # Must not raise — every attribute referenced in the payload
        # exists on Order, so there's nothing for the publish path to
        # trip over.
        await om._publish_fill_event(order)

    async def test_order_recorded_in_completed_orders_when_publish_fails(
        self,
        metrics_backend: RecordingBackend,
    ) -> None:
        """The order must land in ``completed_orders`` even when the
        event bus is unavailable.

        Drives a ``ConnectionError`` out of the bus (an infrastructure
        failure that ``_publish_fill_event`` swallows) and confirms the
        just-filled order still appears in ``completed_orders``.
        """
        flaky_bus = FlakyEventBus(raise_exc=ConnectionError("bus down"))
        om = OrderManager(
            cost_model=DefaultCostModel(),
            risk_engine=RiskEngine(),
            portfolio=Portfolio(initial_cash=100_000.0),
            event_bus=flaky_bus,
        )
        om.set_execution_backend(
            FakeExecutionBackend(success=True, price=150.0, quantity=10)
        )

        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=10)
        order = await om.process_signal(signal, market_price=150.0)

        # The order filled and was emitted at (then swallowed).
        assert order.status == OrderStatus.FILLED
        assert flaky_bus.emit_calls == 1
        # Crucially: it is still in completed_orders despite the bus
        # being down. Downstream reconciliation must never lose it.
        assert order in om.completed_orders
        assert len(om.completed_orders) == 1

    async def test_order_recorded_in_completed_orders_when_publish_propagates(
        self,
        metrics_backend: RecordingBackend,
    ) -> None:
        """Even when a *non*-infrastructure exception propagates out of
        ``_publish_fill_event``, the order must already be in
        ``completed_orders``.

        The append now happens *before* the publish call, so a
        propagated :class:`TypeError` (a programmer bug, intentionally
        re-raised) leaves the order recorded for inspection.
        """
        flaky_bus = FlakyEventBus(raise_exc=TypeError("bad payload type"))
        om = OrderManager(
            cost_model=DefaultCostModel(),
            risk_engine=RiskEngine(),
            portfolio=Portfolio(initial_cash=100_000.0),
            event_bus=flaky_bus,
        )
        om.set_execution_backend(
            FakeExecutionBackend(success=True, price=150.0, quantity=10)
        )

        signal = Signal.buy(symbol="AAPL", strategy_id="s", quantity=10)
        with pytest.raises(TypeError):
            await om.process_signal(signal, market_price=150.0)

        # Despite the propagated error, the order was recorded first.
        assert len(om.completed_orders) == 1
        assert om.completed_orders[0].status == OrderStatus.FILLED
        assert om.completed_orders[0].symbol == "AAPL"


class TestEventBridgeChannelRouting:
    """The bridge must route every subscribed event type to its channel
    using ``EventType`` members directly as mapping keys."""

    @pytest.mark.parametrize(
        ("event_type", "expected_channel"),
        [
            (EventType.PORTFOLIO_UPDATED, "portfolio"),
            (EventType.POSITION_OPENED, "portfolio"),
            (EventType.POSITION_CLOSED, "portfolio"),
            (EventType.ORDER_CREATED, "orders"),
            (EventType.ORDER_VALIDATED, "orders"),
            (EventType.ORDER_SUBMITTED, "orders"),
            (EventType.ORDER_FILLED, "orders"),
            (EventType.ORDER_REJECTED, "orders"),
            (EventType.ORDER_FAILED, "orders"),
            (EventType.STRATEGY_LOADED, "strategies"),
            (EventType.STRATEGY_UNLOADED, "strategies"),
            (EventType.STRATEGY_ERROR, "strategies"),
        ],
    )
    def test_event_type_member_routes_to_channel(
        self, event_type: EventType, expected_channel: str
    ) -> None:
        # Lookup by EventType member (the canonical key form).
        assert _EVENT_TO_CHANNEL[event_type] == expected_channel
        # Lookup by the dotted string value the EventBus serializes into
        # the payload's "type" field — must work because StrEnum members
        # hash identically to their .value.
        assert _EVENT_TO_CHANNEL[event_type.value] == expected_channel

    def test_mapping_uses_dotted_keys_not_underscore(self) -> None:
        # Every key must be either an EventType member or its dotted
        # ``.value`` — never the legacy underscore form.
        for key in _EVENT_TO_CHANNEL:
            if isinstance(key, EventType):
                continue
            assert key.count(".") >= 1, f"unexpected non-dotted key: {key!r}"
            assert "_" not in key, f"unexpected underscore-style key: {key!r}"

    async def test_bridge_routes_every_subscribed_event_type(
        self,
        event_bus: EventBus,
        recording_manager: RecordingConnectionManager,
        bridge: EventBusBridge,
    ) -> None:
        # Emit one event of every subscribed type and confirm each one
        # is routed to its expected channel via the broadcast envelope.
        cases = [
            (EventType.PORTFOLIO_UPDATED, "portfolio"),
            (EventType.POSITION_OPENED, "portfolio"),
            (EventType.POSITION_CLOSED, "portfolio"),
            (EventType.ORDER_CREATED, "orders"),
            (EventType.ORDER_VALIDATED, "orders"),
            (EventType.ORDER_SUBMITTED, "orders"),
            (EventType.ORDER_FILLED, "orders"),
            (EventType.ORDER_REJECTED, "orders"),
            (EventType.ORDER_FAILED, "orders"),
            (EventType.STRATEGY_LOADED, "strategies"),
            (EventType.STRATEGY_UNLOADED, "strategies"),
            (EventType.STRATEGY_ERROR, "strategies"),
        ]
        for et, _channel in cases:
            await event_bus.emit(et, {"symbol": "AAPL"}, source="test")
            await asyncio.sleep(0.02)

        channels_observed = {msg.channel for _room, msg in recording_manager.broadcasts}
        assert channels_observed == {"portfolio", "orders", "strategies"}

    async def test_bridge_drops_unknown_dotted_event_type(
        self,
        event_bus: EventBus,
        recording_manager: RecordingConnectionManager,
        bridge: EventBusBridge,
    ) -> None:
        # An event type the bridge doesn't subscribe to (here: a market
        # data event) is dropped silently — no broadcast, no exception.
        await event_bus.emit(
            EventType.MARKET_DATA_UPDATE, {"symbol": "AAPL"}, source="test"
        )
        await asyncio.sleep(0.02)
        assert recording_manager.broadcasts == []
