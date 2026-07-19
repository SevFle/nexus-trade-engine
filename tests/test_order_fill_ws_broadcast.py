"""Focused unit test: OrderManager fill event → WebSocket broadcast.

Verifies the wiring introduced for "WebSocket broadcast of order fill
events": when ``OrderManager`` fills an order it publishes an
``ORDER_FILLED`` event to the event bus, which the
:class:`~engine.api.ws.event_bridge.EventBusBridge` picks up and fans
out to the ``orders`` channel — producing the WS broadcast payload
connected clients receive.

Scope is intentionally narrow: OrderManager → EventBus → EventBusBridge
→ a recording ConnectionManager stand-in. No Redis, no FastAPI.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from engine.api.ws.event_bridge import EventBusBridge
from engine.core.cost_model import DefaultCostModel
from engine.core.execution.base import FillResult
from engine.core.order_manager import OrderManager, OrderStatus
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal
from engine.events.bus import EventBus, EventType

if TYPE_CHECKING:
    from engine.api.ws.protocol import EventMessage
    from engine.core.order_manager import Order


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
        # bridge must normalise the dotted ``EventType`` value
        # (``"order.filled"``) to the underscore mapping key and route it
        # to the orders channel. ``status`` resolves the room to
        # ``orders:status:filled``.
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
