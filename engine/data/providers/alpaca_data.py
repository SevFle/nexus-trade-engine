"""Alpaca Markets data adapter (equities)."""

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
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

ALPACA_BASE = "https://data.alpaca.markets"

INTERVAL_MAP = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "1h": "1Hour",
    "1d": "1Day",
    "1wk": "1Week",
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


class AlpacaDataProvider(HTTPProviderBase, IDataProvider):
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        client: httpx.AsyncClient | None = None,
        cache: ProviderCache | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise FatalProviderError("alpaca requires api_key and api_secret")
        self._api_key = api_key
        self._api_secret = api_secret

        capability = DataProviderCapability(
            name="alpaca",
            asset_classes=frozenset({AssetClass.EQUITY}),
            supports_realtime=True,
            min_interval="1m",
            rate_limit=RateLimit(requests_per_minute=200, burst=10),
            requires_api_key=True,
        )
        HTTPProviderBase.__init__(
            self,
            capability,
            ALPACA_BASE,
            client=client,
            cache=cache,
            default_headers=self._auth_headers(),
        )

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        if interval not in INTERVAL_MAP:
            raise FatalProviderError(f"alpaca invalid interval {interval}")
        if period not in PERIOD_DAYS:
            raise FatalProviderError(f"alpaca invalid period {period}")

        cache_key = ProviderCache.make_key(
            "alpaca", "ohlcv", symbol=symbol, period=period, interval=interval
        )
        cached = await self._cache.get_dataframe(cache_key)
        if cached is not None:
            return cached

        # Drop sub-second precision (some Alpaca tiers reject it) and clip
        # the request window so the current still-forming bar is excluded
        # — avoids look-ahead in backtests that pass ``end=now()``.
        now = datetime.now(UTC).replace(microsecond=0)
        end = now - timedelta(minutes=1)
        start = (end - timedelta(days=PERIOD_DAYS[period])).replace(microsecond=0)
        encoded = encode_path_segment(symbol)
        data = await self._request_json(
            "GET",
            f"/v2/stocks/{encoded}/bars",
            params={
                "timeframe": INTERVAL_MAP[interval],
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
                "adjustment": "all",
            },
        )
        df = self._parse_bars(data)
        df = normalise_ohlcv(df)
        await self._cache.set_dataframe(cache_key, df, DEFAULT_OHLCV_TTL_S)
        return df

    async def get_latest_price(self, symbol: str) -> float | None:
        encoded = encode_path_segment(symbol)
        data = await self._request_json("GET", f"/v2/stocks/{encoded}/trades/latest")
        trade = (data or {}).get("trade") or {}
        price = trade.get("p")
        return float(price) if isinstance(price, (int, float)) else None

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        data = await self._request_json(
            "GET",
            "/v2/stocks/trades/latest",
            params={"symbols": ",".join(symbols)},
        )
        out: dict[str, float] = {}
        for sym, trade in ((data or {}).get("trades") or {}).items():
            price = (trade or {}).get("p")
            if isinstance(price, (int, float)):
                out[sym] = float(price)
        return out

    async def get_options_chain(
        self, symbol: str, expiry: str | None = None
    ) -> pd.DataFrame:
        raise FatalProviderError("alpaca options chain not implemented")

    async def get_orderbook(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        raise FatalProviderError("alpaca does not expose L2 orderbook")

    def stream_prices(self, symbols: list[str]) -> AsyncIterator[dict[str, float]]:
        raise FatalProviderError("alpaca streaming requires SIP websocket")

    async def health_check(self) -> HealthCheckResult:
        return await self._probe_health(path="/v2/stocks/AAPL/trades/latest")

    def _auth_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
        }

    @staticmethod
    def _parse_bars(payload: dict) -> pd.DataFrame:
        bars = (payload or {}).get("bars") or []
        if not bars:
            return pd.DataFrame()
        index = pd.to_datetime([b["t"] for b in bars], utc=True)
        return pd.DataFrame(
            {
                "open": [b.get("o") for b in bars],
                "high": [b.get("h") for b in bars],
                "low": [b.get("l") for b in bars],
                "close": [b.get("c") for b in bars],
                "volume": [b.get("v") for b in bars],
            },
            index=index,
        )
