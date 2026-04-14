"""
IStrategy interface for the SDK.

This is the standalone version that third-party developers install.
It mirrors the engine's plugins.sdk but without engine dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, Field

from nexus_sdk.signals import Signal


class StrategyConfig(BaseModel):
    strategy_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class DataFeed(BaseModel):
    feed_type: str
    symbols: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class MarketState(BaseModel):
    timestamp: Any = None
    prices: dict[str, float] = Field(default_factory=dict)
    volumes: dict[str, int] = Field(default_factory=dict)
    ohlcv: dict[str, list[dict]] = Field(default_factory=dict)
    news: list[dict] = Field(default_factory=list)
    sentiment: dict[str, float] = Field(default_factory=dict)
    macro: dict[str, Any] = Field(default_factory=dict)
    order_book: dict[str, dict] = Field(default_factory=dict)

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


class IStrategy(ABC):
    """The strategy plugin interface. Implement this to create a Nexus plugin."""

    @property
    @abstractmethod
    def id(self) -> str: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def version(self) -> str: ...

    @property
    def author(self) -> str:
        return "unknown"

    @property
    def description(self) -> str:
        return ""

    @abstractmethod
    async def initialize(self, config: StrategyConfig) -> None: ...

    @abstractmethod
    async def dispose(self) -> None: ...

    @abstractmethod
    async def evaluate(self, portfolio, market: MarketState, costs) -> list[Signal]: ...

    async def on_order_fill(self, fill: dict) -> None:
        pass

    async def on_market_open(self) -> None:
        pass

    async def on_market_close(self) -> None:
        pass

    @abstractmethod
    def get_config_schema(self) -> dict: ...

    def get_required_data_feeds(self) -> list[DataFeed]:
        return [DataFeed(feed_type="ohlcv")]

    def get_min_history_bars(self) -> int:
        return 50

    def get_watchlist(self) -> list[str]:
        return []
