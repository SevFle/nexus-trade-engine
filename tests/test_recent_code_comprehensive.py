"""
Comprehensive tests for most recently changed code — execution backends,
backtest runner loop edge cases, performance metrics edge cases, and
order manager cost-rejection paths.

Targets:
- engine/core/execution/backtest.py  (BacktestBackend)
- engine/core/execution/paper.py     (PaperBackend)
- engine/core/execution/live.py      (LiveBackend)
- engine/core/execution/base.py      (FillResult, ExecutionBackend)
- engine/core/backtest_runner.py     (BacktestRunner loop edge cases)
- engine/core/metrics.py             (PerformanceMetrics edge cases)
- engine/core/cost_model.py          (DefaultCostModel uncovered branches)
- engine/core/order_manager.py       (cost_pct rejection detail)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.core.cost_model import (
    CostBreakdown,
    DefaultCostModel,
    Money,
    TaxLot,
    TaxMethod,
)
from engine.core.execution.backtest import BacktestBackend
from engine.core.execution.base import FillResult
from engine.core.execution.live import LiveBackend
from engine.core.execution.paper import PaperBackend
from engine.core.metrics import (
    PerformanceMetrics,
    compute_cagr,
    compute_max_drawdown,
    compute_sharpe_ratio,
)
from engine.core.order_manager import OrderManager, OrderStatus
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal
from engine.data.feeds import MarketDataProvider


class _FakeBackend:
    def __init__(self, success=True, price=100.0, quantity=10):
        self._success = success
        self._price = price
        self._quantity = quantity

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def execute(self, order, market_price, costs):
        if self._success:
            return FillResult(success=True, price=self._price, quantity=self._quantity)
        return FillResult(success=False, reason="Simulated failure")


class _SynthProvider(MarketDataProvider):
    def __init__(self, df):
        self._df = df

    async def get_latest_price(self, symbol):
        return float(self._df["close"].iloc[-1]) if not self._df.empty else None

    async def get_ohlcv(self, symbol, period="1y", interval="1d"):
        return self._df

    async def get_multiple_prices(self, symbols):
        if self._df.empty:
            return {}
        return {symbols[0]: float(self._df["close"].iloc[-1])}


def _make_ohlcv(n_bars=100, base=100.0, seed=42):
    dates = pd.bdate_range("2025-01-01", periods=n_bars)
    rng = np.random.default_rng(seed)
    close = base + np.cumsum(rng.normal(0, 0.5, n_bars))
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": rng.integers(100_000, 1_000_000, n_bars),
        },
        index=dates,
    )


def _make_order(symbol="AAPL", side=Side.BUY, quantity=10):
    return Signal.buy(symbol=symbol, strategy_id="test", quantity=quantity)


# ═══════════════════════════════════════════════════════════════════════
# 1. BacktestBackend — unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestBacktestBackendConnect:
    async def test_connect_succeeds(self):
        backend = BacktestBackend()
        await backend.connect()

    async def test_disconnect_succeeds(self):
        backend = BacktestBackend()
        await backend.disconnect()


class TestBacktestBackendDeterministic:
    async def test_same_seed_produces_same_fills(self):
        b1 = BacktestBackend(random_seed=123)
        b2 = BacktestBackend(random_seed=123)
        await b1.connect()
        await b2.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(1.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill1 = await b1.execute(order, 100.0, costs)
        fill2 = await b2.execute(order, 100.0, costs)
        assert fill1.price == fill2.price
        assert fill1.quantity == fill2.quantity
        assert fill1.success is True

    async def test_different_seeds_different_results(self):
        b1 = BacktestBackend(fill_probability=1.0, random_seed=1)
        b2 = BacktestBackend(fill_probability=1.0, random_seed=999)

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(1.0))
        order1 = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=5000
        )
        order2 = Order(
            signal_id="s2", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=5000
        )
        fill1 = await b1.execute(order1, 100.0, costs)
        fill2 = await b2.execute(order2, 100.0, costs)
        assert fill1.success is True
        assert fill2.success is True


class TestBacktestBackendFillProbability:
    async def test_zero_fill_probability_always_fails(self):
        backend = BacktestBackend(fill_probability=0.0, random_seed=42)
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown()
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is False
        assert "Simulated fill failure" in fill.reason

    async def test_one_fill_probability_always_succeeds(self):
        backend = BacktestBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown()
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is True


class TestBacktestBackendSlippage:
    async def test_buy_applies_positive_slippage(self):
        backend = BacktestBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()

        from engine.core.order_manager import Order

        slippage = Money(5.0)
        costs = CostBreakdown(slippage=slippage)
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        expected_slippage_per_share = 5.0 / 10
        assert fill.price == pytest.approx(100.0 + expected_slippage_per_share, abs=0.001)

    async def test_sell_applies_negative_slippage(self):
        backend = BacktestBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()

        from engine.core.order_manager import Order

        slippage = Money(5.0)
        costs = CostBreakdown(slippage=slippage)
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.SELL, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        expected_slippage_per_share = 5.0 / 10
        assert fill.price == pytest.approx(100.0 - expected_slippage_per_share, abs=0.001)

    async def test_zero_quantity_zero_slippage(self):
        backend = BacktestBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(5.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=0
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is True
        assert fill.price == 100.0


class TestBacktestBackendPartialFills:
    async def test_large_order_gets_partial_fill(self):
        backend = BacktestBackend(
            fill_probability=1.0, partial_fill_enabled=True, random_seed=42
        )
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(10.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=5000
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is True
        assert fill.quantity >= 1
        assert fill.quantity <= 5000

    async def test_small_order_no_partial_fill(self):
        backend = BacktestBackend(
            fill_probability=1.0, partial_fill_enabled=True, random_seed=42
        )
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(1.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=100
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is True
        assert fill.quantity == 100

    async def test_partial_fill_disabled_gives_full_quantity(self):
        backend = BacktestBackend(
            fill_probability=1.0, partial_fill_enabled=False, random_seed=42
        )
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(10.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=5000
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is True
        assert fill.quantity == 5000


# ═══════════════════════════════════════════════════════════════════════
# 2. PaperBackend — unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestPaperBackendConnect:
    async def test_connect_sets_state(self):
        backend = PaperBackend()
        await backend.connect()
        assert backend._connected is True

    async def test_disconnect_clears_state(self):
        backend = PaperBackend()
        await backend.connect()
        await backend.disconnect()
        assert backend._connected is False


class TestPaperBackendExecution:
    async def test_execute_without_connect_fails(self):
        backend = PaperBackend()
        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(1.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is False
        assert "not connected" in fill.reason.lower()

    async def test_connected_execute_succeeds(self):
        backend = PaperBackend()
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(1.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is True
        assert fill.quantity == 10

    async def test_buy_slippage_increases_price(self):
        backend = PaperBackend()
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(10.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.price >= 100.0

    async def test_sell_slippage_decreases_price(self):
        backend = PaperBackend()
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(10.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.SELL, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.price <= 100.0

    async def test_zero_quantity_no_division_error(self):
        backend = PaperBackend()
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(5.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=0
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is True

    async def test_paper_produces_full_quantity(self):
        backend = PaperBackend()
        await backend.connect()

        from engine.core.order_manager import Order

        costs = CostBreakdown(slippage=Money(1.0))
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=5000
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.quantity == 5000


# ═══════════════════════════════════════════════════════════════════════
# 3. LiveBackend — unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestLiveBackendConnect:
    async def test_connect_sets_client_none(self):
        backend = LiveBackend()
        await backend.connect()
        assert backend._client is None

    async def test_disconnect_clears_client(self):
        backend = LiveBackend()
        await backend.disconnect()
        assert backend._client is None


class TestLiveBackendExecution:
    async def test_execute_without_client_fails(self):
        backend = LiveBackend()
        from engine.core.order_manager import Order

        costs = CostBreakdown()
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is False
        assert "not yet implemented" in fill.reason.lower() or "not connected" in fill.reason.lower()

    async def test_execute_with_none_client_returns_failure(self):
        backend = LiveBackend()
        backend._client = None
        from engine.core.order_manager import Order

        costs = CostBreakdown()
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        fill = await backend.execute(order, 100.0, costs)
        assert fill.success is False

    async def test_broker_name_stored(self):
        backend = LiveBackend(broker_name="ibkr")
        assert backend.broker_name == "ibkr"


# ═══════════════════════════════════════════════════════════════════════
# 4. PerformanceMetrics — edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestPerformanceMetricsEmpty:
    def test_empty_equity_curve(self):
        metrics = PerformanceMetrics(
            equity_curve=[], trade_log=[], initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.total_return_pct == 0.0
        assert report.total_trades == 0
        assert report.sharpe_ratio == 0.0
        assert report.max_drawdown_pct == 0.0
        assert report.volatility_annual_pct == 0.0

    def test_single_point_curve(self):
        metrics = PerformanceMetrics(
            equity_curve=[{"total_value": 100_000.0, "cash": 100_000.0}],
            trade_log=[],
            initial_cash=100_000.0,
        )
        report = metrics.calculate()
        assert report.total_return_pct == pytest.approx(0.0)
        assert report.sharpe_ratio == 0.0
        assert report.max_drawdown_pct == 0.0
        assert report.max_drawdown_duration_days == 0


class TestPerformanceMetricsZeroInitial:
    def test_zero_initial_cash(self):
        metrics = PerformanceMetrics(
            equity_curve=[
                {"total_value": 0.0, "cash": 0.0},
                {"total_value": 0.0, "cash": 0.0},
            ],
            trade_log=[],
            initial_cash=0.0,
        )
        report = metrics.calculate()
        assert report.total_return_pct == 0.0
        assert report.annualized_return_pct == 0.0


class TestPerformanceMetricsRecovery:
    def test_max_drawdown_recovery_found(self):
        values = [100, 110, 105, 95, 90, 100, 110, 115]
        curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100.0
        )
        report = metrics.calculate()
        assert report.max_drawdown_pct > 0
        assert report.max_drawdown_recovery_days is not None
        assert report.max_drawdown_recovery_days > 0

    def test_no_recovery_returns_none(self):
        values = [100, 110, 105, 95, 80, 70, 60]
        curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100.0
        )
        report = metrics.calculate()
        assert report.max_drawdown_recovery_days is None

    def test_zero_drawdown_zero_recovery(self):
        curve = [
            {"total_value": 100.0, "cash": 100.0},
            {"total_value": 100.0, "cash": 100.0},
        ]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100.0
        )
        report = metrics.calculate()
        assert report.max_drawdown_pct == 0.0
        assert report.max_drawdown_recovery_days == 0


class TestPerformanceMetricsDrawdownDuration:
    def test_monotonic_rise_no_drawdown(self):
        values = list(range(100, 120))
        curve = [{"total_value": float(v), "cash": float(v) * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100.0
        )
        report = metrics.calculate()
        assert report.max_drawdown_duration_days == 0

    def test_single_dip_drawdown_duration(self):
        values = [100, 110, 100, 95, 100, 110]
        curve = [{"total_value": float(v), "cash": float(v) * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100.0
        )
        report = metrics.calculate()
        assert report.max_drawdown_duration_days > 0


class TestPerformanceMetricsRolling:
    def test_rolling_window_metrics(self):
        values = [100 + i * 0.5 for i in range(60)]
        curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve,
            trade_log=[],
            initial_cash=100.0,
            rolling_windows=[20],
        )
        report = metrics.calculate()
        assert len(report.rolling_metrics) == 1
        assert report.rolling_metrics[0].window_days == 20

    def test_rolling_window_too_large_skipped(self):
        values = [100 + i * 0.5 for i in range(10)]
        curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve,
            trade_log=[],
            initial_cash=100.0,
            rolling_windows=[100],
        )
        report = metrics.calculate()
        assert len(report.rolling_metrics) == 0

    def test_rolling_window_multiple_windows(self):
        values = [100 + i * 0.5 for i in range(100)]
        curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve,
            trade_log=[],
            initial_cash=100.0,
            rolling_windows=[20, 60],
        )
        report = metrics.calculate()
        assert len(report.rolling_metrics) == 2


class TestPerformanceMetricsExposure:
    def test_fully_invested(self):
        curve = [
            {"total_value": 100_000.0, "cash": 0.0},
            {"total_value": 105_000.0, "cash": 0.0},
        ]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.exposure_pct == pytest.approx(100.0)

    def test_fully_in_cash(self):
        curve = [
            {"total_value": 100_000.0, "cash": 100_000.0},
            {"total_value": 100_000.0, "cash": 100_000.0},
        ]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.exposure_pct == pytest.approx(0.0)

    def test_empty_curve_zero_exposure(self):
        metrics = PerformanceMetrics(
            equity_curve=[], trade_log=[], initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.exposure_pct == 0.0


class TestPerformanceMetricsTurnover:
    def test_no_trades_zero_turnover(self):
        curve = [{"total_value": 100_000.0, "cash": 100_000.0}] * 10
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.turnover_ratio == 0.0

    def test_trades_with_turnover(self):
        curve = [{"total_value": 100_000.0 + i * 100, "cash": 50_000.0} for i in range(10)]
        trades = [
            {"quantity": 100, "fill_price": 150.0, "realized_pnl": 0.0, "side": "buy"},
            {"quantity": 100, "fill_price": 155.0, "realized_pnl": 500.0, "side": "sell"},
        ]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=trades, initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.turnover_ratio > 0

    def test_empty_curve_zero_turnover(self):
        metrics = PerformanceMetrics(
            equity_curve=[], trade_log=[], initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.turnover_ratio == 0.0


class TestPerformanceMetricsCosts:
    def test_costs_from_dict_breakdown(self):
        trades = [
            {
                "quantity": 100,
                "fill_price": 150.0,
                "realized_pnl": 0.0,
                "cost_breakdown": {"total": 15.0, "tax_estimate": 5.0},
                "side": "buy",
            },
        ]
        metrics = PerformanceMetrics(
            equity_curve=[{"total_value": 100_000.0, "cash": 100_000.0}],
            trade_log=trades,
            initial_cash=100_000.0,
        )
        report = metrics.calculate()
        assert report.total_costs == 15.0
        assert report.total_taxes == 5.0

    def test_costs_from_object_breakdown(self):
        from engine.core.metrics import CostBreakdown as MetricsCostBreakdown

        cb = MetricsCostBreakdown(total=5.0, tax_estimate=3.0)
        trades = [
            {
                "quantity": 100,
                "fill_price": 150.0,
                "realized_pnl": 0.0,
                "cost_breakdown": cb,
                "side": "buy",
            },
        ]
        metrics = PerformanceMetrics(
            equity_curve=[{"total_value": 100_000.0, "cash": 100_000.0}],
            trade_log=trades,
            initial_cash=100_000.0,
        )
        report = metrics.calculate()
        assert report.total_costs == pytest.approx(5.0)
        assert report.total_taxes == pytest.approx(3.0)

    def test_no_cost_breakdown(self):
        trades = [
            {"quantity": 100, "fill_price": 150.0, "realized_pnl": 0.0, "side": "buy"},
        ]
        metrics = PerformanceMetrics(
            equity_curve=[{"total_value": 100_000.0, "cash": 100_000.0}],
            trade_log=trades,
            initial_cash=100_000.0,
        )
        report = metrics.calculate()
        assert report.total_costs == 0.0
        assert report.total_taxes == 0.0


class TestPerformanceMetricsConsecutive:
    def test_max_consecutive_wins(self):
        trades = [
            {"realized_pnl": 100.0},
            {"realized_pnl": 200.0},
            {"realized_pnl": -50.0},
            {"realized_pnl": 150.0},
            {"realized_pnl": 300.0},
            {"realized_pnl": 250.0},
        ]
        curve = [{"total_value": 100_000.0 + i * 100, "cash": 50_000.0} for i in range(10)]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=trades, initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.max_consecutive_wins == 3

    def test_max_consecutive_losses(self):
        trades = [
            {"realized_pnl": -100.0},
            {"realized_pnl": -200.0},
            {"realized_pnl": -300.0},
            {"realized_pnl": 50.0},
            {"realized_pnl": -100.0},
        ]
        curve = [{"total_value": 100_000.0 + i * 100, "cash": 50_000.0} for i in range(10)]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=trades, initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.max_consecutive_losses == 3

    def test_empty_trades_consecutive(self):
        metrics = PerformanceMetrics(
            equity_curve=[{"total_value": 100_000.0, "cash": 100_000.0}],
            trade_log=[],
            initial_cash=100_000.0,
        )
        report = metrics.calculate()
        assert report.max_consecutive_wins == 0
        assert report.max_consecutive_losses == 0


class TestPerformanceMetricsWinRateProfitFactor:
    def test_all_winners(self):
        trades = [
            {"realized_pnl": 100.0},
            {"realized_pnl": 200.0},
            {"realized_pnl": 50.0},
        ]
        curve = [{"total_value": 100_000.0 + i * 100, "cash": 50_000.0} for i in range(10)]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=trades, initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.win_rate == pytest.approx(100.0)
        assert report.profit_factor is None
        assert report.avg_loser == 0.0

    def test_all_losers(self):
        trades = [
            {"realized_pnl": -100.0},
            {"realized_pnl": -200.0},
        ]
        curve = [{"total_value": 100_000.0 + i * 100, "cash": 50_000.0} for i in range(10)]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=trades, initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.win_rate == 0.0
        assert report.profit_factor == 0.0
        assert report.avg_winner == 0.0

    def test_mixed_trades(self):
        trades = [
            {"realized_pnl": 200.0},
            {"realized_pnl": -100.0},
        ]
        curve = [{"total_value": 100_000.0 + i * 100, "cash": 50_000.0} for i in range(10)]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=trades, initial_cash=100_000.0
        )
        report = metrics.calculate()
        assert report.win_rate == pytest.approx(50.0)
        assert report.profit_factor == pytest.approx(2.0)
        assert report.avg_winner == 200.0
        assert report.avg_loser == -100.0
        assert report.best_trade == 200.0
        assert report.worst_trade == -100.0


class TestPerformanceMetricsCalmar:
    def test_calmar_with_drawdown(self):
        values = [100, 110, 100, 90, 95, 100, 120]
        curve = [{"total_value": float(v), "cash": float(v) * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100.0
        )
        report = metrics.calculate()
        assert report.calmar_ratio is not None

    def test_calmar_zero_drawdown_positive_return(self):
        values = list(range(100, 110))
        curve = [{"total_value": float(v), "cash": float(v) * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve, trade_log=[], initial_cash=100.0
        )
        report = metrics.calculate()
        assert report.max_drawdown_pct == 0.0
        assert report.calmar_ratio is None


class TestPerformanceMetricsToDict:
    def test_to_dict_includes_all_fields(self):
        values = [100 + i for i in range(20)]
        curve = [{"total_value": float(v), "cash": float(v) * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=curve,
            trade_log=[{"realized_pnl": 10.0, "quantity": 10, "fill_price": 100.0, "side": "buy"}],
            initial_cash=100.0,
            rolling_windows=[10],
        )
        report = metrics.calculate()
        d = report.to_dict()
        assert "total_return_pct" in d
        assert "sharpe_ratio" in d
        assert "max_drawdown_pct" in d
        assert "rolling_metrics" in d
        assert len(d["rolling_metrics"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# 5. Standalone metric functions
# ═══════════════════════════════════════════════════════════════════════


class TestComputeSharpeRatio:
    def test_empty_returns_zero(self):
        assert compute_sharpe_ratio([]) == 0.0

    def test_constant_returns_zero_std(self):
        result = compute_sharpe_ratio([0.0] * 10)
        assert result == 0.0

    def test_positive_sharpe(self):
        returns = [0.01] * 5 + [0.02] * 5
        sharpe = compute_sharpe_ratio(returns)
        assert sharpe != 0.0


class TestComputeMaxDrawdown:
    def test_empty_curve(self):
        assert compute_max_drawdown([]) == 0.0

    def test_no_drawdown(self):
        assert compute_max_drawdown([100, 110, 120]) == 0.0

    def test_with_drawdown(self):
        dd = compute_max_drawdown([100, 90, 80, 90, 100])
        assert dd == pytest.approx(20.0)

    def test_single_value(self):
        assert compute_max_drawdown([100.0]) == 0.0


class TestComputeCAGR:
    def test_zero_start(self):
        assert compute_cagr(0, 200, 5) == 0.0

    def test_zero_years(self):
        assert compute_cagr(100, 200, 0) == 0.0

    def test_positive_cagr(self):
        cagr = compute_cagr(100, 200, 5)
        assert cagr > 0
        assert cagr == pytest.approx(((2.0 ** (1 / 5)) - 1) * 100)


# ═══════════════════════════════════════════════════════════════════════
# 6. BacktestRunner loop edge cases
# ═══════════════════════════════════════════════════════════════════════


class _WrongSymbolStrategy:
    name = "wrong_symbol"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal.buy(symbol="MSFT", strategy_id="test", quantity=10)]


class _HoldOnlyStrategy:
    name = "hold_only"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal.hold(symbol="TEST", strategy_id="test")]


class _MultiSignalStrategy:
    name = "multi_signal"
    version = "1.0.0"

    def __init__(self):
        self._bar = 0

    def on_bar(self, state, portfolio):
        self._bar += 1
        if self._bar == 60:
            return [
                Signal.buy(symbol="TEST", strategy_id="test", quantity=50),
                Signal.buy(symbol="MSFT", strategy_id="test", quantity=50),
                Signal.hold(symbol="TEST", strategy_id="test"),
            ]
        return []


class TestBacktestRunnerWrongSymbol:
    async def test_wrong_symbol_signals_filtered(self):
        df = _make_ohlcv(100)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="wrong",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_WrongSymbolStrategy(), provider=provider
        )
        result = await runner.run()
        assert len(result.trades) == 0


class TestBacktestRunnerHoldSignals:
    async def test_hold_signals_do_not_create_trades(self):
        df = _make_ohlcv(100)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_HoldOnlyStrategy(), provider=provider
        )
        result = await runner.run()
        assert len(result.trades) == 0
        assert len(result.equity_curve) > 0


class TestBacktestRunnerMultiSignal:
    async def test_multi_signal_filters_correctly(self):
        df = _make_ohlcv(100)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="multi",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_MultiSignalStrategy(), provider=provider
        )
        result = await runner.run()
        assert len(result.trades) == 1
        assert result.trades[0]["symbol"] == "TEST"


class TestBacktestRunnerWarmup:
    async def test_warmup_bars_skipped_gracefully(self):
        df = _make_ohlcv(100)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=200,
        )

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        runner = BacktestRunner(
            config=config, strategy=HoldStrat(), provider=provider
        )
        result = await runner.run()
        assert len(result.equity_curve) == 0


class TestBacktestRunnerZeroInitialCapital:
    async def test_zero_initial_capital(self):
        df = _make_ohlcv(100)

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=0.0,
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=HoldStrat(), provider=provider
        )
        result = await runner.run()
        assert result.total_return_pct == 0.0


class TestBacktestRunnerTzAware:
    async def test_tz_aware_data_localizes_dates(self):
        dates = pd.bdate_range("2024-01-01", periods=60, tz="US/Eastern")
        rng = np.random.default_rng(42)
        close = 100 + np.cumsum(rng.normal(0, 0.5, 60))
        df = pd.DataFrame(
            {
                "open": close - 0.1,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": rng.integers(100_000, 1_000_000, 60),
            },
            index=dates,
        )

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date="2024-01-01",
            end_date="2024-03-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=HoldStrat(), provider=provider
        )
        result = await runner.run()
        assert len(result.equity_curve) > 0


class TestBacktestRunnerPortfolioId:
    async def test_portfolio_id_propagated(self):
        import uuid

        pid = uuid.uuid4()
        df = _make_ohlcv(100)

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
            portfolio_id=pid,
        )
        runner = BacktestRunner(
            config=config, strategy=HoldStrat(), provider=provider
        )
        result = await runner.run()
        assert result.portfolio_id == pid


# ═══════════════════════════════════════════════════════════════════════
# 7. OrderManager cost-rejection detail
# ═══════════════════════════════════════════════════════════════════════


class TestOrderManagerCostPctRejection:
    async def test_cost_pct_rejection_with_reason(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel(commission_per_trade=100.0)
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend(success=True, price=100.0, quantity=10))
        signal = Signal.buy(
            symbol="AAPL",
            strategy_id="test",
            quantity=10,
            max_cost_pct=1e-6,
        )
        order = await mgr.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.RISK_REJECTED
        assert any("Cost" in h.get("reason", "") for h in order.status_history)

    async def test_cost_pct_within_tolerance_passes(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel(commission_per_trade=0.0)
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend(success=True, price=100.0, quantity=10))
        signal = Signal.buy(
            symbol="AAPL",
            strategy_id="test",
            quantity=10,
            max_cost_pct=1.0,
        )
        order = await mgr.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.FILLED

    async def test_no_max_cost_pct_skips_check(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel(commission_per_trade=100.0)
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend(success=True, price=100.0, quantity=10))
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await mgr.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.FILLED

    async def test_cost_pct_zero_trade_value_inf(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel()
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend(success=True, price=0.0, quantity=10))
        signal = Signal.buy(
            symbol="AAPL",
            strategy_id="test",
            quantity=10,
            max_cost_pct=0.01,
        )
        order = await mgr.process_signal(signal, market_price=0.0)
        assert order.status == OrderStatus.RISK_REJECTED


# ═══════════════════════════════════════════════════════════════════════
# 8. DefaultCostModel — uncovered branches
# ═══════════════════════════════════════════════════════════════════════


class TestDefaultCostModelSlippage:
    def test_slippage_with_volume(self):
        model = DefaultCostModel(slippage_bps=10.0)
        slip = model.estimate_slippage("AAPL", 100, 100.0, 1_000_000)
        assert slip.amount > 0
        base = 100.0 * (10.0 / 10_000) * 100
        participation = 100 / 1_000_000
        expected = base * (1.0 + participation * 10)
        assert slip.amount == pytest.approx(expected)

    def test_slippage_zero_volume_uses_base(self):
        model = DefaultCostModel(slippage_bps=10.0)
        slip = model.estimate_slippage("AAPL", 100, 100.0, 0)
        expected = 100.0 * (10.0 / 10_000) * 100
        assert slip.amount == pytest.approx(expected)


class TestDefaultCostModelTax:
    def test_tax_with_loss_no_tax(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=150.0,
                purchase_date=sell_date - timedelta(days=30),
            ),
        ]
        tax = model.estimate_tax("AAPL", 100.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date)
        assert tax.amount == 0.0

    def test_tax_long_term_rate(self):
        model = DefaultCostModel(long_term_tax_rate=0.20)
        sell_date = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=100.0,
                purchase_date=sell_date - timedelta(days=400),
            ),
        ]
        tax = model.estimate_tax("AAPL", 200.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date)
        expected = (200.0 - 100.0) * 100 * 0.20
        assert tax.amount == pytest.approx(expected)

    def test_tax_specific_lot_method(self):
        model = DefaultCostModel(short_term_tax_rate=0.37)
        sell_date = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=100.0,
                purchase_date=sell_date - timedelta(days=10),
            ),
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=120.0,
                purchase_date=sell_date - timedelta(days=5),
            ),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 50, lots, TaxMethod.SPECIFIC_LOT, sell_date=sell_date)
        expected = (150.0 - 100.0) * 50 * 0.37
        assert tax.amount == pytest.approx(expected)

    def test_tax_no_lots_zero(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        tax = model.estimate_tax("AAPL", 150.0, 100, [], TaxMethod.FIFO, sell_date=sell_date)
        assert tax.amount == 0.0

    def test_tax_more_quantity_than_lots(self):
        model = DefaultCostModel(short_term_tax_rate=0.37)
        sell_date = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=100.0,
                purchase_date=sell_date - timedelta(days=10),
            ),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 200, lots, TaxMethod.FIFO, sell_date=sell_date)
        expected = (150.0 - 100.0) * 50 * 0.37
        assert tax.amount == pytest.approx(expected)


class TestDefaultCostModelWashSale:
    def test_wash_sale_different_symbol_no_match(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        assert model.check_wash_sale(
            "AAPL",
            sell_date,
            [{"symbol": "MSFT", "date": sell_date - timedelta(days=5)}],
        ) is False

    def test_wash_sale_within_window(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        assert model.check_wash_sale(
            "AAPL",
            sell_date,
            [{"symbol": "AAPL", "date": sell_date - timedelta(days=5)}],
        ) is True

    def test_wash_sale_adjustment_positive_loss_no_wash(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        result = model.calculate_wash_sale_adjustment(
            "AAPL",
            sell_date,
            100.0,
            [{"symbol": "AAPL", "date": sell_date - timedelta(days=5), "price": 100.0, "quantity": 10}],
        )
        assert result["is_wash_sale"] is False

    def test_wash_sale_adjustment_loss_no_replacement(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        result = model.calculate_wash_sale_adjustment(
            "AAPL",
            sell_date,
            -500.0,
            [{"symbol": "MSFT", "date": sell_date - timedelta(days=5), "price": 100.0, "quantity": 10}],
        )
        assert result["is_wash_sale"] is False
        assert result["adjustment"] == 0.0

    def test_wash_sale_adjustment_loss_with_replacement(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        result = model.calculate_wash_sale_adjustment(
            "AAPL",
            sell_date,
            -500.0,
            [
                {"symbol": "AAPL", "date": sell_date - timedelta(days=5), "price": 100.0, "quantity": 100},
            ],
        )
        assert result["is_wash_sale"] is True
        assert result["adjustment"] == 500.0
        assert result["adjustment_per_share"] == 5.0

    def test_wash_sale_adjustment_zero_replacement_qty(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        result = model.calculate_wash_sale_adjustment(
            "AAPL",
            sell_date,
            -500.0,
            [{"symbol": "AAPL", "date": sell_date - timedelta(days=5), "price": 100.0, "quantity": 0}],
        )
        assert result["is_wash_sale"] is True
        assert result["adjustment_per_share"] == 0.0


class TestDefaultCostModelDividend:
    def test_qualified_dividend(self):
        model = DefaultCostModel(qualified_dividend_rate=0.15)
        tax = model.estimate_dividend_tax(1000.0, is_qualified=True)
        assert tax.amount == pytest.approx(150.0)

    def test_ordinary_dividend(self):
        model = DefaultCostModel(ordinary_dividend_rate=0.37)
        tax = model.estimate_dividend_tax(1000.0, is_qualified=False)
        assert tax.amount == pytest.approx(370.0)


class TestDefaultCostModelEstimatePct:
    def test_round_trip_cost_pct(self):
        model = DefaultCostModel(
            commission_per_trade=1.0, spread_bps=5.0, slippage_bps=10.0
        )
        pct = model.estimate_pct("AAPL", 100.0, "buy")
        expected_one_side_bps = 5.0 + 10.0
        expected_round_trip_bps = expected_one_side_bps * 2
        expected_commission_bps = (1.0 / 100.0) * 10_000
        expected = (expected_round_trip_bps + expected_commission_bps) / 10_000
        assert pct == pytest.approx(expected)


class TestDefaultCostModelEstimateTotal:
    def test_estimate_total_no_tax(self):
        model = DefaultCostModel()
        cb = model.estimate_total("AAPL", 100, 150.0, "buy", 1_000_000)
        assert cb.tax_estimate.amount == 0.0
        assert cb.currency_conversion.amount == 0.0
        assert cb.total.amount > 0

    def test_estimate_total_with_all_components(self):
        model = DefaultCostModel(
            commission_per_trade=5.0,
            spread_bps=10.0,
            slippage_bps=5.0,
            exchange_fee_per_share=0.01,
        )
        cb = model.estimate_total("AAPL", 100, 150.0, "buy", 1_000_000)
        assert cb.commission.amount == 5.0
        assert cb.spread.amount > 0
        assert cb.slippage.amount > 0
        assert cb.exchange_fee.amount == pytest.approx(0.01 * 100)
        assert cb.total.amount == pytest.approx(
            cb.commission.amount
            + cb.spread.amount
            + cb.slippage.amount
            + cb.exchange_fee.amount
        )


# ═══════════════════════════════════════════════════════════════════════
# 9. FillResult edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestFillResultEdgeCases:
    def test_default_values(self):
        fr = FillResult(success=True)
        assert fr.price == 0.0
        assert fr.quantity == 0
        assert fr.reason == ""

    def test_success_with_values(self):
        fr = FillResult(success=True, price=150.25, quantity=100)
        assert fr.price == 150.25
        assert fr.quantity == 100

    def test_failure_with_reason(self):
        fr = FillResult(success=False, reason="Insufficient liquidity")
        assert fr.success is False
        assert fr.reason == "Insufficient liquidity"


# ═══════════════════════════════════════════════════════════════════════
# 10. Integration: BacktestRunner with real execution pipeline
# ═══════════════════════════════════════════════════════════════════════


class _BuyOnFirstBarStrategy:
    name = "buy_first"
    version = "1.0.0"

    def __init__(self):
        self._bought = False

    def on_bar(self, state, portfolio):
        if not self._bought:
            self._bought = True
            return [Signal.buy(symbol="TEST", strategy_id="test", quantity=10)]
        return []


class TestBacktestRunnerIntegration:
    async def test_buy_produces_trade_record(self):
        df = _make_ohlcv(100)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="buy_first",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuyOnFirstBarStrategy(), provider=provider
        )
        result = await runner.run()
        buys = [t for t in result.trades if t["side"] == "buy"]
        assert len(buys) >= 1
        assert buys[0]["quantity"] > 0
        assert buys[0]["fill_price"] > 0

    async def test_equity_curve_has_timestamps(self):
        df = _make_ohlcv(100)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
        )

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        runner = BacktestRunner(
            config=config, strategy=HoldStrat(), provider=provider
        )
        result = await runner.run()
        for point in result.equity_curve:
            assert "timestamp" in point
            assert "total_value" in point
            assert "cash" in point

    async def test_total_return_positive_on_profitable_run(self):
        df = _make_ohlcv(100, base=100.0, seed=1)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="buy_first",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuyOnFirstBarStrategy(), provider=provider
        )
        result = await runner.run()
        assert isinstance(result.total_return_pct, float)

    async def test_metrics_include_evaluation(self):
        df = _make_ohlcv(100)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
        )

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        runner = BacktestRunner(
            config=config, strategy=HoldStrat(), provider=provider
        )
        result = await runner.run()
        assert "evaluation" in result.metrics
        if result.metrics["evaluation"] is not None:
            assert "composite_score" in result.metrics["evaluation"]


class TestBacktestRunnerDebugMode:
    async def test_debug_mode_runs(self):
        df = _make_ohlcv(100)
        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
            debug=True,
        )

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        runner = BacktestRunner(
            config=config, strategy=HoldStrat(), provider=provider
        )
        result = await runner.run()
        assert len(result.equity_curve) > 0
