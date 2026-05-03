"""
Integration tests for the 5 major backtest loop fixes:
M1: PnL on full exit
M2: Tax lot tracking (FIFO)
M3: Silent background failures
M4: Strategy sandboxing
M5: Wrong trade count
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.core.cost_model import DefaultCostModel, TaxMethod
from engine.core.portfolio import Portfolio
from engine.core.signal import Side, Signal
from engine.data.feeds import MarketDataProvider
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox


class FakeProvider(MarketDataProvider):
    """In-memory data provider for testing."""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    async def get_latest_price(self, symbol: str) -> float | None:
        if self._df.empty:
            return None
        return float(self._df["close"].iloc[-1])

    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        return self._df

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        price = await self.get_latest_price(symbols[0]) if symbols else None
        return dict.fromkeys(symbols, price or 0.0)


def _make_ohlcv(
    n_bars: int = 100,
    base_price: float = 100.0,
    trend: float = 0.5,
) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=n_bars)
    np.random.seed(42)
    noise = np.random.normal(0, 1, n_bars)
    close = base_price + np.cumsum(noise * 0.5 + trend * 0.1)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.random.randint(100_000, 1_000_000, n_bars),
        },
        index=dates,
    )


_MIN_BARS = 5


class BuySellStrategy:
    """Strategy that buys on bar 60, sells on bar 80."""

    name = "test_buy_sell"
    version = "0.1.0"

    def __init__(self):
        self._bar_count = 0

    def on_bar(self, state, portfolio) -> list[dict]:
        self._bar_count += 1
        signals = []
        if self._bar_count == 60:
            signals.append(
                Signal(
                    symbol="TEST",
                    side=Side.BUY,
                    quantity=100,
                    strategy_id="test",
                )
            )
        elif self._bar_count == 80:
            signals.append(
                Signal(
                    symbol="TEST",
                    side=Side.SELL,
                    quantity=100,
                    strategy_id="test",
                )
            )
        return signals


class DoubleBuySellStrategy:
    """Strategy that does 2 buy-sell cycles."""

    name = "test_double"
    version = "0.1.0"

    def __init__(self):
        self._bar_count = 0

    def on_bar(self, state, portfolio) -> list[dict]:
        self._bar_count += 1
        signals = []
        if self._bar_count == 55:
            signals.append(Signal(symbol="TEST", side=Side.BUY, quantity=50, strategy_id="test"))
        elif self._bar_count == 65:
            signals.append(Signal(symbol="TEST", side=Side.SELL, quantity=50, strategy_id="test"))
        elif self._bar_count == 70:
            signals.append(Signal(symbol="TEST", side=Side.BUY, quantity=50, strategy_id="test"))
        elif self._bar_count == 85:
            signals.append(Signal(symbol="TEST", side=Side.SELL, quantity=50, strategy_id="test"))
        return signals


class CrashingStrategy:
    """Strategy that raises on bar 60."""

    name = "test_crash"
    version = "0.1.0"

    def __init__(self):
        self._bar_count = 0

    def on_bar(self, state, portfolio) -> list[dict]:
        self._bar_count += 1
        if self._bar_count == 60:
            raise RuntimeError("Strategy exploded!")
        return []


class CrashingAsyncStrategy:
    """Strategy that raises on bar 60 via on_bar."""

    name = "test_crash_async"
    version = "0.1.0"

    def __init__(self):
        self._bar_count = 0

    def on_bar(self, state, portfolio) -> list[dict]:
        self._bar_count += 1
        if self._bar_count == 60:
            raise RuntimeError("Strategy exploded async!")
        return []


class TestM1PnlOnFullExit:
    """M1: PnL correctly calculated when fully exiting a position."""

    @pytest.mark.asyncio
    async def test_pnl_calculated_on_full_exit(self):
        df = _make_ohlcv(100)
        provider = FakeProvider(df)
        config = BacktestConfig(
            strategy_name="test_buy_sell",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=100_000.0,
            min_bars=_MIN_BARS,
        )

        runner = BacktestRunner(
            config=config,
            strategy=BuySellStrategy(),
            provider=provider,
        )
        result = await runner.run()

        assert len(result.equity_curve) > 0

        sells = [t for t in result.trades if t["side"] == "sell"]
        assert len(sells) == 1
        assert sells[0]["realized_pnl"] is not None
        assert sells[0]["realized_pnl"] != 0, "PnL should be non-zero on full exit"

        assert len(result.trades) >= 2

        total_costs = result.metrics.get("total_costs", 0.0)
        assert total_costs > 0, "Costs should be applied (no free trades)"

        assert "total_return_pct" in result.metrics
        assert result.final_capital > 0

    @pytest.mark.asyncio
    async def test_pnl_negative_on_loss(self):
        np.random.seed(99)
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

        provider = FakeProvider(df)
        config = BacktestConfig(
            strategy_name="test_buy_sell",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=100_000.0,
            min_bars=_MIN_BARS,
        )

        runner = BacktestRunner(
            config=config,
            strategy=BuySellStrategy(),
            provider=provider,
        )
        result = await runner.run()

        sell_trades = [t for t in result.trades if t["side"] == "sell"]
        assert len(sell_trades) == 1
        assert sell_trades[0]["realized_pnl"] < 0


class TestM2TaxLotsTracked:
    """M2: Tax lots populated on buys, FIFO consumption on sells."""

    def test_tax_lots_created_on_buy(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].quantity == 100
        assert lots[0].purchase_price == 150.0

    def test_multiple_buys_create_multiple_lots(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 100.0)
        p.transaction_date = datetime(2025, 2, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 120.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 2

    def test_sell_consumes_fifo_lots(self):
        p = Portfolio(initial_cash=200_000, tax_method=TaxMethod.FIFO)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 2, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 120.0)
        p.transaction_date = datetime(2025, 3, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 150, 130.0)

        assert len(consumed) == 2
        assert consumed[0]["purchase_price"] == 100.0
        assert consumed[0]["quantity"] == 100
        assert consumed[1]["purchase_price"] == 120.0
        assert consumed[1]["quantity"] == 50

    def test_tax_estimated_on_sell(self):
        cost_model = DefaultCostModel(short_term_tax_rate=0.37)
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        lots = p.get_tax_lots("AAPL")
        tax = cost_model.estimate_tax("AAPL", 150.0, 100, lots)
        assert tax.amount > 0


class TestM3SilentFailures:
    """M3: Background task errors are propagated, not swallowed."""

    @pytest.mark.asyncio
    async def test_runner_raises_on_bad_data(self):
        df = pd.DataFrame()
        provider = FakeProvider(df)
        config = BacktestConfig(
            strategy_name="test",
            symbol="TEST",
            start_date="2025-01-01",
            end_date="2025-12-31",
        )
        runner = BacktestRunner(config=config, strategy=BuySellStrategy(), provider=provider)

        with pytest.raises(RuntimeError, match="No OHLCV data"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_runner_raises_on_no_strategy(self):
        df = _make_ohlcv(100)
        provider = FakeProvider(df)
        config = BacktestConfig(
            strategy_name="test",
            symbol="TEST",
            start_date="2025-01-01",
            end_date="2025-12-31",
        )
        runner = BacktestRunner(config=config, strategy=None, provider=provider)

        with pytest.raises(RuntimeError, match="No strategy"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_runner_raises_on_no_provider(self):
        config = BacktestConfig(
            strategy_name="test",
            symbol="TEST",
            start_date="2025-01-01",
            end_date="2025-12-31",
        )
        runner = BacktestRunner(config=config, strategy=BuySellStrategy(), provider=None)

        with pytest.raises(RuntimeError, match="No data provider"):
            await runner.run()

    @pytest.mark.asyncio
    async def test_api_returns_404_for_unknown_id(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from engine.api.routes.backtest import router

        app = FastAPI()
        app.include_router(router, prefix="/api/backtest")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/backtest/results/nonexistent-id")
            assert resp.status_code == 404
            body = resp.json()
            assert body["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_api_returns_202_for_running(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from engine.api.routes.backtest import _backtest_results, router
        from tests.conftest import FAKE_USER_ID

        _backtest_results["running-test-id"] = (
            time.monotonic(),
            str(FAKE_USER_ID),
            {"status": "running", "strategy_name": "test", "symbol": "TEST"},
        )

        app = FastAPI()
        app.include_router(router, prefix="/api/backtest")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/backtest/results/running-test-id")
            assert resp.status_code == 202
            body = resp.json()
            assert body["status"] == "running"


class TestM4StrategySandbox:
    """M4: Strategy runs through StrategySandbox, errors don't crash engine."""

    @pytest.mark.asyncio
    async def test_sandbox_catches_strategy_exception(self):
        strategy = CrashingStrategy()
        manifest = StrategyManifest(id="crash", name="crash", version="0.1.0")
        sandbox = StrategySandbox(strategy, manifest)

        from engine.core.portfolio import PortfolioSnapshot

        snapshot = PortfolioSnapshot(
            cash=100_000.0,
            positions={},
            total_value=100_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )

        for _ in range(59):
            result = await sandbox.safe_evaluate(snapshot, None, DefaultCostModel())
            assert result == []

        result = await sandbox.safe_evaluate(snapshot, None, DefaultCostModel())
        assert result == [], "Sandbox should return empty list on error"

        assert sandbox.metrics.errors == 1
        assert sandbox.metrics.last_error is not None

    @pytest.mark.asyncio
    async def test_backtest_survives_strategy_crash(self):
        df = _make_ohlcv(100)
        provider = FakeProvider(df)
        config = BacktestConfig(
            strategy_name="test_crash",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=100_000.0,
            min_bars=_MIN_BARS,
        )

        runner = BacktestRunner(
            config=config,
            strategy=CrashingStrategy(),
            provider=provider,
        )
        result = await runner.run()

        assert len(result.equity_curve) > 0, "Bars should still be processed after crash"
        assert result.final_capital > 0


class TestM5TradeCount:
    """M5: total_trades counts all filled orders, not just sells."""

    @pytest.mark.asyncio
    async def test_total_trades_counts_buys_and_sells(self):
        df = _make_ohlcv(100)
        provider = FakeProvider(df)
        config = BacktestConfig(
            strategy_name="test_buy_sell",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=100_000.0,
            min_bars=_MIN_BARS,
        )

        runner = BacktestRunner(
            config=config,
            strategy=BuySellStrategy(),
            provider=provider,
        )
        result = await runner.run()

        total = len(result.trades)
        buys = len([t for t in result.trades if t["side"] == "buy"])
        sells = len([t for t in result.trades if t["side"] == "sell"])

        assert buys == 1, f"Expected 1 buy, got {buys}"
        assert sells == 1, f"Expected 1 sell, got {sells}"
        assert total == 2, f"Expected 2 total trades, got {total}"

    @pytest.mark.asyncio
    async def test_total_trades_in_metrics(self):
        df = _make_ohlcv(100)
        provider = FakeProvider(df)
        config = BacktestConfig(
            strategy_name="test_buy_sell",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=100_000.0,
            min_bars=_MIN_BARS,
        )

        runner = BacktestRunner(
            config=config,
            strategy=BuySellStrategy(),
            provider=provider,
        )
        result = await runner.run()

        metrics_total = result.metrics.get("total_trades", 0)
        assert metrics_total == 2, f"metrics.total_trades should be 2, got {metrics_total}"

    @pytest.mark.asyncio
    async def test_double_cycle_trade_count(self):
        df = _make_ohlcv(100)
        provider = FakeProvider(df)
        config = BacktestConfig(
            strategy_name="test_double",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=100_000.0,
            min_bars=_MIN_BARS,
        )

        runner = BacktestRunner(
            config=config,
            strategy=DoubleBuySellStrategy(),
            provider=provider,
        )
        result = await runner.run()

        total = len(result.trades)
        assert total == 4, f"Expected 4 total trades (2 buys + 2 sells), got {total}"
