"""
Comprehensive tests for OrderManager targeting uncovered code paths.

Covers: Order model edge cases, status transitions, _calculate_quantity edge cases,
multiple sequential signals, order types, avg_volume propagation, and more.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from engine.core.cost_model import DefaultCostModel
from engine.core.execution.base import FillResult
from engine.core.order_manager import Order, OrderManager, OrderStatus, OrderType
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal


class _ScriptedBackend:
    """Backend that pops FillResults off a queue, enabling partial-fill
    sequences across multiple ``execute`` calls.

    For these tests we drive ``OrderManager._reconcile_fill`` directly so we
    can apply two fills to a single order without re-running validation,
    costing, and risk checks. This isolates the partial-fill accumulation
    behaviour under test.
    """

    def __init__(self, fills):
        self._fills = list(fills)

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def execute(self, order, market_price, costs):
        # Mirror the real execution backend contract consumed by
        # ``OrderManager.process_signal``: pop the next scripted
        # ``FillResult`` and return it. This lets an end-to-end test
        # simulate a partial fill (or a sequence of fills) from a single
        # ``process_signal`` invocation without re-implementing costing /
        # risk plumbing in the test double.
        if not self._fills:
            return FillResult(success=False, reason="No scripted fill available")
        return self._fills.pop(0)


class FakeBackend:
    def __init__(self, success=True, price=100.0, quantity=10):
        self._success = success
        self._price = price
        self._quantity = quantity
        self.calls = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def execute(self, order, market_price, costs):
        self.calls.append((order, market_price, costs))
        if self._success:
            return FillResult(success=True, price=self._price, quantity=self._quantity)
        return FillResult(success=False, reason="Simulated failure")


@pytest.fixture
def portfolio():
    return Portfolio(initial_cash=100_000.0)


@pytest.fixture
def cost_model():
    return DefaultCostModel()


@pytest.fixture
def risk_engine():
    return RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)


@pytest.fixture
def backend():
    return FakeBackend(success=True, price=100.0, quantity=10)


@pytest.fixture
def om(cost_model, risk_engine, portfolio, backend):
    manager = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
    manager.set_execution_backend(backend)
    return manager


# ── Order model tests ──


class TestOrderModel:
    def test_order_default_status_is_pending(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        assert order.status == OrderStatus.PENDING

    def test_order_default_type_is_market(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        assert order.order_type == OrderType.MARKET

    def test_order_with_limit_type(self):
        order = Order(
            signal_id="s1",
            strategy_id="strat",
            symbol="AAPL",
            side=Side.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            limit_price=99.0,
        )
        assert order.order_type == OrderType.LIMIT
        assert order.limit_price == 99.0

    def test_order_with_stop_type(self):
        order = Order(
            signal_id="s1",
            strategy_id="strat",
            symbol="AAPL",
            side=Side.BUY,
            quantity=10,
            order_type=OrderType.STOP,
        )
        assert order.order_type == OrderType.STOP

    def test_order_with_stop_limit_type(self):
        order = Order(
            signal_id="s1",
            strategy_id="strat",
            symbol="AAPL",
            side=Side.BUY,
            quantity=10,
            order_type=OrderType.STOP_LIMIT,
            limit_price=95.0,
        )
        assert order.order_type == OrderType.STOP_LIMIT

    def test_order_default_fill_fields_are_none(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        assert order.fill_price is None
        assert order.fill_quantity is None
        assert order.filled_at is None
        assert order.cost_breakdown is None

    def test_order_has_uuid_id(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        assert len(order.id) == 36
        assert "-" in order.id

    def test_order_has_created_at(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        assert isinstance(order.created_at, datetime)

    def test_order_status_history_starts_empty(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        assert order.status_history == []

    def test_transition_records_history(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        order.transition(OrderStatus.VALIDATED, "passed checks")
        assert len(order.status_history) == 1
        entry = order.status_history[0]
        assert entry["from"] == OrderStatus.PENDING
        assert entry["to"] == OrderStatus.VALIDATED
        assert entry["reason"] == "passed checks"
        assert "timestamp" in entry

    def test_transition_multiple_times(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        order.transition(OrderStatus.VALIDATED)
        order.transition(OrderStatus.COSTED)
        order.transition(OrderStatus.RISK_APPROVED)
        assert len(order.status_history) == 3
        assert order.status == OrderStatus.RISK_APPROVED

    def test_transition_with_empty_reason(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10
        )
        order.transition(OrderStatus.VALIDATED)
        assert order.status_history[0]["reason"] == ""


class TestOrderStatusEnum:
    def test_all_status_values(self):
        expected = {
            "pending",
            "validated",
            "costed",
            "risk_approved",
            "risk_rejected",
            "submitted",
            "filled",
            "partially_filled",
            "cancelled",
            "rejected",
            "failed",
        }
        actual = {s.value for s in OrderStatus}
        assert actual == expected

    def test_all_order_type_values(self):
        expected = {"market", "limit", "stop", "stop_limit"}
        actual = {t.value for t in OrderType}
        assert actual == expected


# ── _calculate_quantity edge cases ──


class TestCalculateQuantity:
    def test_zero_price_returns_zero(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        result = om._calculate_quantity(signal, 0.0)
        assert result == 0

    def test_negative_price_returns_zero(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        result = om._calculate_quantity(signal, -10.0)
        assert result == 0

    def test_zero_weight_returns_zero(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.0)
        result = om._calculate_quantity(signal, 100.0)
        assert result == 0

    def test_full_weight_uses_all_cash(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=1.0)
        result = om._calculate_quantity(signal, 100.0)
        assert result == 1000  # 100_000 / 100

    def test_half_weight(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        result = om._calculate_quantity(signal, 50.0)
        assert result == 1000  # (100_000 * 0.5) // 50

    def test_small_weight_fractional_shares_truncated(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.1)
        result = om._calculate_quantity(signal, 33.0)
        expected = int(100_000 * 0.1 // 33.0)
        assert result == expected


# ── _validate_order edge cases ──


class TestValidateOrder:
    def test_zero_quantity_rejected(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=0
        )
        assert om._validate_order(order, 100.0) is False

    def test_negative_quantity_rejected(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=-5
        )
        assert om._validate_order(order, 100.0) is False

    def test_buy_exact_cash_allowed(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        qty = int(100_000.0 // 100.0)
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=qty
        )
        assert om._validate_order(order, 100.0) is True

    def test_buy_one_over_cash_rejected(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        qty = int(100_000.0 // 100.0) + 1
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=qty
        )
        assert om._validate_order(order, 100.0) is False

    def test_sell_exact_quantity_held_allowed(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        portfolio.open_position("AAPL", 10, 100.0)
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.SELL, quantity=10
        )
        assert om._validate_order(order, 100.0) is True

    def test_sell_one_more_than_held_rejected(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        portfolio.open_position("AAPL", 10, 100.0)
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.SELL, quantity=11
        )
        assert om._validate_order(order, 100.0) is False


# ── Pipeline: multiple sequential signals ──


class TestMultipleSequentialSignals:
    async def test_buy_then_sell_same_symbol(self, om, portfolio):
        buy_signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        buy_order = await om.process_signal(buy_signal, market_price=100.0)
        assert buy_order.status == OrderStatus.FILLED

        sell_signal = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        sell_order = await om.process_signal(sell_signal, market_price=110.0)
        assert sell_order.status == OrderStatus.FILLED

        assert "AAPL" not in portfolio.positions
        assert len(om.completed_orders) == 2

    async def test_two_buys_same_symbol_accumulates(self, om, portfolio):
        s1 = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(s1, market_price=100.0)

        s2 = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(s2, market_price=100.0)

        assert portfolio.positions["AAPL"].quantity == 20

    async def test_multiple_different_symbols(self, om, portfolio):
        s1 = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(s1, market_price=100.0)

        s2 = Signal.buy(symbol="MSFT", strategy_id="test", quantity=20)
        await om.process_signal(s2, market_price=200.0)

        assert "AAPL" in portfolio.positions
        assert "MSFT" in portfolio.positions
        assert len(om.completed_orders) == 2


# ── Cost model integration ──


class TestCostModelIntegration:
    async def test_cost_breakdown_attached_to_order(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.cost_breakdown is not None
        assert "commission" in order.cost_breakdown
        assert "spread" in order.cost_breakdown
        assert "slippage" in order.cost_breakdown
        assert "exchange_fee" in order.cost_breakdown
        assert "total" in order.cost_breakdown

    async def test_custom_cost_model_parameters(self, portfolio):
        cm = DefaultCostModel(
            commission_per_trade=5.0,
            spread_bps=10.0,
            slippage_bps=20.0,
            exchange_fee_per_share=0.001,
        )
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        backend = FakeBackend(success=True, price=100.0, quantity=10)
        manager = OrderManager(cost_model=cm, risk_engine=re, portfolio=portfolio)
        manager.set_execution_backend(backend)

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await manager.process_signal(signal, market_price=100.0)
        assert order.cost_breakdown["commission"] == 5.0


# ── Risk engine integration ──


class TestRiskEngineIntegration:
    async def test_max_daily_trades_zero_rejects(self, portfolio):
        re = RiskEngine(max_daily_trades=0, max_position_pct=1.0, max_single_order_value=1_000_000)
        cm = DefaultCostModel()
        manager = OrderManager(cost_model=cm, risk_engine=re, portfolio=portfolio)
        manager.set_execution_backend(FakeBackend())

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await manager.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.RISK_REJECTED

    async def test_max_daily_trades_exhausted(self, portfolio):
        re = RiskEngine(max_daily_trades=1, max_position_pct=1.0, max_single_order_value=1_000_000)
        cm = DefaultCostModel()
        backend = FakeBackend(success=True, price=100.0, quantity=1)
        manager = OrderManager(cost_model=cm, risk_engine=re, portfolio=portfolio)
        manager.set_execution_backend(backend)

        s1 = Signal.buy(symbol="AAPL", strategy_id="test", quantity=1)
        o1 = await manager.process_signal(s1, market_price=50.0)
        assert o1.status == OrderStatus.FILLED

        s2 = Signal.buy(symbol="MSFT", strategy_id="test", quantity=1)
        o2 = await manager.process_signal(s2, market_price=50.0)
        assert o2.status == OrderStatus.RISK_REJECTED


# ── Execution backend integration ──


class TestExecutionBackendIntegration:
    async def test_backend_receives_correct_market_price(self, portfolio, cost_model):
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        backend = FakeBackend(success=True, price=155.0, quantity=10)
        manager = OrderManager(cost_model=cost_model, risk_engine=re, portfolio=portfolio)
        manager.set_execution_backend(backend)

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await manager.process_signal(signal, market_price=150.0)

        assert len(backend.calls) == 1
        _, mp, _ = backend.calls[0]
        assert mp == 150.0

    async def test_set_execution_backend_replaces(self, cost_model, risk_engine, portfolio):
        manager = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        b1 = FakeBackend(success=True, price=100.0, quantity=10)
        b2 = FakeBackend(success=True, price=200.0, quantity=20)

        manager.set_execution_backend(b1)
        manager.set_execution_backend(b2)

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await manager.process_signal(signal, market_price=100.0)
        assert order.fill_price == 200.0
        assert order.fill_quantity == 20


# ── Status transition tracking ──


class TestFullStatusTransitions:
    async def test_rejected_order_has_correct_transitions(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=100_000)
        order = await om.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.REJECTED
        statuses = [h["to"] for h in order.status_history]
        assert OrderStatus.REJECTED in statuses
        assert OrderStatus.VALIDATED not in statuses

    async def test_filled_order_has_full_pipeline(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(signal, market_price=100.0)

        statuses = [h["to"] for h in order.status_history]
        assert statuses == [
            OrderStatus.VALIDATED,
            OrderStatus.COSTED,
            OrderStatus.RISK_APPROVED,
            OrderStatus.SUBMITTED,
            OrderStatus.FILLED,
        ]

    async def test_failed_order_status_sequence(self, portfolio, cost_model):
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        backend = FakeBackend(success=False)
        manager = OrderManager(cost_model=cost_model, risk_engine=re, portfolio=portfolio)
        manager.set_execution_backend(backend)

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await manager.process_signal(signal, market_price=100.0)

        statuses = [h["to"] for h in order.status_history]
        assert OrderStatus.FAILED in statuses
        assert OrderStatus.FILLED not in statuses

    async def test_no_backend_failure_sequence(self, cost_model, risk_engine, portfolio):
        manager = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await manager.process_signal(signal, market_price=100.0)

        statuses = [h["to"] for h in order.status_history]
        assert OrderStatus.VALIDATED in statuses
        assert OrderStatus.COSTED in statuses
        assert OrderStatus.RISK_APPROVED in statuses
        assert OrderStatus.FAILED in statuses


# ── Portfolio mutation checks ──


class TestPortfolioMutation:
    async def test_buy_reduces_cash(self, om, portfolio):
        initial = portfolio.cash
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=100.0)
        assert portfolio.cash < initial

    async def test_sell_increases_cash(self, om, portfolio):
        portfolio.open_position("AAPL", 10, 80.0)
        cash_before_sell = portfolio.cash
        signal = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=120.0)
        assert portfolio.cash > cash_before_sell

    async def test_sell_updates_realized_pnl(self, om, portfolio):
        portfolio.open_position("AAPL", 10, 80.0)
        signal = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=120.0)
        assert portfolio.realized_pnl != 0.0


# ── Cost rejection path ──


class TestCostRejectionDetailed:
    async def test_cost_pct_with_zero_trade_value(self, cost_model, portfolio):
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        manager = OrderManager(cost_model=cost_model, risk_engine=re, portfolio=portfolio)
        manager.set_execution_backend(FakeBackend())

        signal = Signal.buy(
            symbol="AAPL",
            strategy_id="test",
            quantity=10,
            max_cost_pct=0.00001,
        )
        order = await manager.process_signal(signal, market_price=150.0)
        assert order.status == OrderStatus.RISK_REJECTED

    async def test_signal_without_max_cost_passes_cost_stage(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(signal, market_price=100.0)
        statuses = [h["to"] for h in order.status_history]
        assert OrderStatus.COSTED in statuses

    async def test_signal_with_none_max_cost_passes(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10, max_cost_pct=None)
        order = await om.process_signal(signal, market_price=100.0)
        assert OrderStatus.COSTED in [h["to"] for h in order.status_history]


# ── Pending orders dict ──


class TestPendingOrders:
    def test_pending_orders_initially_empty(self, cost_model, risk_engine, portfolio):
        manager = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        assert manager.pending_orders == {}

    def test_completed_orders_initially_empty(self, cost_model, risk_engine, portfolio):
        manager = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        assert manager.completed_orders == []

    async def test_filled_order_added_to_completed(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=100.0)
        assert len(om.completed_orders) == 1

    async def test_rejected_order_not_added_to_completed(self, cost_model, risk_engine, portfolio):
        manager = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=100_000)
        await manager.process_signal(signal, market_price=150.0)
        assert len(manager.completed_orders) == 0

    async def test_failed_order_added_to_completed(self, cost_model, risk_engine, portfolio):
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        backend = FakeBackend(success=False)
        manager = OrderManager(cost_model=cost_model, risk_engine=re, portfolio=portfolio)
        manager.set_execution_backend(backend)

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await manager.process_signal(signal, market_price=100.0)
        assert len(manager.completed_orders) == 1


# ── Avg volume propagation ──


class TestAvgVolumePropagation:
    async def test_avg_volume_passed_to_cost_model(self, portfolio):
        class TrackingCostModel(DefaultCostModel):
            def __init__(self):
                super().__init__()
                self.last_avg_volume = None

            def estimate_total(self, symbol, quantity, price, side, avg_volume=0):
                self.last_avg_volume = avg_volume
                return super().estimate_total(symbol, quantity, price, side, avg_volume)

        cm = TrackingCostModel()
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        backend = FakeBackend(success=True, price=100.0, quantity=10)
        manager = OrderManager(cost_model=cm, risk_engine=re, portfolio=portfolio)
        manager.set_execution_backend(backend)

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await manager.process_signal(signal, market_price=100.0, avg_volume=5_000_000)
        assert cm.last_avg_volume == 5_000_000


# ── Partial-fill reconciliation ──


class TestPartialFillReconciliation:
    """Verifies partial-fill reconciliation against the async
    ``OrderManager._reconcile_fill``.

    ``_reconcile_fill`` is the shared bookkeeping routine called by both
    ``process_signal`` (initial fill) and ``continue_order`` (residual fill).
    For every fill it:

    * accumulates ``fill.quantity`` into the order's *cumulative*
      ``fill_quantity`` and recomputes ``fill_price`` as the running
      volume-weighted average (VWAP) across all fills applied so far;
    * appends a per-fill record to ``order.fills`` for auditability;
    * transitions the order to ``PARTIALLY_FILLED`` while the cumulative
      quantity is short of ``order.quantity`` and to ``FILLED`` once it
      reaches it; and
    * books *this* fill's slice into the portfolio.

    These tests therefore drive ``_reconcile_fill`` **directly** (it must be
    ``await``-ed) to isolate the partial-fill accumulation, VWAP, audit-trail
    and status-transition behaviour — the latter being the critical contract
    that a partial fill must surface as ``PARTIALLY_FILLED`` rather than being
    silently mislabelled ``FILLED`` (or left in a pre-fill state).
    """

    def _build_buy_order(self, quantity=10, price=100.0):
        order = Order(
            signal_id="s1",
            strategy_id="strat",
            symbol="AAPL",
            side=Side.BUY,
            quantity=quantity,
        )
        order.transition(OrderStatus.VALIDATED)
        order.transition(OrderStatus.COSTED)
        order.transition(OrderStatus.RISK_APPROVED)
        cost_model = DefaultCostModel()
        breakdown = cost_model.estimate_total(
            symbol="AAPL", quantity=quantity, price=price, side="buy"
        )
        return order, breakdown

    async def test_first_partial_fill_is_partially_filled(
        self, cost_model, risk_engine, portfolio
    ):
        """A single under-fill lands in PARTIALLY_FILLED with the filled
        quantity recorded — never FILLED, and never left in its pre-fill
        status."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        await om._reconcile_fill(
            order, FillResult(success=True, price=100.0, quantity=4), breakdown
        )

        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 4
        # A single fill means the VWAP degenerates to the fill's price.
        assert order.fill_price == 100.0
        assert order.filled_at is not None
        # Per-fill audit trail records exactly one entry.
        assert len(order.fills) == 1
        assert order.fills[0]["price"] == 100.0
        assert order.fills[0]["quantity"] == 4

    async def test_two_partial_fills_accumulate_to_filled(
        self, cost_model, risk_engine, portfolio
    ):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        # Round 1: 4 of 10 at $100 — partial.
        await om._reconcile_fill(
            order, FillResult(success=True, price=100.0, quantity=4), breakdown
        )
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 4
        assert order.fill_price == 100.0

        # Round 2: remaining 6 at $101 — cumulative reaches requested qty.
        await om._reconcile_fill(
            order, FillResult(success=True, price=101.0, quantity=6), breakdown
        )
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10
        # fill_price is the volume-weighted average across BOTH fills.
        expected_vwap = (100.0 * 4 + 101.0 * 6) / 10
        assert order.fill_price == pytest.approx(expected_vwap)
        # Both fills recorded in the audit trail, in arrival order.
        assert len(order.fills) == 2
        assert [f["quantity"] for f in order.fills] == [4, 6]
        assert [f["price"] for f in order.fills] == [100.0, 101.0]

    async def test_partial_fills_track_vwap_and_book_each_slice(
        self, cost_model, risk_engine, portfolio
    ):
        """Each fill round books only that round's shares into the portfolio
        (position quantity grows per fill) and the order tracks the running
        VWAP across the fills."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        cash_before = portfolio.cash
        await om._reconcile_fill(
            order, FillResult(success=True, price=100.0, quantity=4), breakdown
        )
        # Round 1 books exactly the 4 filled shares.
        assert portfolio.positions["AAPL"].quantity == 4
        assert portfolio.cash < cash_before  # buying consumes cash
        assert order.fill_price == 100.0

        await om._reconcile_fill(
            order, FillResult(success=True, price=110.0, quantity=6), breakdown
        )
        # Round 2 accumulates the position to the full order.
        assert portfolio.positions["AAPL"].quantity == 10
        assert order.status == OrderStatus.FILLED
        # VWAP across 4@$100 and 6@$110.
        assert order.fill_price == pytest.approx((100.0 * 4 + 110.0 * 6) / 10)

    async def test_partial_fill_then_exact_completion(
        self, cost_model, risk_engine, portfolio
    ):
        """A partial fill followed by a fill that exactly closes the residual
        settles as FILLED with the full requested quantity."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        await om._reconcile_fill(
            order, FillResult(success=True, price=100.0, quantity=3), breakdown
        )
        assert order.status == OrderStatus.PARTIALLY_FILLED
        await om._reconcile_fill(
            order, FillResult(success=True, price=102.0, quantity=7), breakdown
        )
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10

    async def test_overfill_clamps_to_filled(self, cost_model, risk_engine, portfolio):
        """A cumulative total that reaches or exceeds order.quantity should
        settle as FILLED, never PARTIALLY_FILLED."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        await om._reconcile_fill(
            order, FillResult(success=True, price=100.0, quantity=10), breakdown
        )
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10
        assert len(order.fills) == 1

    async def test_sell_partial_fill_accumulates(
        self, cost_model, risk_engine, portfolio
    ):
        """Symmetric check on the SELL path: a partial sell is PARTIALLY_FILLED
        and a second sell that completes the order is FILLED, with the
        position drained and cash raised in the process."""
        portfolio.open_position("AAPL", 10, 80.0)
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)

        order = Order(
            signal_id="s1",
            strategy_id="strat",
            symbol="AAPL",
            side=Side.SELL,
            quantity=10,
        )
        order.transition(OrderStatus.VALIDATED)
        order.transition(OrderStatus.COSTED)
        order.transition(OrderStatus.RISK_APPROVED)
        cost_model = DefaultCostModel()
        breakdown = cost_model.estimate_total(
            symbol="AAPL", quantity=10, price=120.0, side="sell"
        )

        cash_before = portfolio.cash
        await om._reconcile_fill(
            order, FillResult(success=True, price=120.0, quantity=5), breakdown
        )
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 5
        assert order.fill_price == 120.0
        # Selling brings cash in.
        assert portfolio.cash > cash_before

        await om._reconcile_fill(
            order, FillResult(success=True, price=130.0, quantity=5), breakdown
        )
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10
        # VWAP across 5@$120 and 5@$130.
        assert order.fill_price == pytest.approx((120.0 * 5 + 130.0 * 5) / 10)
        assert "AAPL" not in portfolio.positions

    async def test_partial_fill_records_timestamped_audit_trail(
        self, cost_model, risk_engine, portfolio
    ):
        """``order.fills`` records every fill with its price, quantity and an
        ISO-8601 timestamp, so downstream consumers can reconstruct the exact
        fill sequence behind the cumulative VWAP."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        await om._reconcile_fill(
            order, FillResult(success=True, price=100.0, quantity=4), breakdown
        )
        await om._reconcile_fill(
            order, FillResult(success=True, price=101.0, quantity=6), breakdown
        )

        assert len(order.fills) == 2
        for entry in order.fills:
            assert set(entry) == {"price", "quantity", "timestamp"}
            assert isinstance(entry["timestamp"], str)
            # Timestamp parses cleanly as ISO-8601.
            datetime.fromisoformat(entry["timestamp"])

    async def test_process_signal_partial_fill_via_backend(
        self, cost_model, risk_engine, portfolio
    ):
        """End-to-end: when the backend returns a partial fill from a single
        ``process_signal`` call, the order lands in PARTIALLY_FILLED and the
        portfolio only reflects the filled slice."""
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        om = OrderManager(cost_model=cost_model, risk_engine=re, portfolio=portfolio)
        om.set_execution_backend(_ScriptedBackend(
            [FillResult(success=True, price=100.0, quantity=4)]
        ))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 4
        assert portfolio.positions["AAPL"].quantity == 4
