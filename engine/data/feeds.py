"""
Market data feeds — abstraction layer for fetching price data.

Supports multiple providers (Yahoo, Alpaca, Polygon, etc.)
with a unified interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import pandas as pd
import structlog

logger = structlog.get_logger()


class MarketDataProvider(ABC):
    """Abstract market data provider."""

    @abstractmethod
    async def get_latest_price(self, symbol: str) -> Optional[float]:
        ...

    @abstractmethod
    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """
        Returns DataFrame with columns: open, high, low, close, volume
        Indexed by datetime.
        """
        ...

    @abstractmethod
    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        ...


class YahooDataProvider(MarketDataProvider):
    """Yahoo Finance data provider (free, good for development)."""

    async def get_latest_price(self, symbol: str) -> Optional[float]:
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            data = ticker.history(period="1d")
            if not data.empty:
                return float(data["Close"].iloc[-1])
            return None
        except Exception as e:
            logger.error("yahoo.price_error", symbol=symbol, error=str(e))
            return None

    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval)
            df.columns = [c.lower() for c in df.columns]
            return df[["open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.error("yahoo.ohlcv_error", symbol=symbol, error=str(e))
            return pd.DataFrame()

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        import yfinance as yf
        prices = {}
        try:
            data = yf.download(symbols, period="1d", group_by="ticker", progress=False)
            for sym in symbols:
                try:
                    if len(symbols) == 1:
                        prices[sym] = float(data["Close"].iloc[-1])
                    else:
                        prices[sym] = float(data[sym]["Close"].iloc[-1])
                except (KeyError, IndexError):
                    pass
        except Exception as e:
            logger.error("yahoo.batch_error", error=str(e))
        return prices


def get_data_provider(provider_name: str = "yahoo") -> MarketDataProvider:
    """Factory for market data providers."""
    providers = {
        "yahoo": YahooDataProvider,
    }
    provider_class = providers.get(provider_name)
    if not provider_class:
        raise ValueError(f"Unknown data provider: {provider_name}. Available: {list(providers.keys())}")
    return provider_class()
