"""Focused tests for ``OrderManager.continue_order``.

``continue_order`` resumes a ``PARTIALLY_FILLED`` order by:

1. Loading the existing order by id.
2. Asserting its status is ``PARTIALLY_FILLED``.
3. Computing the unfilled residual.
4. Asking the execution backend to fill the residual.
5. Calling ``_reconcile_fill`` on the *same* ``Order`` object so the
   cumulative ``fill_quantity`` and the volume-weighted average fill
   price (VWAP) are updated in place.

A ``max_retries`` guard (default 10) caps the loop so a backend that
always under-fills cannot trap the caller forever.

These tests pin two properties the rest of the suite doesn't cover:

* After ``process_signal`` produces a partial fill and
  ``continue_order`` drains the residual, ``order.fill_quantity`` is the
  *cumulative* total and ``order.fill_price`` is the VWAP across every
  fill — not just the last fill's price.
* ``continue_order`` refuses orders that are not partially filled and
  raises ``ValueError`` so callers can't accidentally re-execute a
  completed / rejected / failed order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engine.core.cost_model import DefaultCostModel
from engine.core.execution.base import FillResult
from engine.core.order_manager import OrderManager, OrderStatus
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Signal

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order


class ScriptedBackend:
    """Execution backend that returns a scripted sequence of fills.

    Each call to ``execute`` pops the next ``FillResult`` from
    ``script``. This lets a single test drive a partial fill through
    ``process_signal`` and then a completing fill through
    ``continue_order`` against deterministic prices and quantities.
    """

    def __init__(self, script: list[FillResult]):
        self._script = list(script)
        self.calls: list[tuple[Order, float, CostBreakdown]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def execute(
        self, order: Order, market_price: float, costs: CostBreakdown
    ) -> FillResult:
        self.calls.append((order, market_price, costs))
        if not self._script:
            raise AssertionError("ScriptedBackend ran out of scripted fills")
        return self._script.pop(0)


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(initial_cash=100_000.0)


@pytest.fixture
def cost_model() -> DefaultCostModel:
    return DefaultCostModel()


@pytest.fixture
def risk_engine() -> RiskEngine:
    return RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)


class TestContinueOrderCompletesPartialFill:
    """``process_signal`` → partial fill → ``continue_order`` → FILLED."""

    async def test_continue_order_drains_residual_and_tracks_cumulative_vwap(
        self,
        cost_model: DefaultCostModel,
        risk_engine: RiskEngine,
        portfolio: Portfolio,
    ) -> None:
        # First call under-fills (5 of 10) at $100; the continuation
        # call completes the residual 5 at $110. Cumulative VWAP should
        # be (5*100 + 5*110) / 10 == $105.
        backend = ScriptedBackend(
            [
                FillResult(success=True, price=100.0, quantity=5),
                FillResult(success=True, price=110.0, quantity=5),
            ]
        )
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(backend)

        signal = Signal.buy(symbol="AAPL", strategy_id="strat", quantity=10)
        order = await om.process_signal(signal, market_price=100.0)

        # Sanity: the initial fill was partial.
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 5
        assert order.fill_price == 100.0
        assert len(order.fills) == 1

        # Same Order object is the one returned by continue_order.
        continued = await om.continue_order(order.id)
        assert continued is order

        # Cumulative fill_quantity reaches the full order quantity and
        # the order is now FILLED, not PARTIALLY_FILLED.
        assert continued.status == OrderStatus.FILLED
        assert continued.fill_quantity == 10

        # VWAP across both fills — not just the last fill's price.
        expected_vwap = (100.0 * 5 + 110.0 * 5) / 10
        assert continued.fill_price == pytest.approx(expected_vwap)

        # The per-fill audit trail records both fills in order.
        assert len(continued.fills) == 2
        assert continued.fills[0]["price"] == 100.0
        assert continued.fills[0]["quantity"] == 5
        assert continued.fills[1]["price"] == 110.0
        assert continued.fills[1]["quantity"] == 5

        # The backend saw two execute() calls — one per fill.
        assert len(backend.calls) == 2
        # No duplicate entry in completed_orders: continue_order mutates
        # the same Order object recorded by process_signal.
        assert len(om.completed_orders) == 1
        assert om.completed_orders[0] is order


class TestContinueOrderRejectsNonPartial:
    """``continue_order`` raises on anything that isn't PARTIALLY_FILLED."""

    async def test_continue_order_raises_on_filled_order(
        self,
        cost_model: DefaultCostModel,
        risk_engine: RiskEngine,
        portfolio: Portfolio,
    ) -> None:
        # A backend that fills the whole order up front leaves no
        # residual to continue — calling continue_order must raise.
        backend = ScriptedBackend([FillResult(success=True, price=100.0, quantity=10)])
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(backend)

        signal = Signal.buy(symbol="AAPL", strategy_id="strat", quantity=10)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.FILLED

        with pytest.raises(ValueError, match="partially_filled"):
            await om.continue_order(order.id)

        # No extra fill attempt was made against the completed order.
        assert len(backend.calls) == 1


class TestContinueOrderMaxRetriesGuard:
    """The ``max_retries`` guard prevents infinite loops when a backend
    always under-fills."""

    async def test_max_retries_caps_loop_on_pathological_backend(
        self,
        cost_model: DefaultCostModel,
        risk_engine: RiskEngine,
        portfolio: Portfolio,
    ) -> None:
        # A backend that always fills exactly 1 share, regardless of the
        # requested residual. With max_retries=3 the loop must stop
        # after 3 continuation attempts even though the order is still
        # PARTIALLY_FILLED.
        one_share = FillResult(success=True, price=100.0, quantity=1)
        # process_signal gets the first 1-share fill; then up to 3
        # continuation attempts each yield 1 share.
        backend = ScriptedBackend([one_share, one_share, one_share, one_share])
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(backend)

        signal = Signal.buy(symbol="AAPL", strategy_id="strat", quantity=100)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.PARTIALLY_FILLED

        continued = await om.continue_order(order.id, max_retries=3)

        # 1 initial fill + 3 continuation attempts == 4 backend calls.
        assert len(backend.calls) == 4
        # Order is still partial: 1 + 3 == 4 of 100 filled.
        assert continued.fill_quantity == 4
        assert continued.status == OrderStatus.PARTIALLY_FILLED


class TestContinueOrderUnknownAndUnconfigured:
    """Edge cases: unknown id and missing execution backend."""

    async def test_continue_order_raises_on_unknown_id(
        self,
        cost_model: DefaultCostModel,
        risk_engine: RiskEngine,
        portfolio: Portfolio,
    ) -> None:
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(
            ScriptedBackend([FillResult(success=True, price=100.0, quantity=1)])
        )
        with pytest.raises(ValueError, match="Unknown order"):
            await om.continue_order("does-not-exist")

    async def test_continue_order_raises_when_no_backend(
        self,
        cost_model: DefaultCostModel,
        risk_engine: RiskEngine,
        portfolio: Portfolio,
    ) -> None:
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        # No execution backend wired.

        # Inject a PARTIALLY_FILLED order directly into completed_orders
        # so continue_order can find it, then assert it refuses to run
        # without a backend.
        from engine.core.order_manager import Order
        from engine.core.signal import Side

        order = Order(
            signal_id="s",
            strategy_id="strat",
            symbol="AAPL",
            side=Side.BUY,
            quantity=10,
        )
        order.fill_quantity = 5
        order.fill_price = 100.0
        order.transition(OrderStatus.PARTIALLY_FILLED)
        om.completed_orders.append(order)

        with pytest.raises(ValueError, match="No execution backend"):
            await om.continue_order(order.id)
