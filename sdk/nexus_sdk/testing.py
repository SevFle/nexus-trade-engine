"""
Testing utilities for strategy developers.

Provides a lightweight backtest runner and mock objects
so developers can test strategies without the full engine.
"""

from __future__ import annotations

from nexus_sdk.strategy import IStrategy, StrategyConfig, MarketState
from nexus_sdk.types import PortfolioSnapshot, Money, CostBreakdown
from nexus_sdk.signals import Signal, Side


class MockCostModel:
    """Simple cost model for local testing."""

    def __init__(self, spread_bps: float = 5.0, slippage_bps: float = 10.0):
        self.spread_bps = spread_bps
        self.slippage_bps = slippage_bps

    def estimate_total(self, symbol: str, quantity: int, price: float, side: str, avg_volume: int = 0):
        spread = price * (self.spread_bps / 10_000)
        slippage = price * (self.slippage_bps / 10_000) * quantity
        return CostBreakdown(
            spread=Money(spread),
            slippage=Money(slippage),
        )

    def estimate_pct(self, symbol: str, price: float, side: str = "buy") -> float:
        return (self.spread_bps + self.slippage_bps) * 2 / 10_000


class StrategyTestHarness:
    """
    Lightweight test runner for strategy plugins.

    Usage:
        harness = StrategyTestHarness(MyStrategy())
        await harness.setup(params={"threshold": 0.7})
        signals = await harness.tick(prices={"AAPL": 150.0})
        harness.assert_buy("AAPL", signals)
    """

    def __init__(self, strategy: IStrategy, initial_cash: float = 100_000.0):
        self.strategy = strategy
        self.cost_model = MockCostModel()
        self.portfolio = PortfolioSnapshot(
            cash=initial_cash,
            total_value=initial_cash,
        )
        self.signals_history: list[list[Signal]] = []

    async def setup(self, params: dict = None, secrets: dict = None):
        config = StrategyConfig(
            strategy_id=self.strategy.id,
            params=params or {},
            secrets=secrets or {},
        )
        await self.strategy.initialize(config)

    async def tick(
        self,
        prices: dict[str, float] = None,
        ohlcv: dict[str, list[dict]] = None,
        news: list[dict] = None,
    ) -> list[Signal]:
        market = MarketState(
            prices=prices or {},
            ohlcv=ohlcv or {},
            news=news or [],
        )
        signals = await self.strategy.evaluate(self.portfolio, market, self.cost_model)
        self.signals_history.append(signals)
        return signals

    async def teardown(self):
        await self.strategy.dispose()

    # ── Assertion helpers ──

    @staticmethod
    def assert_buy(symbol: str, signals: list[Signal]):
        buys = [s for s in signals if s.symbol == symbol and s.side == Side.BUY]
        assert len(buys) > 0, f"Expected BUY signal for {symbol}, got none"

    @staticmethod
    def assert_sell(symbol: str, signals: list[Signal]):
        sells = [s for s in signals if s.symbol == symbol and s.side == Side.SELL]
        assert len(sells) > 0, f"Expected SELL signal for {symbol}, got none"

    @staticmethod
    def assert_no_signals(signals: list[Signal]):
        trades = [s for s in signals if s.side != Side.HOLD]
        assert len(trades) == 0, f"Expected no trade signals, got {len(trades)}"
