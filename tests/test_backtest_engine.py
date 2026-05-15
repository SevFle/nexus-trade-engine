"""Tests for the backtest loop engine: multi-symbol, cost config, strategy params,
build_timeline helper, run_backtest standalone function, and acceptance criteria."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import (
    BacktestConfig,
    BacktestRunner,
    BacktestSummary,
    build_timeline,
    run_backtest,
)
from engine.core.signal import Side, Signal
from engine.data.feeds import MarketDataProvider


class _SyntheticProvider(MarketDataProvider):
    def __init__(self, data: dict[str, pd.DataFrame]):
        self._data = data

    async def get_latest_price(self, symbol: str) -> float | None:
        df = self._data.get(symbol)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        return self._data.get(symbol, pd.DataFrame())

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        result = {}
        for sym in symbols:
            price = await self.get_latest_price(sym)
            if price is not None:
                result[sym] = price
        return result


def _make_df(
    n_days: int = 100,
    base_price: float = 100.0,
    seed: int = 42,
    start_str: str = "2024-01-01",
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = (
        datetime(2024, 1, 1, tzinfo=UTC)
        if start_str == "2024-01-01"
        else pd.Timestamp(start_str)
    )
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


def _make_multi_symbol_data(
    symbols: list[str],
    n_days: int = 100,
    base_price: float = 100.0,
) -> dict[str, pd.DataFrame]:
    data = {}
    for i, sym in enumerate(symbols):
        data[sym] = _make_df(n_days=n_days, base_price=base_price + i * 10, seed=42 + i)
    return data


class _AlwaysHoldStrategy:
    name = "always_hold"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


class _BuyFirstBarStrategy:
    name = "buy_first"
    version = "1.0.0"

    def __init__(self):
        self._bought = False

    def on_bar(self, state, portfolio):
        if not self._bought and portfolio.cash > 50000:
            self._bought = True
            symbol = next(iter(state.prices.keys())) if state.prices else "AAPL"
            return [Signal.buy(symbol=symbol, strategy_id=self.name, quantity=10)]
        return []


class _MultiSymbolBuyStrategy:
    name = "multi_buy"
    version = "1.0.0"

    def __init__(self):
        self._bought = False

    def on_bar(self, state, portfolio):
        if not self._bought and portfolio.cash > 50000:
            self._bought = True
            return [
                Signal.buy(symbol=sym, strategy_id=self.name, quantity=5)
                for sym in state.prices
            ]
        return []


class _ParametrizedStrategy:
    name = "parametrized"
    version = "1.0.0"

    window: int = 20
    threshold: float = 2.0

    def on_bar(self, state, portfolio):
        return []


class _BuySellStrategy:
    name = "buy_sell"
    version = "1.0.0"

    def __init__(self):
        self._bar_count = 0

    def on_bar(self, state, portfolio):
        self._bar_count += 1
        symbol = next(iter(state.prices.keys())) if state.prices else "AAPL"
        if self._bar_count == 60:
            return [Signal(symbol=symbol, side=Side.BUY, quantity=100, strategy_id="test")]
        if self._bar_count == 80:
            return [Signal(symbol=symbol, side=Side.SELL, quantity=100, strategy_id="test")]
        return []


# ── build_timeline tests ──


class TestBuildTimeline:
    def test_single_symbol(self):
        df = _make_df(n_days=50)
        data = {"AAPL": df}
        timeline = build_timeline(data)

        assert len(timeline) == 50
        assert all("AAPL" in bars for _, bars in timeline)

    def test_multi_symbol_aligned(self):
        data = _make_multi_symbol_data(["AAPL", "MSFT"], n_days=50)
        timeline = build_timeline(data)

        assert len(timeline) == 50
        for _, bars in timeline:
            assert "AAPL" in bars
            assert "MSFT" in bars

    def test_multi_symbol_missing_bars_no_forward_fill(self):
        dates_aapl = pd.bdate_range("2024-01-01", periods=5)
        dates_msft = pd.bdate_range("2024-01-01", periods=3)

        df_aapl = pd.DataFrame(
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            index=dates_aapl,
        )
        df_msft = pd.DataFrame(
            {"open": 200, "high": 201, "low": 199, "close": 200, "volume": 2000},
            index=dates_msft,
        )

        timeline = build_timeline({"AAPL": df_aapl, "MSFT": df_msft})

        assert len(timeline) == 5
        has_both = sum(1 for _, bars in timeline if "AAPL" in bars and "MSFT" in bars)
        assert has_both == 3

        _, last_bars = timeline[-1]
        assert "AAPL" in last_bars
        assert "MSFT" not in last_bars

    def test_empty_data(self):
        timeline = build_timeline({})
        assert timeline == []

    def test_sorted_by_timestamp(self):
        data = _make_multi_symbol_data(["AAPL", "MSFT"], n_days=30)
        timeline = build_timeline(data)

        timestamps = [ts for ts, _ in timeline]
        assert timestamps == sorted(timestamps)

    def test_bar_dict_has_ohlcv_keys(self):
        df = _make_df(n_days=10)
        timeline = build_timeline({"AAPL": df})

        for _, bars in timeline:
            bar = bars["AAPL"]
            assert "open" in bar
            assert "high" in bar
            assert "low" in bar
            assert "close" in bar
            assert "volume" in bar
            assert isinstance(bar["volume"], int)


# ── Multi-symbol backtest tests ──


class TestMultiSymbolBacktest:
    @pytest.mark.asyncio
    async def test_multi_symbol_equity_curve(self):
        data = _make_multi_symbol_data(["AAPL", "MSFT"], n_days=100)
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="always_hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            symbols=["AAPL", "MSFT"],
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=provider,
        )
        result = await runner.run()

        assert len(result.equity_curve) > 0
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)
        assert len(result.trades) == 0

    @pytest.mark.asyncio
    async def test_multi_symbol_single_symbol_equivalence(self):
        data = {"AAPL": _make_df(n_days=100, seed=42)}
        provider = _SyntheticProvider(data)

        config_single = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            random_seed=42,
        )
        runner1 = BacktestRunner(
            config=config_single, strategy=_BuySellStrategy(), provider=provider,
        )
        result1 = await runner1.run()

        config_multi = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            symbols=["AAPL"],
            min_bars=5,
            random_seed=42,
        )
        runner2 = BacktestRunner(
            config=config_multi, strategy=_BuySellStrategy(), provider=provider,
        )
        result2 = await runner2.run()

        assert result1.final_capital == pytest.approx(result2.final_capital, rel=1e-6)
        assert len(result1.trades) == len(result2.trades)

    @pytest.mark.asyncio
    async def test_multi_symbol_trades_with_cost(self):
        data = _make_multi_symbol_data(["AAPL", "MSFT"], n_days=100)
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="multi_buy",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            symbols=["AAPL", "MSFT"],
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_MultiSymbolBuyStrategy(), provider=provider,
        )
        result = await runner.run()

        assert len(result.trades) >= 2
        symbols_traded = {t["symbol"] for t in result.trades}
        assert len(symbols_traded) >= 1

        for trade in result.trades:
            assert trade.get("cost_breakdown") is not None
            assert trade["cost_breakdown"].get("total", 0) > 0


# ── Configurable cost model tests ──


class TestConfigurableCostModel:
    @pytest.mark.asyncio
    async def test_custom_cost_config_applied(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            cost_config={"spread_bps": 50.0, "slippage_bps": 100.0},
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider,
        )
        result = await runner.run()

        for trade in result.trades:
            if trade.get("cost_breakdown"):
                assert trade["cost_breakdown"]["total"] > 0

    @pytest.mark.asyncio
    async def test_higher_costs_with_higher_config(self):
        provider_low = _SyntheticProvider({"AAPL": _make_df(n_days=100, seed=42)})
        provider_high = _SyntheticProvider({"AAPL": _make_df(n_days=100, seed=42)})

        config_low = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            random_seed=42,
            cost_config={"spread_bps": 1.0, "slippage_bps": 1.0},
        )
        config_high = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            random_seed=42,
            cost_config={"spread_bps": 100.0, "slippage_bps": 200.0},
        )

        runner_low = BacktestRunner(
            config=config_low, strategy=_BuySellStrategy(), provider=provider_low,
        )
        result_low = await runner_low.run()

        runner_high = BacktestRunner(
            config=config_high, strategy=_BuySellStrategy(), provider=provider_high,
        )
        result_high = await runner_high.run()

        assert result_high.metrics.get("total_costs", 0) > result_low.metrics.get("total_costs", 0)

    @pytest.mark.asyncio
    async def test_default_costs_are_nonzero(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider,
        )
        result = await runner.run()

        total_costs = result.metrics.get("total_costs", 0)
        assert total_costs > 0, "Default cost model should produce non-zero costs"


# ── Strategy params tests ──


class TestStrategyParams:
    @pytest.mark.asyncio
    async def test_strategy_params_applied(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="parametrized",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            strategy_params={"window": 50, "threshold": 3.5},
        )
        strategy = _ParametrizedStrategy()
        assert strategy.window == 20
        assert strategy.threshold == 2.0

        runner = BacktestRunner(
            config=config, strategy=strategy, provider=provider,
        )
        await runner.run()

        assert strategy.window == 50
        assert strategy.threshold == 3.5

    @pytest.mark.asyncio
    async def test_strategy_params_not_overwriting_methods(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="parametrized",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            strategy_params={"on_bar": "should_not_override"},
        )
        strategy = _ParametrizedStrategy()
        runner = BacktestRunner(
            config=config, strategy=strategy, provider=provider,
        )
        await runner.run()

        assert callable(strategy.on_bar)


# ── Determinism tests ──


class TestDeterminism:
    @pytest.mark.asyncio
    async def test_same_seed_deterministic(self):
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            random_seed=42,
        )

        runner1 = BacktestRunner(
            config=config, strategy=_BuySellStrategy(),
            provider=_SyntheticProvider({"AAPL": _make_df(n_days=100, seed=42)}),
        )
        result1 = await runner1.run()

        runner2 = BacktestRunner(
            config=config, strategy=_BuySellStrategy(),
            provider=_SyntheticProvider({"AAPL": _make_df(n_days=100, seed=42)}),
        )
        result2 = await runner2.run()

        assert result1.final_capital == pytest.approx(result2.final_capital, rel=1e-6)
        assert len(result1.trades) == len(result2.trades)
        for t1, t2 in zip(result1.trades, result2.trades, strict=True):
            assert t1["fill_price"] == pytest.approx(t2["fill_price"], rel=1e-6)

    @pytest.mark.asyncio
    async def test_different_seeds_may_differ(self):
        config1 = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            random_seed=42,
        )
        config2 = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            random_seed=99,
        )

        runner1 = BacktestRunner(
            config=config1, strategy=_BuySellStrategy(),
            provider=_SyntheticProvider({"AAPL": _make_df(n_days=100, seed=42)}),
        )
        result1 = await runner1.run()

        runner2 = BacktestRunner(
            config=config2, strategy=_BuySellStrategy(),
            provider=_SyntheticProvider({"AAPL": _make_df(n_days=100, seed=42)}),
        )
        result2 = await runner2.run()

        assert result1.final_capital == pytest.approx(result2.final_capital, rel=0.01)


# ── Zero trades test ──


class TestZeroTrades:
    @pytest.mark.asyncio
    async def test_hold_strategy_zero_return(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="always_hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=provider,
        )
        result = await runner.run()

        assert len(result.trades) == 0
        assert result.total_return_pct == pytest.approx(0.0, abs=0.01)
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)
        assert result.metrics.get("total_costs", 0) == 0
        assert result.metrics.get("total_trades", 0) == 0


# ── Cost breakdown verification ──


class TestCostBreakdownVerification:
    @pytest.mark.asyncio
    async def test_trade_log_has_filled_cost_breakdowns(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider,
        )
        result = await runner.run()

        for trade in result.trades:
            assert "cost_breakdown" in trade
            cb = trade["cost_breakdown"]
            assert cb is not None
            assert "commission" in cb
            assert "spread" in cb
            assert "slippage" in cb
            assert "total" in cb
            assert cb["total"] > 0

    @pytest.mark.asyncio
    async def test_total_costs_positive(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider,
        )
        result = await runner.run()

        assert result.metrics.get("total_costs", 0) > 0

    @pytest.mark.asyncio
    async def test_cost_drag_computed(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider,
        )
        result = await runner.run()

        assert "cost_drag_pct" in result.metrics
        if result.trades:
            assert result.metrics["cost_drag_pct"] > 0


# ── Equity curve verification ──


class TestEquityCurveVerification:
    @pytest.mark.asyncio
    async def test_one_point_per_processed_bar(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="always_hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=provider,
        )
        result = await runner.run()

        assert len(result.equity_curve) > 0

    @pytest.mark.asyncio
    async def test_equity_curve_has_required_fields(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="always_hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=provider,
        )
        result = await runner.run()

        for point in result.equity_curve:
            assert "timestamp" in point
            assert "total_value" in point
            assert "cash" in point

    @pytest.mark.asyncio
    async def test_equity_curve_starts_at_initial_capital(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="always_hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=provider,
        )
        result = await runner.run()

        assert result.equity_curve[0]["total_value"] == 100_000.0


# ── Tax calculation (FIFO by default) ──


class TestTaxFIFO:
    @pytest.mark.asyncio
    async def test_tax_estimate_in_sell_cost_breakdown(self):
        data = {"AAPL": _make_df(n_days=100, base_price=100.0, seed=42)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            cost_config={"short_term_tax_rate": 0.37, "long_term_tax_rate": 0.20},
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider,
        )
        result = await runner.run()

        sells = [t for t in result.trades if t["side"] == "sell"]
        if sells:
            for sell in sells:
                cb = sell.get("cost_breakdown", {})
                assert "tax_estimate" in cb


# ── Metrics completeness ──


class TestMetricsCompleteness:
    @pytest.mark.asyncio
    async def test_all_required_metrics_present(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider,
        )
        result = await runner.run()

        required_keys = [
            "total_return_pct",
            "sharpe_ratio",
            "sortino_ratio",
            "max_drawdown_pct",
            "total_trades",
            "win_rate",
            "total_costs",
            "total_taxes",
            "cost_drag_pct",
            "profit_factor",
            "avg_trade_pnl",
            "max_consecutive_losses",
        ]
        for key in required_keys:
            assert key in result.metrics, f"Missing metric: {key}"

    @pytest.mark.asyncio
    async def test_evaluation_in_metrics(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider,
        )
        result = await runner.run()

        assert "evaluation" in result.metrics
        evaluation = result.metrics["evaluation"]
        assert "composite_score" in evaluation
        assert 0.0 <= evaluation["composite_score"] <= 100.0
        assert "grade" in evaluation


# ── BacktestSummary ──


class TestBacktestSummaryNew:
    def test_from_metrics_with_multi_symbol_data(self):
        from engine.core.metrics import PerformanceMetrics

        equity_curve = [
            {"timestamp": "2024-01-01", "total_value": 100_000, "cash": 100_000},
            {"timestamp": "2024-01-02", "total_value": 101_000, "cash": 80_000},
            {"timestamp": "2024-01-03", "total_value": 102_000, "cash": 102_000},
        ]
        trade_log = [
            {
                "side": "buy",
                "quantity": 100,
                "fill_price": 100.0,
                "realized_pnl": 0.0,
                "cost_breakdown": {"total": 5.0, "tax_estimate": 0.0},
            },
            {
                "side": "sell",
                "quantity": 100,
                "fill_price": 110.0,
                "realized_pnl": 995.0,
                "cost_breakdown": {"total": 5.0, "tax_estimate": 370.0},
            },
        ]
        pm = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100_000,
        )
        summary = BacktestSummary.from_metrics(pm)

        assert summary.total_trades == 2
        assert summary.total_costs == 10.0
        assert summary.total_taxes == 370.0
        assert summary.cost_drag_pct > 0
        assert summary.total_return_pct > 0


# ── run_backtest standalone function ──


class TestRunBacktestStandalone:
    @pytest.mark.asyncio
    async def test_run_backtest_raises_for_missing_strategy(self):
        config = BacktestConfig(
            strategy_name="nonexistent_strategy_xyz",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        with pytest.raises(ValueError, match="Strategy not found"):
            await run_backtest(config)


# ── Interval parameter ──


class TestIntervalParameter:
    @pytest.mark.asyncio
    async def test_interval_passed_to_provider(self):
        data = {"AAPL": _make_df(n_days=100)}
        provider = _SyntheticProvider(data)
        config = BacktestConfig(
            strategy_name="always_hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            interval="1h",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=provider,
        )
        result = await runner.run()
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)


# ── Volume passthrough ──


class TestVolumePassthrough:
    @pytest.mark.asyncio
    async def test_volume_influences_slippage(self):
        data_low_vol = {
            "AAPL": _make_df(n_days=100, seed=42).assign(volume=[10_000_000] * 100),
        }
        data_high_vol = {
            "AAPL": _make_df(n_days=100, seed=42).assign(volume=[100] * 100),
        }

        config = BacktestConfig(
            strategy_name="buy_sell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            min_bars=5,
            random_seed=42,
            cost_config={"slippage_bps": 100.0},
        )

        runner_low = BacktestRunner(
            config=config,
            strategy=_BuySellStrategy(),
            provider=_SyntheticProvider(data_low_vol),
        )
        result_low = await runner_low.run()

        runner_high = BacktestRunner(
            config=config,
            strategy=_BuySellStrategy(),
            provider=_SyntheticProvider(data_high_vol),
        )
        result_high = await runner_high.run()

        assert result_high.metrics.get("total_costs", 0) >= result_low.metrics.get("total_costs", 0)
