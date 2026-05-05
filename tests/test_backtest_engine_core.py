"""
Comprehensive tests for the BacktestRunner engine core (Phase 1).

Covers:
- IStrategy.evaluate() routing with ICostModel injection
- Configurable slippage, fill probability, commission
- SDK signal conversion to engine signals
- Tax-lot tracking through the evaluate path
- Simulated fill pipeline
- Error resilience (strategy exceptions don't crash the runner)
- Backward compatibility with on_bar strategies
- Equity curve and performance metrics
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from nexus_sdk import IStrategy, MarketState, Signal, StrategyConfig
from nexus_sdk.signals import Side

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.data.feeds import MarketDataProvider


class _InMemoryProvider(MarketDataProvider):
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


def _make_ohlcv(
    n_bars: int = 100,
    base_price: float = 100.0,
    trend: float = 0.3,
    seed: int = 42,
) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=n_bars)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 1, n_bars)
    close = base_price + np.cumsum(noise * 0.5 + trend * 0.1)
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


_MIN_BARS = 5


class _BuyEvaluateStrategy(IStrategy):
    """SDK IStrategy that buys once on bar 60."""

    def __init__(self):
        self._bar_count = 0
        self._cost_model_received: Any = None

    @property
    def id(self) -> str:
        return "test_buy_eval"

    @property
    def name(self) -> str:
        return "BuyEvaluate"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        self._bar_count += 1
        self._cost_model_received = costs
        if self._bar_count == 60:
            return [Signal(
                symbol="TEST",
                side=Side.BUY,
                quantity=100,
                strategy_id=self.id,
            )]
        return []

    def get_config_schema(self) -> dict:
        return {}


class _BuySellEvaluateStrategy(IStrategy):
    """SDK IStrategy that buys on bar 60, sells on bar 80."""

    def __init__(self):
        self._bar_count = 0

    @property
    def id(self) -> str:
        return "test_buy_sell_eval"

    @property
    def name(self) -> str:
        return "BuySellEvaluate"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        self._bar_count += 1
        signals = []
        if self._bar_count == 60:
            signals.append(Signal(
                symbol="TEST", side=Side.BUY, quantity=100, strategy_id=self.id,
            ))
        elif self._bar_count == 80:
            signals.append(Signal(
                symbol="TEST", side=Side.SELL, quantity=100, strategy_id=self.id,
            ))
        return signals

    def get_config_schema(self) -> dict:
        return {}


class _DoubleCycleEvaluateStrategy(IStrategy):
    """SDK IStrategy with 2 buy-sell cycles."""

    def __init__(self):
        self._bar_count = 0

    @property
    def id(self) -> str:
        return "test_double_eval"

    @property
    def name(self) -> str:
        return "DoubleCycleEvaluate"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        self._bar_count += 1
        signals = []
        if self._bar_count == 55:
            signals.append(Signal(symbol="TEST", side=Side.BUY, quantity=50, strategy_id=self.id))
        elif self._bar_count == 65:
            signals.append(Signal(symbol="TEST", side=Side.SELL, quantity=50, strategy_id=self.id))
        elif self._bar_count == 70:
            signals.append(Signal(symbol="TEST", side=Side.BUY, quantity=50, strategy_id=self.id))
        elif self._bar_count == 85:
            signals.append(Signal(symbol="TEST", side=Side.SELL, quantity=50, strategy_id=self.id))
        return signals

    def get_config_schema(self) -> dict:
        return {}


class _CrashingEvaluateStrategy(IStrategy):
    """SDK IStrategy that raises on bar 60."""

    def __init__(self):
        self._bar_count = 0

    @property
    def id(self) -> str:
        return "test_crash_eval"

    @property
    def name(self) -> str:
        return "CrashingEvaluate"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        self._bar_count += 1
        if self._bar_count == 60:
            raise RuntimeError("Strategy evaluate exploded!")
        return []

    def get_config_schema(self) -> dict:
        return {}


class _CostAwareEvaluateStrategy(IStrategy):
    """SDK IStrategy that checks cost model before trading."""

    def __init__(self):
        self._bar_count = 0
        self._cost_pct_seen: float | None = None

    @property
    def id(self) -> str:
        return "test_cost_aware"

    @property
    def name(self) -> str:
        return "CostAwareEvaluate"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        self._bar_count += 1
        if self._bar_count == 60 and costs is not None:
            price = market.latest("TEST")
            if price is not None:
                self._cost_pct_seen = costs.estimate_pct("TEST", price, "buy")
            return [Signal(
                symbol="TEST", side=Side.BUY, quantity=100, strategy_id=self.id,
            )]
        return []

    def get_config_schema(self) -> dict:
        return {}


class _AlwaysHoldEvaluateStrategy(IStrategy):
    """SDK IStrategy that never trades."""

    @property
    def id(self) -> str:
        return "test_hold_eval"

    @property
    def name(self) -> str:
        return "AlwaysHoldEvaluate"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]:
        return []

    def get_config_schema(self) -> dict:
        return {}


def _make_config(**overrides) -> BacktestConfig:
    defaults = {
        "strategy_name": "test",
        "symbol": "TEST",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "initial_capital": 100_000.0,
        "min_bars": _MIN_BARS,
    }
    defaults.update(overrides)
    return BacktestConfig(**defaults)


class TestIStrategyEvaluateRouting:
    """Verify BacktestRunner routes through IStrategy.evaluate() correctly."""

    @pytest.mark.asyncio
    async def test_evaluate_path_produces_trades(self):
        df = _make_ohlcv(100)
        config = _make_config()
        strategy = _BuySellEvaluateStrategy()
        runner = BacktestRunner(
            config=config, strategy=strategy, provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert result.final_capital > 0
        assert len(result.trades) >= 1
        assert len(result.equity_curve) > 0

    @pytest.mark.asyncio
    async def test_evaluate_path_buy_and_sell(self):
        df = _make_ohlcv(100)
        config = _make_config()
        strategy = _BuySellEvaluateStrategy()
        runner = BacktestRunner(
            config=config, strategy=strategy, provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        buys = [t for t in result.trades if t["side"] == "buy"]
        sells = [t for t in result.trades if t["side"] == "sell"]
        assert len(buys) == 1
        assert len(sells) == 1

    @pytest.mark.asyncio
    async def test_evaluate_path_total_trade_count(self):
        df = _make_ohlcv(100)
        config = _make_config()
        strategy = _DoubleCycleEvaluateStrategy()
        runner = BacktestRunner(
            config=config, strategy=strategy, provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert len(result.trades) == 4
        assert result.metrics["total_trades"] == 4

    @pytest.mark.asyncio
    async def test_evaluate_path_hold_preserves_capital(self):
        df = _make_ohlcv(100)
        config = _make_config()
        strategy = _AlwaysHoldEvaluateStrategy()
        runner = BacktestRunner(
            config=config, strategy=strategy, provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)
        assert len(result.trades) == 0

    @pytest.mark.asyncio
    async def test_evaluate_path_uses_islategy_detection(self):
        strategy = _BuyEvaluateStrategy()
        config = _make_config()
        runner = BacktestRunner(config=config, strategy=strategy, provider=_InMemoryProvider(_make_ohlcv(100)))
        assert runner._use_evaluate is True

    @pytest.mark.asyncio
    async def test_on_bar_strategy_not_detected_as_islategy(self):
        from tests.test_backtest_loop import BuySellStrategy

        strategy = BuySellStrategy()
        config = _make_config()
        runner = BacktestRunner(config=config, strategy=strategy, provider=_InMemoryProvider(_make_ohlcv(100)))
        assert runner._use_evaluate is False


class TestICostModelInjection:
    """Verify cost model is passed to IStrategy.evaluate()."""

    @pytest.mark.asyncio
    async def test_cost_model_passed_to_evaluate(self):
        df = _make_ohlcv(100)
        config = _make_config()
        strategy = _BuyEvaluateStrategy()
        runner = BacktestRunner(
            config=config, strategy=strategy, provider=_InMemoryProvider(df),
        )
        await runner.run()

        assert strategy._cost_model_received is not None

    @pytest.mark.asyncio
    async def test_cost_model_has_estimate_pct(self):
        df = _make_ohlcv(100)
        config = _make_config()
        strategy = _CostAwareEvaluateStrategy()
        runner = BacktestRunner(
            config=config, strategy=strategy, provider=_InMemoryProvider(df),
        )
        await runner.run()

        assert strategy._cost_pct_seen is not None
        assert strategy._cost_pct_seen > 0


class TestConfigurableSlippage:
    """Verify slippage_bps config propagates to fills."""

    @pytest.mark.asyncio
    async def test_zero_slippage_reduces_costs(self):
        df = _make_ohlcv(100)
        config_zero = _make_config(slippage_bps=0.0)
        config_high = _make_config(slippage_bps=50.0)

        strategy_zero = _BuyEvaluateStrategy()
        runner_zero = BacktestRunner(
            config=config_zero, strategy=strategy_zero, provider=_InMemoryProvider(df),
        )
        result_zero = await runner_zero.run()

        strategy_high = _BuyEvaluateStrategy()
        runner_high = BacktestRunner(
            config=config_high, strategy=strategy_high, provider=_InMemoryProvider(df),
        )
        result_high = await runner_high.run()

        cost_zero = sum(
            t.get("cost_breakdown", {}).get("slippage", 0.0)
            for t in result_zero.trades
        )
        cost_high = sum(
            t.get("cost_breakdown", {}).get("slippage", 0.0)
            for t in result_high.trades
        )
        assert cost_zero < cost_high

    @pytest.mark.asyncio
    async def test_slippage_affects_fill_price(self):
        df = _make_ohlcv(100)
        config_no_slip = _make_config(slippage_bps=0.0, spread_bps=0.0, commission_per_trade=0.0)
        config_with_slip = _make_config(slippage_bps=100.0, spread_bps=0.0, commission_per_trade=0.0)

        runner_no = BacktestRunner(
            config=config_no_slip, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result_no = await runner_no.run()

        runner_with = BacktestRunner(
            config=config_with_slip, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result_with = await runner_with.run()

        buys_no = [t for t in result_no.trades if t["side"] == "buy"]
        buys_with = [t for t in result_with.trades if t["side"] == "buy"]
        assert len(buys_no) == 1 and len(buys_with) == 1
        assert buys_with[0]["fill_price"] >= buys_no[0]["fill_price"]


class TestConfigurableFillProbability:
    """Verify fill_probability config affects order outcomes."""

    @pytest.mark.asyncio
    async def test_zero_fill_probability_rejects_all(self):
        df = _make_ohlcv(100)
        config = _make_config(fill_probability=0.0)
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert len(result.trades) == 0, "No trades should fill with 0% fill probability"

    @pytest.mark.asyncio
    async def test_full_fill_probability_fills_all(self):
        df = _make_ohlcv(100)
        config = _make_config(fill_probability=1.0)
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert len(result.trades) >= 1, "Trades should fill with 100% fill probability"

    @pytest.mark.asyncio
    async def test_deterministic_with_same_seed(self):
        df = _make_ohlcv(100)
        config = _make_config(fill_probability=0.98, random_seed=42)

        runner1 = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result1 = await runner1.run()

        runner2 = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result2 = await runner2.run()

        assert result1.final_capital == pytest.approx(result2.final_capital, rel=1e-6)
        assert len(result1.trades) == len(result2.trades)


class TestConfigurableCommission:
    """Verify commission_per_trade config affects costs."""

    @pytest.mark.asyncio
    async def test_commission_appears_in_costs(self):
        df = _make_ohlcv(100)
        config = _make_config(commission_per_trade=5.0)
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        for trade in result.trades:
            cb = trade.get("cost_breakdown", {})
            assert cb.get("commission", 0.0) == pytest.approx(5.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_zero_commission_no_commission_cost(self):
        df = _make_ohlcv(100)
        config = _make_config(commission_per_trade=0.0)
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        for trade in result.trades:
            cb = trade.get("cost_breakdown", {})
            assert cb.get("commission", 0.0) == pytest.approx(0.0, abs=0.001)


class TestPnlOnFullExit:
    """Verify PnL calculation when fully exiting a position via evaluate()."""

    @pytest.mark.asyncio
    async def test_pnl_calculated_on_full_exit(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuySellEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        sells = [t for t in result.trades if t["side"] == "sell"]
        assert len(sells) == 1
        assert sells[0]["realized_pnl"] is not None

    @pytest.mark.asyncio
    async def test_pnl_negative_on_loss(self):
        dates = pd.bdate_range("2025-01-01", periods=100)
        close = np.array([100.0] * 100)
        close[60:] = 80.0
        df = pd.DataFrame(
            {
                "open": close - 0.1,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": [500_000] * 100,
            },
            index=dates,
        )

        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuySellEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        sell_trades = [t for t in result.trades if t["side"] == "sell"]
        assert len(sell_trades) == 1
        assert sell_trades[0]["realized_pnl"] < 0


class TestTaxLotTracking:
    """Verify tax-lot FIFO tracking through the evaluate path."""

    @pytest.mark.asyncio
    async def test_double_cycle_produces_four_trades(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_DoubleCycleEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert len(result.trades) == 4
        buys = [t for t in result.trades if t["side"] == "buy"]
        sells = [t for t in result.trades if t["side"] == "sell"]
        assert len(buys) == 2
        assert len(sells) == 2

    @pytest.mark.asyncio
    async def test_costs_applied_to_all_trades(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_DoubleCycleEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        for trade in result.trades:
            assert "cost_breakdown" in trade
            if trade["cost_breakdown"]:
                assert trade["cost_breakdown"].get("total", 0) >= 0


class TestEquityCurveAndMetrics:
    """Verify equity curve and performance metrics through evaluate path."""

    @pytest.mark.asyncio
    async def test_equity_curve_populated(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert len(result.equity_curve) > 0
        for point in result.equity_curve:
            assert "timestamp" in point
            assert "total_value" in point
            assert "cash" in point
            assert point["total_value"] > 0

    @pytest.mark.asyncio
    async def test_metrics_report_complete(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert "sharpe_ratio" in result.metrics
        assert "max_drawdown_pct" in result.metrics
        assert "total_trades" in result.metrics
        assert "total_return_pct" in result.metrics

    @pytest.mark.asyncio
    async def test_final_capital_matches_equity_curve(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert result.final_capital == result.equity_curve[-1]["total_value"]

    @pytest.mark.asyncio
    async def test_total_return_pct_calculation(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        expected_pct = (
            (result.final_capital - config.initial_capital) / config.initial_capital * 100
        )
        assert result.total_return_pct == pytest.approx(expected_pct, rel=1e-6)


class TestErrorResilience:
    """Verify strategy errors don't crash the runner."""

    @pytest.mark.asyncio
    async def test_crashing_strategy_still_completes(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_CrashingEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert len(result.equity_curve) > 0
        assert result.final_capital > 0

    @pytest.mark.asyncio
    async def test_no_provider_raises(self):
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=None,
        )
        with pytest.raises(RuntimeError, match="No data provider"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_no_strategy_raises(self):
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=None, provider=_InMemoryProvider(_make_ohlcv(100)),
        )
        with pytest.raises(RuntimeError, match="No strategy"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_empty_data_raises(self):
        empty_df = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
        )
        empty_df.index = pd.DatetimeIndex([], name="timestamp")
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(empty_df),
        )
        with pytest.raises(RuntimeError, match="No OHLCV data"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_no_data_in_range_raises(self):
        config = _make_config(start_date="2099-01-01", end_date="2099-12-31")
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(_make_ohlcv(100)),
        )
        with pytest.raises(RuntimeError, match="No data in range"):
            await runner.run()


class TestBackwardCompatibility:
    """Verify existing on_bar strategies still work with enhanced runner."""

    @pytest.mark.asyncio
    async def test_on_bar_strategy_still_works(self):
        from tests.test_backtest_loop import BuySellStrategy

        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=BuySellStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        assert len(result.trades) >= 2
        assert result.final_capital > 0

    @pytest.mark.asyncio
    async def test_on_bar_with_custom_slippage(self):
        from tests.test_backtest_loop import BuySellStrategy

        df = _make_ohlcv(100)
        config = _make_config(slippage_bps=20.0)
        runner = BacktestRunner(
            config=config, strategy=BuySellStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        for trade in result.trades:
            cb = trade.get("cost_breakdown", {})
            if cb:
                assert cb.get("slippage", 0.0) > 0


class TestSdkSignalConversion:
    """Verify SDK signals convert correctly to engine signals."""

    @pytest.mark.asyncio
    async def test_buy_signal_converts(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        buys = [t for t in result.trades if t["side"] == "buy"]
        assert len(buys) >= 1
        assert buys[0]["quantity"] == 100
        assert buys[0]["fill_price"] > 0

    @pytest.mark.asyncio
    async def test_sell_signal_converts(self):
        df = _make_ohlcv(100)
        config = _make_config()
        runner = BacktestRunner(
            config=config, strategy=_BuySellEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result = await runner.run()

        sells = [t for t in result.trades if t["side"] == "sell"]
        assert len(sells) == 1
        assert sells[0]["quantity"] == 100
        assert sells[0]["fill_price"] > 0


class TestSpreadBps:
    """Verify spread_bps config propagates to cost model."""

    @pytest.mark.asyncio
    async def test_custom_spread_affects_costs(self):
        df = _make_ohlcv(100)
        config_low = _make_config(spread_bps=1.0, slippage_bps=0.0, commission_per_trade=0.0)
        config_high = _make_config(spread_bps=50.0, slippage_bps=0.0, commission_per_trade=0.0)

        runner_low = BacktestRunner(
            config=config_low, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result_low = await runner_low.run()

        runner_high = BacktestRunner(
            config=config_high, strategy=_BuyEvaluateStrategy(), provider=_InMemoryProvider(df),
        )
        result_high = await runner_high.run()

        spread_low = sum(
            t.get("cost_breakdown", {}).get("spread", 0.0)
            for t in result_low.trades
        )
        spread_high = sum(
            t.get("cost_breakdown", {}).get("spread", 0.0)
            for t in result_high.trades
        )
        assert spread_low < spread_high
