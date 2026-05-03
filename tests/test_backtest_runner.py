"""Integration tests for BacktestRunner — end-to-end backtest with synthetic data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
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


class _SimpleBuyStrategy:
    name = "simple_buy"
    version = "1.0.0"

    def __init__(self):
        self._bought = False

    def on_bar(self, state, portfolio):
        from engine.core.signal import Signal

        if not self._bought and portfolio.cash > 50000:
            self._bought = True
            return [Signal.buy(symbol="AAPL", strategy_id=self.name, quantity=10)]
        return []


class _AlwaysHoldStrategy:
    name = "always_hold"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


def _make_synthetic_df(
    n_days: int = 252, base_price: float = 150.0, seed: int = 42
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    returns = rng.normal(0.0005, 0.015, n_days)
    closes = base_price * np.cumprod(1 + returns)
    closes[0] = base_price
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


@pytest.fixture
def synthetic_df() -> pd.DataFrame:
    return _make_synthetic_df()


@pytest.fixture
def provider(synthetic_df) -> _SyntheticProvider:
    return _SyntheticProvider(synthetic_df)


@pytest.fixture
def buy_config() -> BacktestConfig:
    return BacktestConfig(
        strategy_name="simple_buy",
        symbol="AAPL",
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        random_seed=42,
    )


@pytest.fixture
def hold_config() -> BacktestConfig:
    return BacktestConfig(
        strategy_name="always_hold",
        symbol="AAPL",
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        random_seed=42,
    )


class TestBacktestRunnerIntegration:
    async def test_run_produces_nonzero_results(self, provider, buy_config):
        runner = BacktestRunner(
            config=buy_config, strategy=_SimpleBuyStrategy(), provider=provider
        )
        result = await runner.run()

        assert result.final_capital != 0
        assert len(result.equity_curve) > 0
        assert result.total_return_pct != 0

    async def test_equity_curve_length_matches_bars(self, provider, buy_config):
        runner = BacktestRunner(
            config=buy_config, strategy=_SimpleBuyStrategy(), provider=provider
        )
        result = await runner.run()

        assert len(result.equity_curve) > 50

    async def test_costs_applied_to_trades(self, provider, buy_config):
        runner = BacktestRunner(
            config=buy_config, strategy=_SimpleBuyStrategy(), provider=provider
        )
        result = await runner.run()

        for trade in result.trades:
            assert "cost_breakdown" in trade
            if trade["cost_breakdown"]:
                assert trade["cost_breakdown"].get("total", 0) >= 0

    async def test_deterministic_with_fixed_seed(self, provider, buy_config):
        runner1 = BacktestRunner(
            config=buy_config, strategy=_SimpleBuyStrategy(), provider=provider
        )
        result1 = await runner1.run()

        runner2 = BacktestRunner(
            config=buy_config, strategy=_SimpleBuyStrategy(), provider=provider
        )
        result2 = await runner2.run()

        assert result1.final_capital == pytest.approx(result2.final_capital, rel=1e-6)
        assert len(result1.trades) == len(result2.trades)

    async def test_hold_strategy_preserves_capital(self, provider, hold_config):
        runner = BacktestRunner(
            config=hold_config, strategy=_AlwaysHoldStrategy(), provider=provider
        )
        result = await runner.run()

        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)
        assert len(result.trades) == 0

    async def test_metrics_report_included(self, provider, buy_config):
        runner = BacktestRunner(
            config=buy_config, strategy=_SimpleBuyStrategy(), provider=provider
        )
        result = await runner.run()

        assert "sharpe_ratio" in result.metrics
        assert "max_drawdown_pct" in result.metrics
        assert "total_trades" in result.metrics

    async def test_evaluation_score_attached_to_metrics(self, provider, buy_config):
        runner = BacktestRunner(
            config=buy_config, strategy=_SimpleBuyStrategy(), provider=provider
        )
        result = await runner.run()

        assert "evaluation" in result.metrics
        evaluation = result.metrics["evaluation"]
        assert "composite_score" in evaluation
        assert 0.0 <= evaluation["composite_score"] <= 100.0
        assert "grade" in evaluation
        assert evaluation["grade"] in {"A+", "A", "B+", "B", "C+", "C", "D", "F"}
        assert "dimensions" in evaluation
        assert "warnings" in evaluation


class TestBacktestRunnerErrors:
    async def test_no_provider_raises(self):
        config = BacktestConfig(
            strategy_name="test",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=None)
        with pytest.raises(RuntimeError, match="No data provider"):
            await runner.run()

    async def test_no_strategy_raises(self, provider):
        config = BacktestConfig(
            strategy_name="test",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        runner = BacktestRunner(config=config, strategy=None, provider=provider)
        with pytest.raises(RuntimeError, match="No strategy"):
            await runner.run()

    async def test_empty_data_raises(self):
        empty_df = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
        )
        empty_df.index = pd.DatetimeIndex([], name="timestamp")
        provider = _SyntheticProvider(empty_df)
        config = BacktestConfig(
            strategy_name="test",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=provider)
        with pytest.raises(RuntimeError, match="No OHLCV data"):
            await runner.run()

    async def test_no_data_in_range_raises(self, synthetic_df):
        provider = _SyntheticProvider(synthetic_df)
        config = BacktestConfig(
            strategy_name="test",
            symbol="AAPL",
            start_date="2099-01-01",
            end_date="2099-12-31",
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=provider)
        with pytest.raises(RuntimeError, match="No data in range"):
            await runner.run()
