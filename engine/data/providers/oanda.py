"""OANDA v20 forex adapter."""

from __future__ import annotations

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

OANDA_LIVE_BASE = "https://api-fxtrade.oanda.com"
OANDA_PRACTICE_BASE = "https://api-fxpractice.oanda.com"

GRANULARITY_MAP = {
    "1m": "M1",
    "5m": "M5",
    "15m": "M15",
    "1h": "H1",
    "4h": "H4",
    "1d": "D",
    "1wk": "W",
}

PERIOD_COUNT = {
    "1d": 24,
    "5d": 120,
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y": 365,
    "2y": 730,
    "5y": 1825,
    "ytd": 365,
    "max": 5000,
}


class OandaDataProvider(HTTPProviderBase, IDataProvider):
    def __init__(
        self,
        *,
        api_key: str,
        environment: str = "practice",
        client: httpx.AsyncClient | None = None,
        cache: ProviderCache | None = None,
    ) -> None:
        if not api_key:
            raise FatalProviderError("oanda api_key is required")
        self._api_key = api_key
        base = OANDA_LIVE_BASE if environment == "live" else OANDA_PRACTICE_BASE

        capability = DataProviderCapability(
            name="oanda",
            asset_classes=frozenset({AssetClass.FOREX}),
            supports_realtime=True,
            min_interval="1m",
            rate_limit=RateLimit(requests_per_minute=120, burst=10),
            requires_api_key=True,
        )
        HTTPProviderBase.__init__(
            self,
            capability,
            base,
            client=client,
            cache=cache,
            default_headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        if interval not in GRANULARITY_MAP:
            raise FatalProviderError(f"oanda invalid interval {interval}")
        if period not in PERIOD_COUNT:
            raise FatalProviderError(f"oanda invalid period {period}")

        # OANDA wire format uses ``EUR_USD`` while callers may pass
        # ``EUR/USD``. Normalise *before* the cache key so both forms
        # resolve to the same cached entry.
        instrument = symbol.replace("/", "_").upper()
        cache_key = ProviderCache.make_key(
            "oanda", "ohlcv", symbol=instrument, period=period, interval=interval
        )
        cached = await self._cache.get_dataframe(cache_key)
        if cached is not None:
            return cached

        encoded = encode_path_segment(instrument)
        data = await self._request_json(
            "GET",
            f"/v3/instruments/{encoded}/candles",
            params={
                "granularity": GRANULARITY_MAP[interval],
                "count": min(PERIOD_COUNT[period], 5000),
                "price": "M",
            },
        )
        df = self._parse_candles(data)
        df = normalise_ohlcv(df)
        await self._cache.set_dataframe(cache_key, df, DEFAULT_OHLCV_TTL_S)
        return df

    async def get_latest_price(self, symbol: str) -> float | None:
        # Forex closes weekends — fall back to a wider window if 1m candles
        # are empty so callers don't get spurious ``None`` mid-week.
        df = await self.get_ohlcv(symbol, period="1d", interval="1m")
        if df.empty:
            df = await self.get_ohlcv(symbol, period="5d", interval="1h")
        if df.empty:
            return None
        return float(df["close"].iloc[-1])

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
        raise FatalProviderError("oanda does not offer options chain")

    async def get_orderbook(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        instrument = symbol.replace("/", "_").upper()
        encoded = encode_path_segment(instrument)
        data = await self._request_json("GET", f"/v3/instruments/{encoded}/orderBook")
        buckets = ((data or {}).get("orderBook") or {}).get("buckets") or []
        rows = [
            (
                float(b["price"]),
                float(b.get("longCountPercent", 0.0)),
                float(b.get("shortCountPercent", 0.0)),
            )
            for b in buckets[:depth]
        ]
        return pd.DataFrame(rows, columns=["price", "long_pct", "short_pct"])

    def stream_prices(self, symbols: list[str]) -> AsyncIterator[dict[str, float]]:
        raise FatalProviderError("oanda streaming uses a separate stream-pricing endpoint")

    async def health_check(self) -> HealthCheckResult:
        return await self._probe_health(path="/v3/accounts")

    @staticmethod
    def _parse_candles(payload: dict) -> pd.DataFrame:
        candles = (payload or {}).get("candles") or []
        complete = [c for c in candles if c.get("complete", True)]
        if not complete:
            return pd.DataFrame()
        index = pd.to_datetime([c["time"] for c in complete], utc=True)
        return pd.DataFrame(
            {
                "open": [float(c["mid"]["o"]) for c in complete],
                "high": [float(c["mid"]["h"]) for c in complete],
                "low": [float(c["mid"]["l"]) for c in complete],
                "close": [float(c["mid"]["c"]) for c in complete],
                "volume": [float(c.get("volume", 0)) for c in complete],
            },
            index=index,
        )
