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
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        assert order.status == OrderStatus.PENDING

    def test_order_default_type_is_market(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        assert order.order_type == OrderType.MARKET

    def test_order_with_limit_type(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL",
            side=Side.BUY, quantity=10, order_type=OrderType.LIMIT, limit_price=99.0,
        )
        assert order.order_type == OrderType.LIMIT
        assert order.limit_price == 99.0

    def test_order_with_stop_type(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL",
            side=Side.BUY, quantity=10, order_type=OrderType.STOP,
        )
        assert order.order_type == OrderType.STOP

    def test_order_with_stop_limit_type(self):
        order = Order(
            signal_id="s1", strategy_id="strat", symbol="AAPL",
            side=Side.BUY, quantity=10, order_type=OrderType.STOP_LIMIT, limit_price=95.0,
        )
        assert order.order_type == OrderType.STOP_LIMIT

    def test_order_default_fill_fields_are_none(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        assert order.fill_price is None
        assert order.fill_quantity is None
        assert order.filled_at is None
        assert order.cost_breakdown is None

    def test_order_has_uuid_id(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        assert len(order.id) == 36
        assert "-" in order.id

    def test_order_has_created_at(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        assert isinstance(order.created_at, datetime)

    def test_order_status_history_starts_empty(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        assert order.status_history == []

    def test_transition_records_history(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        order.transition(OrderStatus.VALIDATED, "passed checks")
        assert len(order.status_history) == 1
        entry = order.status_history[0]
        assert entry["from"] == OrderStatus.PENDING
        assert entry["to"] == OrderStatus.VALIDATED
        assert entry["reason"] == "passed checks"
        assert "timestamp" in entry

    def test_transition_multiple_times(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        order.transition(OrderStatus.VALIDATED)
        order.transition(OrderStatus.COSTED)
        order.transition(OrderStatus.RISK_APPROVED)
        assert len(order.status_history) == 3
        assert order.status == OrderStatus.RISK_APPROVED

    def test_transition_with_empty_reason(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        order.transition(OrderStatus.VALIDATED)
        assert order.status_history[0]["reason"] == ""


class TestOrderStatusEnum:
    def test_all_status_values(self):
        expected = {
            "pending", "validated", "costed", "risk_approved", "risk_rejected",
            "submitted", "filled", "partially_filled", "cancelled", "rejected", "failed",
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
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=0)
        assert om._validate_order(order, 100.0) is False

    def test_negative_quantity_rejected(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=-5)
        assert om._validate_order(order, 100.0) is False

    def test_buy_exact_cash_allowed(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        qty = int(100_000.0 // 100.0)
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=qty)
        assert om._validate_order(order, 100.0) is True

    def test_buy_one_over_cash_rejected(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        qty = int(100_000.0 // 100.0) + 1
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=qty)
        assert om._validate_order(order, 100.0) is False

    def test_sell_exact_quantity_held_allowed(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        portfolio.open_position("AAPL", 10, 100.0)
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.SELL, quantity=10)
        assert om._validate_order(order, 100.0) is True

    def test_sell_one_more_than_held_rejected(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        portfolio.open_position("AAPL", 10, 100.0)
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.SELL, quantity=11)
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
            symbol="AAPL", strategy_id="test", quantity=10, max_cost_pct=0.00001,
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
