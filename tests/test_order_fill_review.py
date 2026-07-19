"""Tests capturing the two HIGH-severity bugs from the last review of
``engine/core/order_manager.py``:

1. **Partial fills must still publish fill events.** The current
   ``process_signal`` only calls ``_publish_fill_event`` when
   ``order.status == OrderStatus.FILLED``. When the execution backend
   fills fewer shares than requested (a partial fill), the order must
   transition to ``OrderStatus.PARTIALLY_FILLED`` *and* still publish an
   ``ORDER_FILLED`` event so WebSocket clients / outbox consumers
   receive the update. The current implementation hard-codes
   ``OrderStatus.FILLED`` on every successful fill and the publish gate
   silently excludes anything else.

2. **``asyncio.wait_for`` timeout on ``EventBus.emit`` must fire when
   the bus is wedged.** A slow/stuck Redis or in-process dispatcher must
   never stall order execution. ``_publish_fill_event`` wraps the emit
   call in ``asyncio.wait_for`` so the slow bus is cancelled, the
   (infrastructure) timeout is swallowed + counted, the order is still
   recorded in ``completed_orders``, and no exception propagates.

The file is self-contained: only in-process mocks/stubs are used, no DB
or Redis is required.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

from engine.core.cost_model import DefaultCostModel
from engine.core.execution.base import FillResult
from engine.core.order_manager import Order, OrderManager, OrderStatus
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Signal
from engine.events.bus import EventType
from engine.observability.metrics import RecordingBackend, set_metrics

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown


class FullFillBackend:
    """Execution backend that fills the full requested quantity at the
    prevailing market price."""

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def execute(
        self, order: Order, market_price: float, costs: CostBreakdown
    ) -> FillResult:
        return FillResult(success=True, price=market_price, quantity=order.quantity)


class PartialFillBackend:
    """Execution backend that returns a *partial* fill: only
    ``fill_qty`` of the requested shares are filled, at ``price``."""

    def __init__(self, fill_qty: int = 5, price: float = 150.0) -> None:
        self._fill_qty = fill_qty
        self._price = price

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def execute(
        self, order: Order, market_price: float, costs: CostBreakdown
    ) -> FillResult:
        return FillResult(success=True, price=self._price, quantity=self._fill_qty)


class RecordingEventBus:
    """EventBus stand-in that records every ``emit`` call without
    touching Redis or sleeping. Used to assert ``_publish_fill_event``
    was actually invoked on the happy path."""

    def __init__(self) -> None:
        self.emit_calls = 0
        self.events: list[tuple] = []

    async def emit(self, event_type, data, source="engine") -> None:
        self.emit_calls += 1
        self.events.append((event_type, data, source))


class SlowEventBus:
    """EventBus stand-in whose ``emit`` ``await``s a sleep far longer
    than the OrderManager's ``wait_for`` timeout.

    Simulates a wedged Redis or in-process dispatcher: the coroutine
    never completes on its own — only the ``asyncio.wait_for`` cancel
    can finish it. Cancellation is cooperative: ``asyncio.sleep`` raises
    ``CancelledError`` inside the coroutine, the ``wait_for`` wrapper
    raises ``TimeoutError`` to the caller, and ``_publish_fill_event``
    swallows it as an infrastructure failure.
    """

    def __init__(self, sleep_seconds: float = 10.0) -> None:
        self._sleep_seconds = sleep_seconds
        self.emit_calls = 0
        self.cancelled = False
        self.last_event_type = None
        self.last_data = None
        self.last_source = None

    async def emit(self, event_type, data, source="engine") -> None:
        self.emit_calls += 1
        self.last_event_type = event_type
        self.last_data = data
        self.last_source = source
        try:
            await asyncio.sleep(self._sleep_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _make_order_manager(event_bus=None, metrics=None) -> OrderManager:
    """Build an OrderManager wired to a real cost model, risk engine,
    and in-memory portfolio — no DB, no Redis."""
    return OrderManager(
        cost_model=DefaultCostModel(),
        risk_engine=RiskEngine(),
        portfolio=Portfolio(initial_cash=100_000.0),
        event_bus=event_bus,
        metrics=metrics,
    )


class TestPartialFillPublishesEvent:
    """HIGH-severity bug 1: partial fills must publish fill events.

    Drives a partial fill (backend fills fewer shares than requested)
    through ``process_signal`` and asserts both:

    * the order is marked ``PARTIALLY_FILLED`` (not silently
      ``FILLED``), and
    * an ``ORDER_FILLED`` event is still published to the bus so
      connected WebSocket clients / outbox consumers see it.

    The current implementation unconditionally transitions successful
    fills to ``FILLED`` and only publishes when
    ``order.status == OrderStatus.FILLED`` — so partial fills are
    mislabelled and (once that is fixed) would be silently dropped by
    the publish gate.
    """

    async def test_partial_fill_publishes_event(self) -> None:
        bus = RecordingEventBus()
        om = _make_order_manager(event_bus=bus)
        om.set_execution_backend(PartialFillBackend(fill_qty=5, price=150.0))
        signal = Signal.buy(symbol="AAPL", strategy_id="strat-1", quantity=10)
        order = await om.process_signal(signal, market_price=150.0)

        # Fill returned 5 of the requested 10 shares.
        assert order.fill_quantity == 5
        assert order.quantity == 10

        # The status must reflect the partial fill — NOT a full fill.
        assert order.status == OrderStatus.PARTIALLY_FILLED, (
            "partial fill (fill_quantity=5 < quantity=10) must transition "
            f"to OrderStatus.PARTIALLY_FILLED, got {order.status!r}"
        )

        # A fill event must still be published so connected clients see
        # the (partial) execution.
        assert bus.emit_calls == 1, (
            "partial fills must publish an ORDER_FILLED event so connected "
            "clients receive the update"
        )
        event_type, data, source = bus.events[0]
        assert event_type == EventType.ORDER_FILLED
        assert source == "order_manager"
        assert data is not None
        assert data["order_id"] == order.id
        assert data["symbol"] == "AAPL"
        assert data["qty"] == 5
        assert data["price"] == 150.0
        assert data["status"] == OrderStatus.PARTIALLY_FILLED.value


class TestSlowEventBusTimeout:
    """HIGH-severity bug 2: a wedged EventBus must not stall order
    execution.

    ``_publish_fill_event`` wraps ``EventBus.emit`` in
    ``asyncio.wait_for(..., timeout=2.0)`` so a stuck bus is cancelled.
    The resulting ``TimeoutError`` is swallowed as an infrastructure
    failure, a counter is incremented, and — crucially — order
    execution is unaffected: the order is still recorded in
    ``completed_orders`` and no exception propagates to the caller.
    """

    @pytest.fixture
    def metrics_backend(self) -> RecordingBackend:
        backend = RecordingBackend()
        set_metrics(backend)
        return backend

    async def test_slow_event_bus_timeout(self, metrics_backend: RecordingBackend) -> None:
        slow_bus = SlowEventBus(sleep_seconds=10.0)
        om = _make_order_manager(event_bus=slow_bus, metrics=metrics_backend)
        om.set_execution_backend(FullFillBackend())
        signal = Signal.buy(symbol="AAPL", strategy_id="strat-1", quantity=10)
        t0 = time.monotonic()
        order = await om.process_signal(signal, market_price=150.0)
        elapsed = time.monotonic() - t0

        # The wedged emit was attempted exactly once.
        assert slow_bus.emit_calls == 1
        assert slow_bus.last_event_type == EventType.ORDER_FILLED

        # The wait_for timeout fired well before the 10s sleep finished.
        assert elapsed < 3.0, (
            f"process_signal took {elapsed:.2f}s on a wedged bus — the "
            "asyncio.wait_for timeout did not fire (expected < 3s)"
        )
        # Cancellation propagated into the bus so its coroutine cleaned up.
        assert slow_bus.cancelled is True, (
            "SlowEventBus.emit was never cancelled — asyncio.wait_for did "
            "not interrupt the wedged emit"
        )

        # Order execution completed normally despite the publish failure.
        assert order.status == OrderStatus.FILLED
        assert order.fill_price == 150.0
        assert order.fill_quantity == 10
        assert order in om.completed_orders
        assert len(om.completed_orders) == 1

        # The swallowed infrastructure failure was counted.
        key = ("order_manager.fill_event_publish_failed", (("error_type", "TimeoutError"),))
        assert metrics_backend.counters.get(key) == 1.0, (
            "expected order_manager.fill_event_publish_failed counter "
            "(error_type=TimeoutError) to be incremented exactly once"
        )
