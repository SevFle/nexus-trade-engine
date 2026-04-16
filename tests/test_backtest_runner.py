from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.data.feeds import MarketDataProvider
from engine.plugins.sdk import BaseStrategy

if TYPE_CHECKING:
    from engine.core.portfolio import Portfolio


class _SDKMarketState:
    def __init__(
        self,
        timestamp: Any,
        prices: dict[str, float] | None = None,
        volumes: dict[str, int] | None = None,
        ohlcv: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.timestamp = timestamp
        self.prices = prices or {}
        self.volumes = volumes or {}
        self.ohlcv = ohlcv or {}


class _TrackingStrategy(BaseStrategy):
    name = "tracking"
    version = "0.1.0"

    def __init__(self) -> None:
        self.portfolio_ids: list[int] = []
        self.last_portfolio: Portfolio | None = None
        self.bought = False

    def on_bar(self, state: Any, portfolio: Portfolio) -> list[dict]:
        self.portfolio_ids.append(id(portfolio))
        self.last_portfolio = portfolio
        if not self.bought:
            price = state.prices.get("TEST", 100.0)
            if isinstance(price, (int, float)) and price > 0:
                portfolio.open_position("TEST", 10, price)
                self.bought = True
        return []


class _StubProvider(MarketDataProvider):
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def get_latest_price(self, _symbol: str) -> float | None:
        if self._df.empty:
            return None
        return float(self._df["close"].iloc[-1])

    async def get_ohlcv(
        self,
        _symbol: str,
        **_kwargs: object,
    ) -> pd.DataFrame:
        return self._df

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        if self._df.empty:
            return {}
        return {s: float(self._df["close"].iloc[-1]) for s in symbols}


def _make_ohlcv(n_bars: int = 60, start_price: float = 100.0) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=n_bars, freq="B")
    rng = np.random.default_rng(42)
    close = start_price + np.cumsum(rng.standard_normal(n_bars))
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n_bars, 1_000_000, dtype=int),
        },
        index=dates,
    )


def _default_config(df: pd.DataFrame) -> BacktestConfig:
    return BacktestConfig(
        strategy_name="tracking",
        symbol="TEST",
        start_date=str(df.index[0].date()),
        end_date=str(df.index[-1].date()),
        initial_capital=100_000.0,
        min_bars=1,
    )


@pytest.fixture(autouse=True)
def _mock_nexus_sdk():
    sdk = MagicMock()
    sdk.MarketState = _SDKMarketState
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(__import__("sys").modules, "nexus_sdk.strategy", sdk)
        yield


class TestPortfolioPersistsAcrossBars:
    async def test_same_portfolio_instance_across_bars(self) -> None:
        df = _make_ohlcv(60)
        strategy = _TrackingStrategy()
        provider = _StubProvider(df)
        runner = BacktestRunner(config=_default_config(df), strategy=strategy, provider=provider)
        await runner.run()

        assert len(strategy.portfolio_ids) > 1, "Strategy should have been called multiple times"
        assert len(set(strategy.portfolio_ids)) == 1, (
            "Portfolio was recreated each bar — expected a single instance"
        )

    async def test_position_opened_on_bar_0_survives_to_last_bar(self) -> None:
        df = _make_ohlcv(60)
        strategy = _TrackingStrategy()
        provider = _StubProvider(df)
        runner = BacktestRunner(config=_default_config(df), strategy=strategy, provider=provider)
        await runner.run()

        assert strategy.bought, "Strategy should have opened a position on bar 0"
        assert strategy.last_portfolio is not None
        assert "TEST" in strategy.last_portfolio.positions, (
            "Position opened on bar 0 should still exist in portfolio after last bar"
        )
        assert strategy.last_portfolio.positions["TEST"].quantity == 10  # noqa: PLR2004

    async def test_final_capital_uses_portfolio_total_value(self) -> None:
        df = _make_ohlcv(60)
        strategy = _TrackingStrategy()
        provider = _StubProvider(df)
        runner = BacktestRunner(config=_default_config(df), strategy=strategy, provider=provider)
        result = await runner.run()

        assert strategy.last_portfolio is not None
        assert result.final_capital == strategy.last_portfolio.total_value
        assert result.final_capital != 0.0
