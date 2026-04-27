"""CoinGecko adapter (crypto spot, no key required)."""

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

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

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

VALID_INTERVALS = {"1d"}


class CoinGeckoDataProvider(HTTPProviderBase, IDataProvider):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        cache: ProviderCache | None = None,
    ) -> None:
        capability = DataProviderCapability(
            name="coingecko",
            asset_classes=frozenset({AssetClass.CRYPTO}),
            supports_realtime=False,
            min_interval="1d",
            rate_limit=RateLimit(requests_per_minute=30, burst=5),
            requires_api_key=False,
        )
        HTTPProviderBase.__init__(self, capability, COINGECKO_BASE, client=client, cache=cache)

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        if interval not in VALID_INTERVALS:
            raise FatalProviderError(f"coingecko only supports daily ({interval} requested)")
        if period not in PERIOD_DAYS:
            raise FatalProviderError(f"coingecko invalid period {period}")

        cache_key = ProviderCache.make_key(
            "coingecko", "ohlcv", symbol=symbol, period=period
        )
        cached = await self._cache.get_dataframe(cache_key)
        if cached is not None:
            return cached

        coin_id = self._symbol_to_id(symbol)
        data = await self._request_json(
            "GET",
            f"/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": PERIOD_DAYS[period]},
        )
        market = await self._request_json(
            "GET",
            f"/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": PERIOD_DAYS[period], "interval": "daily"},
        )
        df = self._parse_ohlc(data, market)
        df = normalise_ohlcv(df)
        await self._cache.set_dataframe(cache_key, df, DEFAULT_OHLCV_TTL_S)
        return df

    async def get_latest_price(self, symbol: str) -> float | None:
        coin_id = self._symbol_to_id(symbol)
        data = await self._request_json(
            "GET",
            "/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
        )
        usd = (data or {}).get(coin_id, {}).get("usd")
        return float(usd) if isinstance(usd, (int, float)) else None

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        ids = [self._symbol_to_id(s) for s in symbols]
        data = await self._request_json(
            "GET",
            "/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": "usd"},
        )
        out: dict[str, float] = {}
        for sym, coin_id in zip(symbols, ids, strict=True):
            usd = (data or {}).get(coin_id, {}).get("usd")
            if isinstance(usd, (int, float)):
                out[sym] = float(usd)
        return out

    async def get_options_chain(
        self, symbol: str, expiry: str | None = None
    ) -> pd.DataFrame:
        raise FatalProviderError("coingecko has no options data")

    async def get_orderbook(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        raise FatalProviderError("coingecko has no order book data")

    def stream_prices(self, symbols: list[str]) -> AsyncIterator[dict[str, float]]:
        raise FatalProviderError("coingecko has no streaming endpoint")

    async def health_check(self) -> HealthCheckResult:
        return await self._probe_health(path="/ping")

    @staticmethod
    def _symbol_to_id(symbol: str) -> str:
        # CoinGecko uses slug ids ("bitcoin"), users may pass "BTC" or "bitcoin".
        normalised = symbol.lower().strip()
        return {
            "btc": "bitcoin",
            "eth": "ethereum",
            "sol": "solana",
            "ada": "cardano",
            "doge": "dogecoin",
        }.get(normalised, normalised)

    @staticmethod
    def _parse_ohlc(ohlc: object, market: object) -> pd.DataFrame:
        if not isinstance(ohlc, list) or not ohlc:
            return pd.DataFrame()
        rows = ohlc
        index = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
        df = pd.DataFrame(
            {
                "open": [float(r[1]) for r in rows],
                "high": [float(r[2]) for r in rows],
                "low": [float(r[3]) for r in rows],
                "close": [float(r[4]) for r in rows],
            },
            index=index,
        )
        volumes = (market or {}).get("total_volumes", []) if isinstance(market, dict) else []
        vol_map: dict[pd.Timestamp, float] = {}
        for ts_ms, vol in volumes:
            vol_map[pd.to_datetime(ts_ms, unit="ms", utc=True).normalize()] = float(vol)
        df["volume"] = [vol_map.get(idx.normalize(), 0.0) for idx in df.index]
        return df
