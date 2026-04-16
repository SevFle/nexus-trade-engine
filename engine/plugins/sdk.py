from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.core.portfolio import PortfolioState
    from engine.data.market_state import MarketState


class BaseStrategy(ABC):
    """Base class all user strategies must extend."""

    name: str = "unnamed"
    version: str = "0.1.0"

    @abstractmethod
    def on_bar(self, state: MarketState, portfolio: PortfolioState) -> list[dict]:
        """Called on each new bar. Return list of order dicts."""
        ...

    def on_start(self, portfolio: PortfolioState) -> None:  # noqa: B027
        """Optional hook before first bar."""

    def on_end(self, portfolio: PortfolioState) -> None:  # noqa: B027
        """Optional hook after last bar."""
