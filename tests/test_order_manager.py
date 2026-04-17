"""Tests for OrderManager — signal → validate → cost → risk → execute → reconcile."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engine.core.cost_model import DefaultCostModel
from engine.core.execution.base import FillResult
from engine.core.order_manager import OrderManager, OrderStatus
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order


class FakeExecutionBackend:
    def __init__(self, success: bool = True, price: float = 100.0, quantity: int = 10):
        self._success = success
        self._price = price
        self._quantity = quantity

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def execute(self, order: Order, market_price: float, costs: CostBreakdown) -> FillResult:
        if self._success:
            return FillResult(success=True, price=self._price, quantity=self._quantity)
        return FillResult(success=False, reason="Simulated failure")


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(initial_cash=100_000.0)


@pytest.fixture
def cost_model() -> DefaultCostModel:
    return DefaultCostModel()


@pytest.fixture
def risk_engine() -> RiskEngine:
    return RiskEngine()


@pytest.fixture
def order_manager(cost_model, risk_engine, portfolio) -> OrderManager:
    om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
    om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))
    return om


class TestBuySignalPipeline:
    async def test_buy_signal_creates_filled_order(self, order_manager):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await order_manager.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.FILLED
        assert order.symbol == "AAPL"
        assert order.side == Side.BUY
        assert order.fill_price == 150.0
        assert order.fill_quantity == 10
        assert order.cost_breakdown is not None

    async def test_buy_updates_portfolio_position(self, order_manager, portfolio):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await order_manager.process_signal(signal, market_price=150.0)

        assert "AAPL" in portfolio.positions
        assert portfolio.positions["AAPL"].quantity == 10

    async def test_buy_deducts_cash(self, order_manager, portfolio):
        initial_cash = portfolio.cash
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await order_manager.process_signal(signal, market_price=150.0)

        assert portfolio.cash < initial_cash


class TestSellSignalPipeline:
    async def test_sell_signal_closes_position(self, order_manager, portfolio):
        portfolio.open_position("AAPL", 10, 100.0)

        signal = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        order = await order_manager.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.FILLED
        assert order.side == Side.SELL

    async def test_sell_calculates_pnl(self, order_manager, portfolio):
        portfolio.open_position("AAPL", 10, 100.0)

        signal = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        await order_manager.process_signal(signal, market_price=150.0)

        assert portfolio.realized_pnl != 0


class TestValidation:
    async def test_signal_exceeds_cash_rejected(self, order_manager):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10_000)
        order = await order_manager.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.REJECTED
        assert order.quantity > 0

    async def test_sell_more_than_held_rejected(self, order_manager, portfolio):
        portfolio.open_position("AAPL", 5, 100.0)
        signal = Signal.sell(symbol="AAPL", strategy_id="test", quantity=100)
        order = await order_manager.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.REJECTED

    async def test_sell_without_position_rejected(self, order_manager):
        signal = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        order = await order_manager.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.REJECTED


class TestCostRejection:
    async def test_cost_exceeds_max_cost_pct_rejected(self, cost_model, portfolio):
        risk_engine = RiskEngine()
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend())

        signal = Signal.buy(
            symbol="AAPL",
            strategy_id="test",
            quantity=10,
            max_cost_pct=0.00001,
        )
        order = await om.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.RISK_REJECTED


class TestRiskRejection:
    async def test_risk_engine_rejects_order(self, cost_model, portfolio):
        risk_engine = RiskEngine(max_daily_trades=0)
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend())

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.RISK_REJECTED


class TestNoExecutionBackend:
    async def test_no_backend_returns_failed(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=1)
        order = await om.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.FAILED


class TestExecutionFailure:
    async def test_backend_failure_returns_failed(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=False))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(signal, market_price=150.0)

        assert order.status == OrderStatus.FAILED


class TestStatusHistory:
    async def test_order_has_status_transitions(self, order_manager):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await order_manager.process_signal(signal, market_price=150.0)

        statuses = [h["to"] for h in order.status_history]
        assert OrderStatus.VALIDATED in statuses
        assert OrderStatus.COSTED in statuses
        assert OrderStatus.RISK_APPROVED in statuses
        assert OrderStatus.FILLED in statuses


class TestWeightBasedQuantity:
    async def test_weight_signal_calculates_quantity(self, cost_model, portfolio):
        risk_engine = RiskEngine(max_position_pct=1.0)
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=100.0, quantity=500))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        order = await om.process_signal(signal, market_price=100.0)

        assert order.status == OrderStatus.FILLED
        assert order.quantity > 0
        assert order.quantity == int(100_000.0 * 0.5 // 100.0)

    async def test_zero_price_signal_rejected(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend())

        signal = Signal.buy(symbol="AAPL", strategy_id="test")
        order = await om.process_signal(signal, market_price=0.0)

        assert order.status == OrderStatus.REJECTED


class TestCompletedOrders:
    async def test_filled_order_added_to_completed(self, order_manager):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await order_manager.process_signal(signal, market_price=150.0)

        assert len(order_manager.completed_orders) == 1

    async def test_rejected_order_not_in_completed(self, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10_000)
        await om.process_signal(signal, market_price=150.0)

        assert len(om.completed_orders) == 0
