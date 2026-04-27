"""Yahoo Finance adapter via the public ``query2.finance.yahoo.com`` API.

No API key required, intended as the default fallback for equities/ETFs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from engine.data.providers._cache import ProviderCache
from engine.data.providers._http import (
    DEFAULT_OHLCV_TTL_S,
    HTTPProviderBase,
    normalise_ohlcv,
)
from engine.data.providers.base import (
    AssetClass,
    DataProviderCapability,
    FatalProviderError,
    HealthCheckResult,
    IDataProvider,
    RateLimit,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

YAHOO_BASE = "https://query2.finance.yahoo.com"

INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "60m",
    "1d": "1d",
    "1wk": "1wk",
    "1mo": "1mo",
}

VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}


class YahooDataProvider(HTTPProviderBase, IDataProvider):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        cache: ProviderCache | None = None,
    ) -> None:
        capability = DataProviderCapability(
            name="yahoo",
            asset_classes=frozenset({AssetClass.EQUITY, AssetClass.ETF}),
            supports_realtime=False,
            min_interval="1m",
            rate_limit=RateLimit(requests_per_minute=120, burst=10),
            requires_api_key=False,
        )
        HTTPProviderBase.__init__(
            self,
            capability,
            YAHOO_BASE,
            client=client,
            cache=cache,
            default_headers={"User-Agent": "nexus-trade-engine/1.0"},
        )

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        if period not in VALID_PERIODS:
            raise FatalProviderError(f"yahoo invalid period {period}")
        if interval not in INTERVAL_MAP:
            raise FatalProviderError(f"yahoo invalid interval {interval}")

        cache_key = ProviderCache.make_key(
            "yahoo", "ohlcv", symbol=symbol, period=period, interval=interval
        )
        cached = await self._cache.get_dataframe(cache_key)
        if cached is not None:
            return cached

        data = await self._request_json(
            "GET",
            f"/v8/finance/chart/{symbol}",
            params={"range": period, "interval": INTERVAL_MAP[interval]},
        )
        df = self._parse_chart(data)
        df = normalise_ohlcv(df)
        await self._cache.set_dataframe(cache_key, df, DEFAULT_OHLCV_TTL_S)
        return df

    async def get_latest_price(self, symbol: str) -> float | None:
        df = await self.get_ohlcv(symbol, period="5d", interval="1d")
        if df.empty:
            return None
        return float(df["close"].iloc[-1])

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        data = await self._request_json(
            "GET",
            "/v7/finance/quote",
            params={"symbols": ",".join(symbols)},
        )
        out: dict[str, float] = {}
        for entry in data.get("quoteResponse", {}).get("result", []) or []:
            sym = entry.get("symbol")
            price = entry.get("regularMarketPrice")
            if isinstance(sym, str) and isinstance(price, (int, float)):
                out[sym] = float(price)
        return out

    async def get_options_chain(
        self, symbol: str, expiry: str | None = None
    ) -> pd.DataFrame:  # pragma: no cover - thin
        raise FatalProviderError("yahoo options chain not implemented")

    async def get_orderbook(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        raise FatalProviderError("yahoo does not support orderbook")

    def stream_prices(self, symbols: list[str]) -> AsyncIterator[dict[str, float]]:
        raise FatalProviderError("yahoo streaming not supported")

    async def health_check(self) -> HealthCheckResult:
        return await self._probe_health(path="/v8/finance/chart/AAPL?range=1d&interval=1d")

    @staticmethod
    def _parse_chart(payload: dict) -> pd.DataFrame:
        chart = (payload or {}).get("chart", {}) or {}
        if chart.get("error"):
            raise FatalProviderError(f"yahoo error: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            return pd.DataFrame()
        result = results[0]
        timestamps = result.get("timestamp") or []
        indicators = (result.get("indicators") or {}).get("quote") or [{}]
        quote = indicators[0] if indicators else {}
        if not timestamps:
            return pd.DataFrame()
        index = pd.to_datetime(timestamps, unit="s", utc=True)
        return pd.DataFrame(
            {
                "open": quote.get("open", []),
                "high": quote.get("high", []),
                "low": quote.get("low", []),
                "close": quote.get("close", []),
                "volume": quote.get("volume", []),
            },
            index=index,
        )
