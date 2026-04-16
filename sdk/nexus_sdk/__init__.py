"""
Nexus Trade SDK — build strategy plugins for the Nexus Trade Engine.

Usage:
    from nexus_sdk import IStrategy, Signal, MarketState, PortfolioSnapshot

    class MyStrategy(IStrategy):
        async def evaluate(self, portfolio, market, costs):
            return [Signal.buy("AAPL", strategy_id=self.id)]
"""

from nexus_sdk.signals import Side, Signal, SignalStrength
from nexus_sdk.strategy import (
    DataFeed,
    IStrategy,
    MarketState,
    StrategyConfig,
)
from nexus_sdk.types import CostBreakdown, Money, PortfolioSnapshot

__all__ = [
    "CostBreakdown",
    "DataFeed",
    "IStrategy",
    "MarketState",
    "Money",
    "PortfolioSnapshot",
    "Side",
    "Signal",
    "SignalStrength",
    "StrategyConfig",
]

__version__ = "0.1.0"
