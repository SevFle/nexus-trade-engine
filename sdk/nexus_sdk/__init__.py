"""
Nexus Trade SDK — build strategy plugins for the Nexus Trade Engine.

Usage:
    from nexus_sdk import IStrategy, Signal, MarketState, PortfolioSnapshot

    class MyStrategy(IStrategy):
        async def evaluate(self, portfolio, market, costs):
            return [Signal.buy("AAPL", strategy_id=self.id)]
"""

from nexus_sdk.strategy import (
    IStrategy,
    StrategyConfig,
    MarketState,
    DataFeed,
)
from nexus_sdk.signals import Signal, Side, SignalStrength
from nexus_sdk.types import PortfolioSnapshot, Money, CostBreakdown

__all__ = [
    "IStrategy",
    "StrategyConfig",
    "MarketState",
    "DataFeed",
    "Signal",
    "Side",
    "SignalStrength",
    "PortfolioSnapshot",
    "Money",
    "CostBreakdown",
]

__version__ = "0.1.0"
