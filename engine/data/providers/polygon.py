"""Polygon.io adapter (equity, options, forex)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from engine.data.providers._cache import ProviderCache
from engine.data.providers._http import (
    DEFAULT_OHLCV_TTL_S,
    HTTPProviderBase,
    encode_path_segment,
    normalise_ohlcv,
)
from engine.data.providers.base import (
    AssetClass,
    DataProviderCapability,
    FatalProviderError,
    HealthCheckResult,
    IDataProvider,
    RateLimit,
    TransientProviderError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

POLYGON_BASE = "https://api.polygon.io"

INTERVAL_MAP = {
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "1h": (1, "hour"),
    "1d": (1, "day"),
    "1wk": (1, "week"),
    "1mo": (1, "month"),
}

PERIOD_DAYS = {
    "1d": 1,
    "5d": 5,
    "1mo": 31,
    "3mo": 92,
    "6mo": 183,
    "1y": 366,
    "2y": 731,
    "5y": 1826,
    "ytd": 366,
    "max": 3650,
}


class PolygonDataProvider(HTTPProviderBase, IDataProvider):
    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        cache: ProviderCache | None = None,
    ) -> None:
        if not api_key:
            raise FatalProviderError("polygon api_key is required")
        self._api_key = api_key

        capability = DataProviderCapability(
            name="polygon",
            asset_classes=frozenset({AssetClass.EQUITY, AssetClass.OPTIONS, AssetClass.FOREX}),
            supports_realtime=True,
            supports_options_chain=True,
            min_interval="1m",
            rate_limit=RateLimit(requests_per_minute=5, burst=2),
            requires_api_key=True,
        )
        HTTPProviderBase.__init__(
            self,
            capability,
            POLYGON_BASE,
            client=client,
            cache=cache,
            default_headers={"Authorization": f"Bearer {api_key}"},
        )

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        if interval not in INTERVAL_MAP:
            raise FatalProviderError(f"polygon invalid interval {interval}")
        if period not in PERIOD_DAYS:
            raise FatalProviderError(f"polygon invalid period {period}")

        cache_key = ProviderCache.make_key(
            "polygon", "ohlcv", symbol=symbol, period=period, interval=interval
        )
        cached = await self._cache.get_dataframe(cache_key)
        if cached is not None:
            return cached

        multiplier, timespan = INTERVAL_MAP[interval]
        # Exclude today's still-forming bar to keep callers free of
        # look-ahead bias in backtests.
        today = datetime.now(UTC).date()
        end = today - timedelta(days=1)
        start = end - timedelta(days=PERIOD_DAYS[period])
        encoded = encode_path_segment(symbol)
        path = (
            f"/v2/aggs/ticker/{encoded}/range/{multiplier}/{timespan}/"
            f"{start.isoformat()}/{end.isoformat()}"
        )
        data = await self._request_json(
            "GET",
            path,
            params={"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        df = self._parse_aggs(data)
        df = normalise_ohlcv(df)
        await self._cache.set_dataframe(cache_key, df, DEFAULT_OHLCV_TTL_S)
        return df

    async def get_latest_price(self, symbol: str) -> float | None:
        encoded = encode_path_segment(symbol)
        data = await self._request_json("GET", f"/v2/last/trade/{encoded}")
        results = data.get("results") or {}
        price = results.get("p")
        return float(price) if isinstance(price, (int, float)) else None

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym in symbols:
            try:
                price = await self.get_latest_price(sym)
            except (FatalProviderError, TransientProviderError):
                continue
            if price is not None:
                out[sym] = price
        return out

    async def get_options_chain(self, symbol: str, expiry: str | None = None) -> pd.DataFrame:
        params: dict[str, str | int] = {"underlying_ticker": symbol, "limit": 250}
        if expiry:
            params["expiration_date"] = expiry
        data = await self._request_json(
            "GET",
            "/v3/reference/options/contracts",
            params=params,
        )
        results = data.get("results") or []
        if not results:
            return pd.DataFrame()
        return pd.DataFrame(results)

    async def get_orderbook(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        raise FatalProviderError("polygon orderbook not implemented in this adapter")

    def stream_prices(self, symbols: list[str]) -> AsyncIterator[dict[str, float]]:
        raise FatalProviderError("polygon streaming requires websocket subscription")

    async def health_check(self) -> HealthCheckResult:
        return await self._probe_health(path="/v3/reference/exchanges?limit=1")

    @staticmethod
    def _parse_aggs(payload: dict) -> pd.DataFrame:
        results = payload.get("results") or []
        if not results:
            return pd.DataFrame()
        index = pd.to_datetime([r["t"] for r in results], unit="ms", utc=True)
        return pd.DataFrame(
            {
                "open": [r.get("o") for r in results],
                "high": [r.get("h") for r in results],
                "low": [r.get("l") for r in results],
                "close": [r.get("c") for r in results],
                "volume": [r.get("v") for r in results],
            },
            index=index,
        )
