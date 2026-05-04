"""Tests for gaps in engine.core.backtest_runner — timezone handling,
BacktestSummary, initial_capital edge, portfolio_id propagation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import (
    BacktestConfig,
    BacktestResult,
    BacktestRunner,
    BacktestSummary,
)
from engine.data.feeds import MarketDataProvider


class _SyntheticProvider(MarketDataProvider):
    def __init__(self, df: pd.DataFrame):
        self._df = df

    async def get_latest_price(self, symbol: str) -> float | None:
        if self._df.empty:
            return None
        return float(self._df["close"].iloc[-1])

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        return self._df

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        if self._df.empty:
            return {}
        return {symbols[0]: float(self._df["close"].iloc[-1])}


class _AlwaysHoldStrategy:
    name = "always_hold"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


def _make_tz_aware_df(n_days: int = 100, tz: str = "US/Eastern") -> pd.DataFrame:
    rng = np.random.default_rng(42)
    start = datetime(2024, 1, 1)
    dates = pd.date_range(start=start, periods=n_days, freq="D", tz=tz)
    closes = 150.0 * np.cumprod(1 + rng.normal(0.0005, 0.015, n_days))
    closes[0] = 150.0
    opens = closes * (1 + rng.normal(0, 0.002, n_days))
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.003, n_days)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.003, n_days)))
    volumes = rng.integers(500_000, 5_000_000, n_days)
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


def _make_naive_df(n_days: int = 100) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    start = datetime(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    closes = 150.0 * np.cumprod(1 + rng.normal(0.0005, 0.015, n_days))
    closes[0] = 150.0
    opens = closes * (1 + rng.normal(0, 0.002, n_days))
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.003, n_days)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.003, n_days)))
    volumes = rng.integers(500_000, 5_000_000, n_days)
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


class TestTimezoneAwareData:
    async def test_tz_aware_data_runs(self):
        df = _make_tz_aware_df()
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            random_seed=42,
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=provider)
        result = await runner.run()
        assert len(result.equity_curve) > 0

    async def test_tz_aware_produces_same_equity_curve_count_as_naive(self):
        tz_df = _make_tz_aware_df(n_days=60)
        naive_df = _make_naive_df(n_days=60)
        tz_provider = _SyntheticProvider(tz_df)
        naive_provider = _SyntheticProvider(naive_df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            random_seed=42,
        )
        tz_runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=tz_provider)
        naive_runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=naive_provider)
        tz_result = await tz_runner.run()
        naive_result = await naive_runner.run()
        assert len(tz_result.equity_curve) == len(naive_result.equity_curve)


class TestInitialCapitalEdgeCases:
    async def test_zero_initial_capital_no_division_error(self):
        df = _make_naive_df()
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=0.0,
            random_seed=42,
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=provider)
        result = await runner.run()
        assert result.total_return_pct == 0.0
        assert result.final_capital == 0.0

    async def test_small_initial_capital(self):
        df = _make_naive_df()
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=1.0,
            random_seed=42,
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=provider)
        result = await runner.run()
        assert result.final_capital == pytest.approx(1.0, abs=0.01)


class TestPortfolioIdPropagation:
    async def test_portfolio_id_in_result(self):
        df = _make_naive_df()
        provider = _SyntheticProvider(df)
        pid = uuid.uuid4()
        config = BacktestConfig(
            strategy_name="hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            portfolio_id=pid,
            random_seed=42,
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=provider)
        result = await runner.run()
        assert result.portfolio_id == pid

    async def test_portfolio_id_none_default(self):
        df = _make_naive_df()
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            random_seed=42,
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=provider)
        result = await runner.run()
        assert result.portfolio_id is None


class TestBacktestConfig:
    def test_default_values(self):
        config = BacktestConfig(
            strategy_name="test",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        assert config.initial_capital == 100_000.0
        assert config.min_bars == 50
        assert config.debug is False
        assert config.random_seed == 42
        assert config.portfolio_id is None

    def test_custom_values(self):
        config = BacktestConfig(
            strategy_name="test",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=50_000.0,
            min_bars=100,
            debug=True,
            random_seed=None,
        )
        assert config.initial_capital == 50_000.0
        assert config.min_bars == 100
        assert config.debug is True
        assert config.random_seed is None


class TestBacktestResult:
    def test_default_values(self):
        result = BacktestResult()
        assert result.portfolio_id is None
        assert result.equity_curve == []
        assert result.trades == []
        assert result.metrics == {}
        assert result.final_capital == 0.0
        assert result.total_return_pct == 0.0


class TestBacktestSummaryFromMetrics:
    def test_from_metrics_produces_summary(self):
        n_days = 60
        df = _make_naive_df(n_days)
        equity_curve = [
            {"timestamp": df.index[i], "total_value": float(df["close"].iloc[i]) * 10, "cash": 50000.0}
            for i in range(n_days)
        ]
        from engine.core.metrics import PerformanceMetrics

        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100_000.0,
        )
        summary = BacktestSummary.from_metrics(metrics)
        assert isinstance(summary, BacktestSummary)
        assert isinstance(summary.total_return_pct, float)
        assert isinstance(summary.sharpe_ratio, float)
        assert isinstance(summary.total_trades, int)
        assert summary.total_trades == 0

    def test_from_metrics_with_trades(self):
        n_days = 60
        df = _make_naive_df(n_days)
        equity_curve = [
            {"timestamp": df.index[i], "total_value": float(df["close"].iloc[i]) * 10, "cash": 50000.0}
            for i in range(n_days)
        ]
        trade_log = [
            {"side": "buy", "realized_pnl": 0.0, "cost_breakdown": {"total": 1.0}},
            {"side": "sell", "realized_pnl": 500.0, "cost_breakdown": {"total": 1.5}},
        ]
        from engine.core.metrics import PerformanceMetrics

        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100_000.0,
        )
        summary = BacktestSummary.from_metrics(metrics)
        assert summary.total_trades == 2
        assert summary.avg_trade_pnl != 0.0

    def test_from_metrics_all_fields_populated(self):
        n_days = 60
        df = _make_naive_df(n_days)
        equity_curve = [
            {"timestamp": df.index[i], "total_value": float(df["close"].iloc[i]) * 10, "cash": 50000.0}
            for i in range(n_days)
        ]
        from engine.core.metrics import PerformanceMetrics

        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100_000.0,
        )
        summary = BacktestSummary.from_metrics(metrics)
        assert summary.annualized_return_pct is not None
        assert summary.volatility_annual_pct is not None
        assert summary.win_rate is not None
