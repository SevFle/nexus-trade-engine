from __future__ import annotations

from typing import TYPE_CHECKING

from engine.plugins.sdk import BaseStrategy

if TYPE_CHECKING:
    from engine.core.portfolio import PortfolioState
    from engine.data.market_state import MarketState


class Strategy(BaseStrategy):
    name = "mean_reversion_basic"
    version = "0.1.0"

    def on_bar(self, state: MarketState, portfolio: PortfolioState) -> list[dict]:
        _ = state, portfolio
        return []
