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

        # Order is sized for 20 shares so b2's 20-share fill does not trip
        # the overfill clamp. (b1 would fill 10 @ 100 and the order would
        # land PARTIALLY_FILLED — both assertions below distinguish b2 as
        # the active backend: b2 is the only one that fills the full 20
        # @ 200.)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=20)
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
    """Verifies that partial fills accumulate into ``fill_quantity``, that
    the FILLED vs PARTIALLY_FILLED status is decided by the cumulative
    total against ``order.quantity``, and that the portfolio is charged
    only for the quantity actually filled in each round."""

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

    def test_first_partial_fill_is_partially_filled(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=4), breakdown)

        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 4
        assert order.fill_price == 100.0
        assert order.filled_at is not None

    def test_two_partial_fills_accumulate_to_filled(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        # Round 1: 4 of 10 — partial.
        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=4), breakdown)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 4

        # Round 2: remaining 6 — cumulative reaches requested quantity.
        om._reconcile_fill(order, FillResult(success=True, price=101.0, quantity=6), breakdown)
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10
        # fill_price is the VWAP across both rounds, NOT the most recent
        # execution price: (4*100 + 6*101) / 10 = 100.6.
        assert order.fill_price == pytest.approx(100.6)

    def test_partial_fills_scale_cost_proportionally(
        self, cost_model, risk_engine, portfolio
    ):
        """The portfolio must be charged only for the shares actually
        filled in each round, not the full pre-computed cost breakdown."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)
        full_cost = breakdown.total.amount
        assert full_cost > 0

        cash_before = portfolio.cash
        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=4), breakdown)
        cash_after_round_1 = portfolio.cash

        # Round 1 should deduct only the 4/10 share of total cost (plus
        # the 4 shares * price). Allow a small float tolerance.
        expected_round_1_cash_drop = (4 * 100.0) + (full_cost * 4 / 10)
        actual_round_1_cash_drop = cash_before - cash_after_round_1
        assert actual_round_1_cash_drop == pytest.approx(expected_round_1_cash_drop)

        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=6), breakdown)
        cash_after_round_2 = portfolio.cash

        expected_round_2_cash_drop = (6 * 100.0) + (full_cost * 6 / 10)
        actual_round_2_cash_drop = cash_after_round_1 - cash_after_round_2
        assert actual_round_2_cash_drop == pytest.approx(expected_round_2_cash_drop)

        # Cumulative cash drop equals a full-fill's worth of cost + shares,
        # so the partial-fill machinery doesn't lose or invent money.
        total_cash_drop = cash_before - cash_after_round_2
        assert total_cash_drop == pytest.approx((10 * 100.0) + full_cost)
        # Position size reflects the full order once both fills land.
        assert portfolio.positions["AAPL"].quantity == 10

    def test_partial_fill_then_exact_completion(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=3), breakdown)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=7), breakdown)
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10

    def test_overfill_clamps_to_filled(self, cost_model, risk_engine, portfolio):
        """A cumulative total that reaches or exceeds order.quantity should
        settle as FILLED, never PARTIALLY_FILLED."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=10), breakdown)
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10

    # ── VWAP behaviour ─

    def test_single_fill_vwap_equals_fill_price(self, cost_model, risk_engine, portfolio):
        """VWAP across a single fill collapses to that fill's price."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        om._reconcile_fill(order, FillResult(success=True, price=123.5, quantity=10), breakdown)
        assert order.fill_price == pytest.approx(123.5)

    def test_fill_price_is_vwap_not_last_price(self, cost_model, risk_engine, portfolio):
        """``order.fill_price`` must be the volume-weighted average of every
        round, not the most recent execution price."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=2), breakdown)
        assert order.fill_price == pytest.approx(100.0)

        om._reconcile_fill(order, FillResult(success=True, price=110.0, quantity=8), breakdown)
        # VWAP = (2*100 + 8*110) / 10 = 108.0 — NOT 110.0.
        assert order.fill_price == pytest.approx(108.0)
        assert order.fill_price != pytest.approx(110.0)

    def test_vwap_weighted_by_quantity_not_simple_average(
        self, cost_model, risk_engine, portfolio
    ):
        """Two fills at different prices in different sizes must produce a
        *quantity-weighted* average, not the simple arithmetic mean."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        # 1 share @ 100 then 9 shares @ 200 → VWAP = (100 + 1800) / 10 = 190,
        # which is NOT the simple mean of 100 and 200 (= 150).
        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=1), breakdown)
        om._reconcile_fill(order, FillResult(success=True, price=200.0, quantity=9), breakdown)
        assert order.fill_quantity == 10
        assert order.fill_price == pytest.approx(190.0)
        assert order.fill_price != pytest.approx(150.0)

    def test_vwap_three_rounds(self, cost_model, risk_engine, portfolio):
        """VWAP accumulates correctly across three or more rounds."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        # 3 @ 100, 3 @ 110, 4 @ 130 →
        # notional = 300 + 330 + 520 = 1150; VWAP = 1150 / 10 = 115.0
        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=3), breakdown)
        om._reconcile_fill(order, FillResult(success=True, price=110.0, quantity=3), breakdown)
        om._reconcile_fill(order, FillResult(success=True, price=130.0, quantity=4), breakdown)

        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10
        assert order.fill_price == pytest.approx(115.0)

    def test_vwap_reconstructs_prior_notional_from_running_value(
        self, cost_model, risk_engine, portfolio
    ):
        """Round 3 should combine its own notional with the *VWAP-derived*
        prior notional — proving the running ``fill_price`` field is being
        used as the source of truth, not the latest execution price."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=5), breakdown)
        # VWAP so far = 100.
        om._reconcile_fill(order, FillResult(success=True, price=120.0, quantity=2), breakdown)
        # VWAP so far = (5*100 + 2*120) / 7 = 740 / 7 ≈ 105.714
        intermediate_vwap = order.fill_price
        assert intermediate_vwap == pytest.approx(740 / 7)

        om._reconcile_fill(order, FillResult(success=True, price=130.0, quantity=3), breakdown)
        # Final VWAP must be (7 * intermediate_vwap + 3 * 130) / 10,
        # NOT (5*100 + 2*120 + 3*130) / 10 recomputed from raw prices —
        # both happen to be equal, but the path the production code takes
        # is the VWAP-reconstruction one.
        expected = (7 * intermediate_vwap + 3 * 130.0) / 10
        assert order.fill_price == pytest.approx(expected)
        assert order.fill_quantity == 10

    # ── Real overfill clamping ──

    def test_overfill_single_round_clamps_to_order_quantity(
        self, cost_model, risk_engine, portfolio
    ):
        """A single fill that exceeds ``order.quantity`` is clamped to
        ``order.quantity``: cumulative qty, status, and cash impact all
        reflect the clamped total, never the raw venue quantity."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)
        cash_before = portfolio.cash

        # Venue says 15 shares; only 10 were requested.
        om._reconcile_fill(
            order, FillResult(success=True, price=100.0, quantity=15), breakdown
        )

        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10  # clamped, not 15
        assert portfolio.positions["AAPL"].quantity == 10  # clamped, not 15
        # Cash must be debited for 10 shares only — not 15.
        # The portfolio.open_position call charges qty*price + cost; the
        # extra 5 shares * 100 = 500 must NOT have left the account.
        full_cost = breakdown.total.amount
        assert portfolio.cash == pytest.approx(cash_before - (10 * 100.0) - full_cost)

    def test_overfill_second_round_clamps_to_remaining(
        self, cost_model, risk_engine, portfolio
    ):
        """After a partial fill, a follow-up fill that exceeds the unfilled
        residual is clamped to that residual — the order still settles as
        FILLED and the portfolio never advances past ``order.quantity``."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        # 8 of 10 filled — partial.
        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=8), breakdown)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 8

        # Venue now returns 5 shares; only 2 remain unfilled.
        cash_before_round_2 = portfolio.cash
        om._reconcile_fill(
            order, FillResult(success=True, price=100.0, quantity=5), breakdown
        )

        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10  # 8 + clamped(2), not 8 + 5 = 13
        assert portfolio.positions["AAPL"].quantity == 10

        # Round 2 cost must reflect only the 2 effective shares: cost
        # component scales by 2/10, not 5/10; the share debit is 2*100.
        full_cost = breakdown.total.amount
        expected_round_2_debit = (2 * 100.0) + (full_cost * 2 / 10)
        assert (cash_before_round_2 - portfolio.cash) == pytest.approx(expected_round_2_debit)

    def test_overfill_does_not_inflate_cash_drop(
        self, cost_model, risk_engine, portfolio
    ):
        """Cumulative cash drop for an overfilled order must equal the
        cash drop of a clean, exact 10-share fill — the overfill must be
        invisible to the portfolio."""
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        full_cost = DefaultCostModel().estimate_total(
            symbol="AAPL", quantity=10, price=100.0, side="buy"
        ).total.amount

        # Clean reference: one exact 10-share fill.
        clean_portfolio = Portfolio(initial_cash=100_000.0)
        clean_om = OrderManager(
            cost_model=cost_model, risk_engine=re, portfolio=clean_portfolio
        )
        clean_order, clean_breakdown = self._build_buy_order(quantity=10, price=100.0)
        clean_om._reconcile_fill(
            clean_order, FillResult(success=True, price=100.0, quantity=10), clean_breakdown
        )

        # Overfill variant: 8 then 5 (clamped to 2).
        over_portfolio = Portfolio(initial_cash=100_000.0)
        over_om = OrderManager(
            cost_model=cost_model, risk_engine=re, portfolio=over_portfolio
        )
        over_order, over_breakdown = self._build_buy_order(quantity=10, price=100.0)
        over_om._reconcile_fill(
            over_order, FillResult(success=True, price=100.0, quantity=8), over_breakdown
        )
        over_om._reconcile_fill(
            over_order, FillResult(success=True, price=100.0, quantity=5), over_breakdown
        )

        assert clean_portfolio.cash == pytest.approx(over_portfolio.cash)
        assert full_cost > 0  # sanity: the comparison is meaningful

    def test_already_filled_order_drops_stray_late_fill(
        self, cost_model, risk_engine, portfolio
    ):
        """Once an order is FILLED, a stray late fill must be a no-op:
        no duplicate FILLED transition, no zero-share tax lot, no cash
        mutation, no portfolio change."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=10), breakdown)
        assert order.status == OrderStatus.FILLED
        transitions_before = len(order.status_history)
        cash_before = portfolio.cash
        lots_before = len(portfolio._tax_lots.get("AAPL", []))

        # Late duplicate fill after the order is already satisfied.
        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=3), breakdown)

        # Nothing advanced.
        assert order.fill_quantity == 10
        assert order.fill_price == pytest.approx(100.0)
        assert portfolio.cash == cash_before
        assert portfolio.positions["AAPL"].quantity == 10
        # No duplicate FILLED transition.
        assert len(order.status_history) == transitions_before
        # No zero-share tax lot created.
        assert len(portfolio._tax_lots.get("AAPL", [])) == lots_before

    def test_overfill_vwap_uses_effective_quantity(
        self, cost_model, risk_engine, portfolio
    ):
        """VWAP must weight by the *clamped* effective quantity, so an
        overfill does not bias the average price toward its slice."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order, breakdown = self._build_buy_order(quantity=10, price=100.0)

        # 4 @ 100, then an overfill of 10 @ 200 that clamps to 6.
        om._reconcile_fill(order, FillResult(success=True, price=100.0, quantity=4), breakdown)
        om._reconcile_fill(order, FillResult(success=True, price=200.0, quantity=10), breakdown)

        assert order.fill_quantity == 10
        # VWAP = (4*100 + 6*200) / 10 = 160.0 — the 4 overfill shares at
        # 200 are dropped entirely, NOT (4*100 + 10*200) / 14.
        assert order.fill_price == pytest.approx(160.0)

    def test_sell_partial_fill_accumulates_and_scales_cost(
        self, cost_model, risk_engine, portfolio
    ):
        """Symmetric check on the SELL path: the non-tax cost and tax
        components are both scaled by the per-round fill ratio."""
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
        om._reconcile_fill(order, FillResult(success=True, price=120.0, quantity=5), breakdown)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.fill_quantity == 5
        # Selling brings cash in: proceeds (5*120) minus scaled cost.
        assert portfolio.cash > cash_before

        om._reconcile_fill(order, FillResult(success=True, price=120.0, quantity=5), breakdown)
        assert order.status == OrderStatus.FILLED
        assert order.fill_quantity == 10
        assert "AAPL" not in portfolio.positions

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
