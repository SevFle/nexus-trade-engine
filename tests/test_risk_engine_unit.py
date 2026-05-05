"""
Comprehensive unit tests for risk_engine.py — RiskCheckResult, RiskEngine
defaults, drawdown calculations, and edge cases not in test_risk_engine.py.
"""

from __future__ import annotations

import uuid

import pytest

from engine.core.order_manager import Order, OrderStatus
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskCheckResult, RiskEngine
from engine.core.signal import Side


def _make_order(
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    quantity: int = 10,
    strategy_id: str = "test",
) -> Order:
    return Order(
        signal_id=str(uuid.uuid4()),
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
    )


class TestRiskCheckResult:
    def test_approved_with_no_reason(self):
        r = RiskCheckResult(approved=True)
        assert r.approved is True
        assert r.reason == ""
        assert r.warnings == []

    def test_rejected_with_reason(self):
        r = RiskCheckResult(approved=False, reason="test reason")
        assert r.approved is False
        assert r.reason == "test reason"

    def test_warnings_default_to_empty(self):
        r = RiskCheckResult(approved=True)
        assert r.warnings == []

    def test_explicit_warnings(self):
        r = RiskCheckResult(approved=True, warnings=["warning1", "warning2"])
        assert len(r.warnings) == 2

    def test_none_warnings_becomes_empty_list(self):
        r = RiskCheckResult(approved=True, warnings=None)
        assert r.warnings == []


class TestRiskEngineDefaults:
    def test_default_max_position_pct(self):
        e = RiskEngine()
        assert e.max_position_pct == 0.20

    def test_default_max_portfolio_risk_pct(self):
        e = RiskEngine()
        assert e.max_portfolio_risk_pct == 0.25

    def test_default_max_open_positions(self):
        e = RiskEngine()
        assert e.max_open_positions == 50

    def test_default_circuit_breaker_drawdown(self):
        e = RiskEngine()
        assert e.circuit_breaker_drawdown_pct == 0.10

    def test_default_max_daily_trades(self):
        e = RiskEngine()
        assert e.max_daily_trades == 100

    def test_default_max_single_order_value(self):
        e = RiskEngine()
        assert e.max_single_order_value == 50_000.0

    def test_circuit_breaker_starts_false(self):
        e = RiskEngine()
        assert e.circuit_breaker_active is False

    def test_daily_trade_count_starts_zero(self):
        e = RiskEngine()
        assert e.daily_trade_count == 0


class TestRiskEngineDrawdown:
    def test_no_drawdown_at_start(self):
        p = Portfolio(initial_cash=100_000.0)
        e = RiskEngine()
        assert e._calculate_drawdown(p) == 0.0

    def test_drawdown_with_loss(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 100, 500.0)
        p.update_prices({"AAPL": 400.0})
        e = RiskEngine()
        dd = e._calculate_drawdown(p)
        assert dd > 0

    def test_no_drawdown_with_gain(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 200.0})
        e = RiskEngine()
        dd = e._calculate_drawdown(p)
        assert dd == 0.0

    def test_drawdown_zero_initial_cash(self):
        p = Portfolio(initial_cash=0)
        e = RiskEngine()
        assert e._calculate_drawdown(p) == 0.0


class TestRiskEngineSellOrders:
    def test_sell_does_not_count_as_new_position(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        e = RiskEngine(max_open_positions=1)
        order = _make_order("AAPL", Side.SELL, 5)
        result = e.check_order(order, p, market_price=100.0)
        assert result.approved

    def test_sell_order_within_value_cap(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 100, 100.0)
        e = RiskEngine(max_single_order_value=5_000.0)
        order = _make_order("AAPL", Side.SELL, 10)
        result = e.check_order(order, p, market_price=100.0)
        assert result.approved


class TestRiskEngineMultiplePositions:
    def test_buy_existing_symbol_not_blocked_by_max_positions(self):
        p = Portfolio(initial_cash=200_000.0)
        e = RiskEngine(max_open_positions=1, max_position_pct=1.0, max_single_order_value=1_000_000.0)
        p.open_position("AAPL", 10, 100.0)
        order = _make_order("AAPL", Side.BUY, 5)
        result = e.check_order(order, p, market_price=100.0)
        assert result.approved

    def test_new_symbol_blocked_at_max_positions(self):
        p = Portfolio(initial_cash=200_000.0)
        e = RiskEngine(max_open_positions=1, max_position_pct=1.0, max_single_order_value=1_000_000.0)
        p.open_position("AAPL", 10, 100.0)
        order = _make_order("MSFT", Side.BUY, 5)
        result = e.check_order(order, p, market_price=100.0)
        assert not result.approved


class TestRiskEngineConcentration:
    def test_exactly_at_limit_approved(self):
        p = Portfolio(initial_cash=100_000.0)
        e = RiskEngine(max_position_pct=0.20, max_single_order_value=1_000_000.0)
        order = _make_order("AAPL", Side.BUY, 10)
        result = e.check_order(order, p, market_price=100.0)
        assert result.approved

    def test_existing_position_increases_concentration(self):
        p = Portfolio(initial_cash=200_000.0)
        e = RiskEngine(max_position_pct=0.15, max_single_order_value=1_000_000.0)
        p.open_position("AAPL", 200, 100.0)
        p.update_prices({"AAPL": 100.0})
        order = _make_order("AAPL", Side.BUY, 200)
        result = e.check_order(order, p, market_price=100.0)
        assert not result.approved

    def test_zero_portfolio_value_no_crash(self):
        p = Portfolio(initial_cash=0)
        e = RiskEngine()
        order = _make_order("AAPL", Side.BUY, 1)
        result = e.check_order(order, p, market_price=100.0)
        assert result.approved


class TestRiskEngineDailyCounterIncrement:
    def test_counter_increments_on_approval(self):
        p = Portfolio(initial_cash=100_000.0)
        e = RiskEngine(max_daily_trades=5)
        for i in range(3):
            order = _make_order("AAPL", Side.BUY, 1)
            result = e.check_order(order, p, market_price=100.0)
            assert result.approved
        assert e.daily_trade_count == 3

    def test_counter_does_not_increment_on_rejection(self):
        p = Portfolio(initial_cash=100_000.0)
        e = RiskEngine(max_daily_trades=0)
        order = _make_order("AAPL", Side.BUY, 1)
        e.check_order(order, p, market_price=100.0)
        assert e.daily_trade_count == 0


class TestRiskEngineResetCircuitBreaker:
    def test_reset_clears_flag(self):
        e = RiskEngine()
        e.circuit_breaker_active = True
        e.reset_circuit_breaker()
        assert e.circuit_breaker_active is False

    def test_reset_allows_trades_again(self):
        p = Portfolio(initial_cash=100_000.0)
        e = RiskEngine()
        e.circuit_breaker_active = True
        e.reset_circuit_breaker()
        order = _make_order("AAPL", Side.BUY, 1)
        result = e.check_order(order, p, market_price=100.0)
        assert result.approved


class TestRiskEngineResetDailyCounters:
    def test_reset_sets_count_to_zero(self):
        e = RiskEngine()
        e.daily_trade_count = 50
        e.reset_daily_counters()
        assert e.daily_trade_count == 0
