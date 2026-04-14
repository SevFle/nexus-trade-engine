"""
IStrategy — the plugin interface that ALL strategies must implement.

This is the ONLY contract between the engine and strategy developers.
What happens inside evaluate() is entirely up to the developer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, Field

from core.signal import Signal
from core.cost_model import ICostModel
from core.portfolio import PortfolioSnapshot


class StrategyConfig(BaseModel):
    """Configuration passed to a strategy at initialization."""
    strategy_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict, description="Encrypted API keys, etc.")


class MarketState(BaseModel):
    """
    Market data snapshot passed to evaluate().

    Strategies can request specific data feeds in their manifest.
    The engine populates what's available.
    """

    timestamp: Any = None  # datetime
    prices: dict[str, float] = Field(default_factory=dict, description="Symbol -> latest price")
    volumes: dict[str, int] = Field(default_factory=dict, description="Symbol -> latest volume")

    # OHLCV history (most recent N bars)
    ohlcv: dict[str, list[dict]] = Field(
        default_factory=dict,
        description="Symbol -> list of {open, high, low, close, volume, timestamp}",
    )

    # Optional enriched data (populated if strategy requests it)
    news: list[dict] = Field(default_factory=list, description="Recent news items")
    sentiment: dict[str, float] = Field(default_factory=dict, description="Symbol -> sentiment score")
    macro: dict[str, Any] = Field(default_factory=dict, description="Macro indicators (VIX, rates, etc.)")
    order_book: dict[str, dict] = Field(default_factory=dict, description="Symbol -> {bids, asks}")

    def latest(self, symbol: str) -> Optional[float]:
        return self.prices.get(symbol)

    def sma(self, symbol: str, period: int = 20) -> Optional[float]:
        bars = self.ohlcv.get(symbol, [])
        if len(bars) < period:
            return None
        closes = [b["close"] for b in bars[-period:]]
        return sum(closes) / period

    def std(self, symbol: str, period: int = 20) -> Optional[float]:
        bars = self.ohlcv.get(symbol, [])
        if len(bars) < period:
            return None
        closes = [b["close"] for b in bars[-period:]]
        mean = sum(closes) / period
        variance = sum((c - mean) ** 2 for c in closes) / period
        return variance ** 0.5

    def get_news(self, hours: int = 24) -> list[dict]:
        return self.news

    def get_macro_indicators(self) -> dict[str, Any]:
        return self.macro


class DataFeed(BaseModel):
    """Declares a data feed requirement for a strategy."""
    feed_type: str  # "ohlcv", "news", "sentiment", "order_book", "macro"
    symbols: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class IStrategy(ABC):
    """
    The strategy plugin interface.

    Every strategy — whether it's a 10-line moving average crossover or
    a multi-model AI pipeline calling LLMs — implements this interface.

    The engine calls these methods. The developer implements them.
    What happens INSIDE is a complete black box.
    """

    # ── Identity ──

    @property
    @abstractmethod
    def id(self) -> str:
        """Unique strategy identifier."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name."""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """Semantic version string."""
        ...

    @property
    def author(self) -> str:
        return "unknown"

    @property
    def description(self) -> str:
        return ""

    # ── Lifecycle ──

    @abstractmethod
    async def initialize(self, config: StrategyConfig) -> None:
        """
        Called once when the strategy is loaded.

        Use this to:
        - Load ML model weights
        - Initialize LLM clients
        - Set up internal state
        - Validate configuration

        Config includes params (user-tunable) and secrets (API keys).
        """
        ...

    @abstractmethod
    async def dispose(self) -> None:
        """
        Called when the strategy is unloaded.

        Clean up: close connections, free GPU memory, etc.
        """
        ...

    # ── Core evaluation ──

    @abstractmethod
    async def evaluate(
        self,
        portfolio: PortfolioSnapshot,
        market: MarketState,
        costs: ICostModel,
    ) -> list[Signal]:
        """
        The main evaluation method. Called on each tick/bar.

        Args:
            portfolio: Immutable snapshot of current portfolio state.
            market: Current market data (prices, history, news, etc.).
            costs: Cost model for pre-trade cost estimation.

        Returns:
            List of Signal objects (BUY, SELL, or HOLD).
            Empty list = do nothing this tick.

        IMPORTANT: The developer has COMPLETE FREEDOM here.
        Call an LLM, run a neural net, use simple rules — anything goes.
        The engine only cares about the Signal[] output.
        """
        ...

    # ── Event hooks (optional) ──

    async def on_order_fill(self, fill: dict) -> None:
        """Called when one of this strategy's signals results in a fill."""
        pass

    async def on_market_open(self) -> None:
        """Called at market open."""
        pass

    async def on_market_close(self) -> None:
        """Called at market close."""
        pass

    # ── Metadata ──

    @abstractmethod
    def get_config_schema(self) -> dict:
        """
        Return a JSON Schema describing user-configurable parameters.
        The UI auto-generates a settings form from this.
        """
        ...

    def get_required_data_feeds(self) -> list[DataFeed]:
        """Declare which data feeds this strategy needs."""
        return [DataFeed(feed_type="ohlcv")]

    def get_min_history_bars(self) -> int:
        """Minimum number of historical bars needed before evaluation."""
        return 50

    def get_watchlist(self) -> list[str]:
        """Symbols this strategy operates on. Empty = all available."""
        return []
