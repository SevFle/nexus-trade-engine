from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.data.feeds import MarketDataProvider
from engine.plugins.sdk import BaseStrategy

if TYPE_CHECKING:
    from engine.core.portfolio import Portfolio


@dataclass
class _SDKMarketState:
    timestamp: datetime
    prices: dict[str, float] = field(default_factory=dict)
    volumes: dict[str, int] = field(default_factory=dict)
    ohlcv: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


sys.modules.setdefault(
    "nexus_sdk.strategy",
    type(sys)("nexus_sdk.strategy"),
)
sys.modules["nexus_sdk.strategy"].MarketState = _SDKMarketState  # type: ignore[attr-defined]


class _TrackingStrategy(BaseStrategy):
    name = "tracking"
    version = "0.1.0"

    def __init__(self) -> None:
        self.portfolio_snapshots: list[int] = []
        self.bought = False

    def on_bar(self, state: Any, portfolio: Portfolio) -> list[dict]:
        self.portfolio_snapshots.append(id(portfolio))
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


class TestPortfolioPersistsAcrossBars:
    async def test_same_portfolio_instance_across_bars(self) -> None:
        df = _make_ohlcv(60)
        strategy = _TrackingStrategy()
        provider = _StubProvider(df)
        runner = BacktestRunner(config=_default_config(df), strategy=strategy, provider=provider)
        await runner.run()

        assert len(strategy.portfolio_snapshots) > 1, (
            "Strategy should have been called multiple times"
        )
        assert len(set(strategy.portfolio_snapshots)) == 1, (
            "Portfolio was recreated each bar — expected a single instance"
        )

    async def test_position_opened_on_bar_0_survives_to_last_bar(self) -> None:
        df = _make_ohlcv(60)
        strategy = _TrackingStrategy()
        provider = _StubProvider(df)
        runner = BacktestRunner(config=_default_config(df), strategy=strategy, provider=provider)
        await runner.run()

        assert strategy.bought, "Strategy should have opened a position"
        assert len(strategy.portfolio_snapshots) > 1

    async def test_portfolio_not_instantiated_inside_loop(self) -> None:
        df = _make_ohlcv(60)
        strategy = _TrackingStrategy()
        provider = _StubProvider(df)
        runner = BacktestRunner(config=_default_config(df), strategy=strategy, provider=provider)
        await runner.run()

        unique_ids = set(strategy.portfolio_snapshots)
        assert len(unique_ids) == 1, f"Expected 1 portfolio instance, got {len(unique_ids)}"
