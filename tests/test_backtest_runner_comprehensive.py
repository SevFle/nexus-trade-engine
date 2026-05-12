"""
Comprehensive tests for BacktestRunner targeting uncovered code paths.

Covers: BacktestConfig defaults, BacktestResult defaults, BacktestSummary,
timezone handling, sell signal P&L, warmup skip, HOLD/wrong-symbol filtering,
multiple trades, and more. Uses pure unit tests with mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import BacktestConfig, BacktestResult, BacktestRunner
from engine.core.signal import Signal


def _make_df(n_days=60, base_price=100.0, seed=42, tz_aware=False):
    rng = np.random.default_rng(seed)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    returns = rng.normal(0.0005, 0.01, n_days)
    closes = base_price * np.cumprod(1 + returns)
    closes[0] = base_price
    idx = pd.DatetimeIndex(dates, name="timestamp")
    if tz_aware:
        idx = idx.tz_convert("US/Eastern")
    return pd.DataFrame(
        {
            "open": closes * (1 + rng.normal(0, 0.001, n_days)),
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": rng.integers(500_000, 5_000_000, n_days),
        },
        index=idx,
    )


class _FakeProvider:
    def __init__(self, df):
        self._df = df

    async def get_latest_price(self, symbol):
        if self._df.empty:
            return None
        return float(self._df["close"].iloc[-1])

    async def get_ohlcv(self, symbol, period="1y", interval="1d"):
        return self._df

    async def get_multiple_prices(self, symbols):
        if self._df.empty:
            return {}
        return {symbols[0]: float(self._df["close"].iloc[-1])}


class _BuySellStrategy:
    name = "buy_sell"
    version = "1.0.0"

    def __init__(self):
        self._bought = False
        self._sold = False

    def on_bar(self, state, portfolio):
        if not self._bought and portfolio.cash > 50000:
            self._bought = True
            return [Signal.buy(symbol="AAPL", strategy_id=self.name, quantity=10)]
        if self._bought and not self._sold and len(state.ohlcv.get("AAPL", [])) > 20:
            self._sold = True
            return [Signal.sell(symbol="AAPL", strategy_id=self.name, quantity=10)]
        return []


class _MultiBuyStrategy:
    name = "multi_buy"
    version = "1.0.0"

    def __init__(self):
        self._count = 0

    def on_bar(self, state, portfolio):
        if self._count < 3 and portfolio.cash > 20000:
            self._count += 1
            return [Signal.buy(symbol="AAPL", strategy_id=self.name, quantity=5)]
        return []


class _AlwaysHoldStrategy:
    name = "always_hold"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


class _WrongSymbolStrategy:
    name = "wrong_symbol"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal.buy(symbol="MSFT", strategy_id=self.name, quantity=10)]


class _HoldSignalStrategy:
    name = "hold_signals"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal.hold(symbol="AAPL", strategy_id=self.name)]


# ── BacktestConfig defaults ──


class TestBacktestConfig:
    def test_defaults(self):
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        assert config.initial_capital == 100_000.0
        assert config.min_bars == 50
        assert config.debug is False
        assert config.random_seed == 42
        assert config.portfolio_id is None

    def test_custom_values(self):
        pid = uuid4()
        config = BacktestConfig(
            strategy_name="test", symbol="MSFT",
            start_date="2024-01-01", end_date="2024-12-31",
            initial_capital=50_000.0, min_bars=30,
            debug=True, random_seed=123, portfolio_id=pid,
        )
        assert config.initial_capital == 50_000.0
        assert config.min_bars == 30
        assert config.debug is True
        assert config.random_seed == 123
        assert config.portfolio_id == pid


# ── BacktestResult defaults ──


class TestBacktestResult:
    def test_defaults(self):
        pid = uuid4()
        result = BacktestResult(portfolio_id=pid)
        assert result.portfolio_id == pid
        assert result.equity_curve == []
        assert result.trades == []
        assert result.metrics == {}
        assert result.final_capital == 0.0
        assert result.total_return_pct == 0.0

    def test_none_portfolio_id(self):
        result = BacktestResult()
        assert result.portfolio_id is None


# ── BacktestRunner constructor ──


class TestBacktestRunnerConstructor:
    def test_stores_config(self):
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(config=config)
        assert runner.config is config
        assert runner.strategy is None
        assert runner.provider is None

    def test_stores_strategy_and_provider(self):
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        strategy = _AlwaysHoldStrategy()
        df = _make_df()
        provider = _FakeProvider(df)
        runner = BacktestRunner(config=config, strategy=strategy, provider=provider)
        assert runner.strategy is strategy
        assert runner.provider is provider

    def test_creates_market_state_builder_with_config(self):
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            min_bars=25, debug=True,
        )
        runner = BacktestRunner(config=config)
        assert runner._builder._min_bars == 25
        assert runner._builder._debug is True


# ── Error paths ──


class TestBacktestRunnerErrors:
    async def test_no_provider_raises(self):
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(config=config, strategy=_AlwaysHoldStrategy(), provider=None)
        with pytest.raises(RuntimeError, match="No data provider"):
            await runner.run()

    async def test_no_strategy_raises(self):
        df = _make_df()
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(config=config, strategy=None, provider=_FakeProvider(df))
        with pytest.raises(RuntimeError, match="No strategy"):
            await runner.run()

    async def test_empty_data_raises(self):
        empty_df = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
        )
        empty_df.index = pd.DatetimeIndex([], name="timestamp")
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(empty_df),
        )
        with pytest.raises(RuntimeError, match="No OHLCV data"):
            await runner.run()

    async def test_no_data_in_range_raises(self):
        df = _make_df()
        config = BacktestConfig(
            strategy_name="test", symbol="AAPL",
            start_date="2099-01-01", end_date="2099-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        with pytest.raises(RuntimeError, match="No data in range"):
            await runner.run()


# ── Hold strategy preserves capital ──


class TestHoldStrategy:
    async def test_hold_preserves_capital(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            initial_capital=100_000.0,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)
        assert len(result.trades) == 0

    async def test_hold_equity_curve_has_entries(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert len(result.equity_curve) > 0


# ── Buy sell strategy ──


class TestBuySellStrategy:
    async def test_buy_sell_produces_trades(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="buy_sell", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert len(result.trades) >= 2
        sides = {t["side"] for t in result.trades}
        assert "buy" in sides
        assert "sell" in sides

    async def test_sell_trade_has_realized_pnl(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="buy_sell", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        sell_trades = [t for t in result.trades if t["side"] == "sell"]
        if sell_trades:
            assert "realized_pnl" in sell_trades[0]

    async def test_buy_trade_realized_pnl_is_zero(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="buy_sell", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        buy_trades = [t for t in result.trades if t["side"] == "buy"]
        if buy_trades:
            assert buy_trades[0]["realized_pnl"] == 0.0


# ── Multiple buys ──


class TestMultipleBuys:
    async def test_multiple_buy_trades(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="multi_buy", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_MultiBuyStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert len(result.trades) == 3
        assert all(t["side"] == "buy" for t in result.trades)


# ── Wrong symbol filtering ──


class TestWrongSymbolFiltering:
    async def test_wrong_symbol_signals_filtered(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="wrong", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_WrongSymbolStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert len(result.trades) == 0


# ── HOLD signal filtering ──


class TestHoldSignalFiltering:
    async def test_hold_signals_produce_no_trades(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="holds", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_HoldSignalStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert len(result.trades) == 0


# ── Timezone handling ──


class TestTimezoneHandling:
    async def test_tz_aware_data(self):
        df = _make_df(n_days=60, tz_aware=True)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)

    async def test_tz_naive_data(self):
        df = _make_df(n_days=60, tz_aware=False)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)


# ── Equity curve structure ──


class TestEquityCurveStructure:
    async def test_equity_curve_has_required_fields(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        for point in result.equity_curve:
            assert "timestamp" in point
            assert "total_value" in point
            assert "cash" in point

    async def test_equity_curve_total_value_starts_at_initial(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            initial_capital=100_000.0,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert result.equity_curve[0]["total_value"] == 100_000.0


# ── Trade record structure ──


class TestTradeRecordStructure:
    async def test_trade_has_required_fields(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="buy_sell", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        if result.trades:
            trade = result.trades[0]
            assert "timestamp" in trade
            assert "symbol" in trade
            assert "side" in trade
            assert "quantity" in trade
            assert "fill_price" in trade
            assert "cost_breakdown" in trade

    async def test_trade_symbol_matches_config(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="buy_sell", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        for trade in result.trades:
            assert trade["symbol"] == "AAPL"


# ── Metrics computation ──


class TestMetricsComputation:
    async def test_metrics_report_present(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert "sharpe_ratio" in result.metrics
        assert "max_drawdown_pct" in result.metrics
        assert "total_trades" in result.metrics

    async def test_total_return_pct_computed(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            initial_capital=100_000.0,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert result.total_return_pct == pytest.approx(0.0, abs=0.01)


# ── Determinism ──


class TestDeterminism:
    async def test_same_seed_same_result(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="buy_sell", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            random_seed=42,
        )

        runner1 = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=_FakeProvider(df),
        )
        result1 = await runner1.run()

        runner2 = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=_FakeProvider(df),
        )
        result2 = await runner2.run()

        assert result1.final_capital == pytest.approx(result2.final_capital, rel=1e-6)
        assert len(result1.trades) == len(result2.trades)


# ── Warmup period (min_bars) ──


class TestWarmupSkip:
    async def test_warmup_skips_insufficient_bars(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            min_bars=50,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        # With 60 days and min_bars=50, first ~50 bars are warmup
        assert len(result.equity_curve) < 60


# ── Evaluation score ──


class TestEvaluationScore:
    async def test_evaluation_attached(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert "evaluation" in result.metrics
        if result.metrics["evaluation"]:
            assert "composite_score" in result.metrics["evaluation"]


# ── Portfolio ID propagation ──


class TestPortfolioIdPropagation:
    async def test_portfolio_id_in_result(self):
        pid = uuid4()
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
            portfolio_id=pid,
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert result.portfolio_id == pid


# ── BacktestSummary ──


class TestBacktestSummary:
    def test_from_metrics_creates_summary(self):
        from engine.core.backtest_runner import BacktestSummary
        from engine.core.metrics import PerformanceMetrics

        equity_curve = [
            {"timestamp": "2024-01-01", "total_value": 100_000, "cash": 100_000},
            {"timestamp": "2024-01-02", "total_value": 101_000, "cash": 100_000},
            {"timestamp": "2024-01-03", "total_value": 100_500, "cash": 100_000},
        ]
        pm = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100_000,
        )
        summary = BacktestSummary.from_metrics(pm)
        assert isinstance(summary, BacktestSummary)
        assert summary.total_trades == 0
        assert isinstance(summary.total_return_pct, float)
        assert isinstance(summary.sharpe_ratio, float)
        assert isinstance(summary.max_drawdown_pct, float)

    def test_from_metrics_with_trades(self):
        from engine.core.backtest_runner import BacktestSummary
        from engine.core.metrics import PerformanceMetrics

        equity_curve = [
            {"timestamp": "2024-01-01", "total_value": 100_000, "cash": 80_000},
            {"timestamp": "2024-01-02", "total_value": 102_000, "cash": 80_000},
            {"timestamp": "2024-01-03", "total_value": 105_000, "cash": 105_000},
        ]
        trades = [
            {"side": "buy", "quantity": 100, "fill_price": 100.0, "realized_pnl": 0.0,
             "cost_breakdown": {"total": 5.0, "tax_estimate": 0.0}},
            {"side": "sell", "quantity": 100, "fill_price": 105.0, "realized_pnl": 495.0,
             "cost_breakdown": {"total": 5.0, "tax_estimate": 10.0}},
        ]
        pm = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trades,
            initial_cash=100_000,
        )
        summary = BacktestSummary.from_metrics(pm)
        assert summary.total_trades == 2
        assert summary.total_costs > 0
        assert summary.total_taxes > 0


# ── Logging output ──


class TestLoggingOutput:
    async def test_run_completes_without_error(self):
        df = _make_df(n_days=60)
        config = BacktestConfig(
            strategy_name="hold", symbol="AAPL",
            start_date="2024-01-01", end_date="2024-12-31",
        )
        runner = BacktestRunner(
            config=config, strategy=_AlwaysHoldStrategy(), provider=_FakeProvider(df),
        )
        result = await runner.run()
        assert isinstance(result, BacktestResult)
