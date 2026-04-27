"""Binance spot/futures data adapter."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

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

BINANCE_SPOT_BASE = "https://api.binance.com"

INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1wk": "1w",
}

PERIOD_LIMIT = {
    "1d": 1,
    "5d": 5,
    "1mo": 31,
    "3mo": 92,
    "6mo": 183,
    "1y": 366,
    "2y": 731,
    "5y": 1826,
    "ytd": 366,
    "max": 1000,
}


class BinanceDataProvider(HTTPProviderBase, IDataProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
        cache: ProviderCache | None = None,
    ) -> None:
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""

        capability = DataProviderCapability(
            name="binance",
            asset_classes=frozenset({AssetClass.CRYPTO}),
            supports_realtime=True,
            supports_orderbook=True,
            min_interval="1m",
            rate_limit=RateLimit(requests_per_minute=1200, burst=20),
            requires_api_key=False,
        )
        headers = {"X-MBX-APIKEY": self._api_key} if self._api_key else None
        HTTPProviderBase.__init__(
            self,
            capability,
            BINANCE_SPOT_BASE,
            client=client,
            cache=cache,
            default_headers=headers,
        )

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        if interval not in INTERVAL_MAP:
            raise FatalProviderError(f"binance invalid interval {interval}")
        if period not in PERIOD_LIMIT:
            raise FatalProviderError(f"binance invalid period {period}")

        cache_key = ProviderCache.make_key(
            "binance", "ohlcv", symbol=symbol, period=period, interval=interval
        )
        cached = await self._cache.get_dataframe(cache_key)
        if cached is not None:
            return cached

        limit = min(PERIOD_LIMIT[period], 1000)
        klines = await self._request_json(
            "GET",
            "/api/v3/klines",
            params={
                "symbol": symbol.upper(),
                "interval": INTERVAL_MAP[interval],
                "limit": limit,
            },
        )
        df = self._parse_klines(klines)
        df = normalise_ohlcv(df)
        await self._cache.set_dataframe(cache_key, df, DEFAULT_OHLCV_TTL_S)
        return df

    async def get_latest_price(self, symbol: str) -> float | None:
        data = await self._request_json(
            "GET", "/api/v3/ticker/price", params={"symbol": symbol.upper()}
        )
        price = data.get("price") if isinstance(data, dict) else None
        return float(price) if isinstance(price, (str, int, float)) else None

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        data = await self._request_json("GET", "/api/v3/ticker/price")
        wanted = {s.upper() for s in symbols}
        out: dict[str, float] = {}
        rows = data if isinstance(data, list) else []
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            sym = entry.get("symbol")
            price = entry.get("price")
            if sym in wanted and price is not None:
                out[sym] = float(price)
        return out

    async def get_options_chain(
        self, symbol: str, expiry: str | None = None
    ) -> pd.DataFrame:
        raise FatalProviderError("binance options chain lives on a separate API")

    async def get_orderbook(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        if depth not in {5, 10, 20, 50, 100, 500, 1000, 5000}:
            depth = 20
        data = await self._request_json(
            "GET",
            "/api/v3/depth",
            params={"symbol": symbol.upper(), "limit": depth},
        )
        bids = [(float(p), float(q), "bid") for p, q in data.get("bids") or []]
        asks = [(float(p), float(q), "ask") for p, q in data.get("asks") or []]
        return pd.DataFrame(bids + asks, columns=["price", "size", "side"])

    def stream_prices(self, symbols: list[str]) -> AsyncIterator[dict[str, float]]:
        raise FatalProviderError("binance streaming requires websocket subscription")

    async def health_check(self) -> HealthCheckResult:
        return await self._probe_health(path="/api/v3/ping")

    def _sign(self, params: dict[str, str | int]) -> str:
        query = urlencode(params)
        return hmac.new(
            self._api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()

    def _ts(self) -> int:  # pragma: no cover - trivial
        return int(time.time() * 1000)

    @staticmethod
    def _parse_klines(payload: object) -> pd.DataFrame:
        if not isinstance(payload, list) or not payload:
            return pd.DataFrame()
        index = pd.to_datetime([row[0] for row in payload], unit="ms", utc=True)
        return pd.DataFrame(
            {
                "open": [float(row[1]) for row in payload],
                "high": [float(row[2]) for row in payload],
                "low": [float(row[3]) for row in payload],
                "close": [float(row[4]) for row in payload],
                "volume": [float(row[5]) for row in payload],
            },
            index=index,
        )
