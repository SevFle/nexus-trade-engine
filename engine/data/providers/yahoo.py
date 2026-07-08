"""Yahoo Finance adapter via the public ``query2.finance.yahoo.com`` API.

No API key required, intended as the default fallback for equities/ETFs.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import pandas as pd
import polars as pl

from engine.data.providers._cache import ProviderCache
from engine.data.providers._http import (
    DEFAULT_OHLCV_TTL_S,
    HTTPProviderBase,
    encode_path_segment,
    normalise_ohlcv,
    validate_symbol,
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

# ---------------------------------------------------------------------------
# YahooFinanceProvider — Polars-native historical OHLCV adapter.
#
# A separate, purpose-built provider that returns :class:`polars.DataFrame`
# frames rather than pandas. Polars is a *core* dependency of the engine
# (pandas is dev-only), so downstream consumers prefer this adapter for
# analytical pipelines. It is intentionally decoupled from the pandas-typed
# :class:`IDataProvider` interface and owns its own HTTP/cache lifecycle via
# :class:`HTTPProviderBase` (SSRF guard, rate-limit token bucket, retry,
# byte-capped streaming reads, secret redaction).
# ---------------------------------------------------------------------------

# Canonical Polars OHLCV layout: a UTC ``timestamp`` column followed by the
# lowercase OHLCV columns. Mirrors the pandas providers' canonical set but
# materialises the index as a first-class column (Polars has no index).
POLARS_OHLCV_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

# Supported intervals mapped to Yahoo's chart-API code and the maximum
# calendar-day lookback Yahoo serves for that granularity. ``None`` means
# "no enforced cap" (daily and coarser). These caps mirror Yahoo's server-
# side limits; validating up front yields a clear ``FatalProviderError``
# instead of an opaque upstream "Too much data requested" payload.
YF_INTERVALS: dict[str, tuple[str, int | None]] = {
    "1m": ("1m", 30),
    "2m": ("2m", 60),
    "5m": ("5m", 60),
    "15m": ("15m", 60),
    "30m": ("30m", 60),
    "60m": ("60m", 730),
    "1h": ("60m", 730),
    "90m": ("90m", 60),
    "1d": ("1d", None),
    "5d": ("5d", None),
    "1wk": ("1wk", None),
    "1mo": ("1mo", None),
    "3mo": ("3mo", None),
}

# Sensible default window when neither an explicit start/end nor a named
# period is supplied. Daily is the most common analytical cadence.
_DEFAULT_PERIOD = "1y"


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

        encoded = encode_path_segment(symbol)
        data = await self._request_json(
            "GET",
            f"/v8/finance/chart/{encoded}",
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
        valid = [validate_symbol(s) for s in symbols]
        data = await self._request_json(
            "GET",
            "/v7/finance/quote",
            params={"symbols": ",".join(valid)},
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


class YahooFinanceProvider(HTTPProviderBase):
    """Polars-native historical OHLCV provider for Yahoo Finance.

    Fetches bars from the public ``query2.finance.yahoo.com`` chart API
    (no API key required) and returns :class:`polars.DataFrame` frames with
    the canonical schema::

        timestamp : Datetime[us, UTC]   (ascending)
        open      : Float64
        high      : Float64
        low       : Float64
        close     : Float64
        volume    : Int64

    This adapter is deliberately decoupled from the pandas-typed
    :class:`IDataProvider` contract so callers get a statically-typed Polars
    return value without fighting the legacy pandas interface. It reuses
    :class:`HTTPProviderBase` for all transport safety guarantees (SSRF
    host-pinning, rate-limit token bucket, bounded retries, byte-capped
    streaming reads, secret redaction in error previews).

    Two range modes are supported:

    * **Named period** (default) — pass ``period`` such as ``"1y"``. Maps to
      Yahoo's ``range`` parameter.
    * **Explicit window** — pass ``start``/``end`` (ISO date strings,
      :class:`datetime.date`, or :class:`datetime.datetime`). Maps to
      Yahoo's ``period1``/``period2`` epoch parameters with strict
      validation (start < end, future end clamped to now, intraday windows
      capped to Yahoo's server-side lookback limits).
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        cache: ProviderCache | None = None,
        base_url: str = YAHOO_BASE,
        timeout: float = 10.0,
    ) -> None:
        capability = DataProviderCapability(
            name="yahoo-finance",
            asset_classes=frozenset({AssetClass.EQUITY, AssetClass.ETF}),
            supports_realtime=False,
            min_interval="1m",
            rate_limit=RateLimit(requests_per_minute=120, burst=10),
            requires_api_key=False,
        )
        HTTPProviderBase.__init__(
            self,
            capability,
            base_url,
            client=client,
            cache=cache,
            timeout=timeout,
            default_headers={"User-Agent": "nexus-trade-engine/1.0"},
        )

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        start: str | dt.date | dt.datetime | None = None,
        end: str | dt.date | dt.datetime | None = None,
        interval: str = "1d",
        period: str | None = None,
    ) -> pl.DataFrame:
        """Return historical OHLCV bars for ``symbol`` as a Polars DataFrame.

        Raises :class:`FatalProviderError` for invalid symbols, intervals,
        or date ranges, and for non-recoverable upstream failures. Returns
        an empty DataFrame (with the canonical schema) when Yahoo has no
        bars for the requested window.
        """
        if interval not in YF_INTERVALS:
            raise FatalProviderError(f"yahoo finance unsupported interval: {interval!r}")

        norm = self.normalize_symbol(symbol)
        params = self._resolve_range(start=start, end=end, interval=interval, period=period)

        # ``params`` already carries the resolved Yahoo ``interval`` code
        # (e.g. user-facing "1h" -> "60m"), so we must not also pass
        # ``interval=...`` here — that would collide as a duplicate kwarg.
        cache_key = ProviderCache.make_key(
            "yahoo-finance",
            "ohlcv",
            symbol=norm,
            **params,
        )
        cached = await self._cache.get_json(cache_key)
        if isinstance(cached, dict):
            return self._parse_chart(cached)

        encoded = encode_path_segment(norm)
        payload = await self._request_json(
            "GET",
            f"/v8/finance/chart/{encoded}",
            params=params,
        )
        if not isinstance(payload, dict):
            raise FatalProviderError("yahoo finance returned a non-object chart payload")

        # Cache the raw payload (small, JSON-serialisable) so repeated queries
        # for the same window hit the cache without re-hitting the network.
        await self._cache.set_json(cache_key, payload, DEFAULT_OHLCV_TTL_S)
        return self._parse_chart(payload)

    async def get_latest_price(self, symbol: str) -> float | None:
        """Return the most recent daily close, or ``None`` when unavailable."""
        df = await self.get_ohlcv(symbol, period="5d", interval="1d")
        if df.is_empty():
            return None
        value = df.select(pl.col("close").last()).item()
        return None if value is None else float(value)

    async def health_check(self) -> HealthCheckResult:
        """Probe Yahoo by fetching a one-day AAPL chart."""
        return await self._probe_health(
            path="/v8/finance/chart/AAPL?range=1d&interval=1d"
        )

    # ------------------------------------------------------------------ #
    # Normalisation & validation                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        """Normalise a user-supplied ticker into Yahoo's canonical form.

        * Strip surrounding whitespace and upper-case.
        * Map ``.`` → ``-`` (Yahoo's chart API expects ``BRK-B``, not
          ``BRK.B``).
        * Reject anything that fails :func:`validate_symbol` (path traversal,
          embedded hosts, over-long symbols) so the value is safe to splice
          into the request path.
        """
        if not isinstance(symbol, str):
            raise FatalProviderError(
                f"yahoo finance symbol must be a string, got {type(symbol).__name__}"
            )
        cleaned = symbol.strip().upper()
        if not cleaned:
            raise FatalProviderError("yahoo finance symbol is empty")
        # Security gate: reject path traversal / SSRF on the *raw* input
        # before the dot rewrite. Replacing "." with "-" first would turn
        # "../etc/passwd" into "--/ETC/PASSWD" and slip past
        # :func:`validate_symbol`'s ".." guard (the symbol regex permits
        # "/" and "-"), letting an attacker escape the chart path segment.
        validate_symbol(cleaned)
        # Yahoo's chart API expects "BRK-B" rather than "BRK.B".
        return validate_symbol(cleaned.replace(".", "-"))

    @staticmethod
    def _parse_date(
        value: str | dt.date | dt.datetime, *, name: str
    ) -> dt.datetime:
        """Parse a boundary into a tz-aware UTC :class:`datetime`."""
        if isinstance(value, dt.datetime):
            parsed = value
        elif isinstance(value, dt.date):
            parsed = dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)
        elif isinstance(value, str):
            text = value.strip()
            try:
                parsed = dt.datetime.fromisoformat(text)
            except ValueError:
                try:
                    day = dt.date.fromisoformat(text)
                except ValueError as exc:
                    raise FatalProviderError(
                        f"yahoo finance invalid {name} date: {value!r}"
                    ) from exc
                parsed = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)
        else:
            raise FatalProviderError(
                f"yahoo finance {name} must be str/date/datetime, "
                f"got {type(value).__name__}"
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)

    def _resolve_range(
        self,
        *,
        start: str | dt.date | dt.datetime | None,
        end: str | dt.date | dt.datetime | None,
        interval: str,
        period: str | None,
    ) -> dict[str, Any]:
        """Translate the requested range into Yahoo chart-API query params.

        Returns either ``{"range": ..., "interval": ...}`` (named-period
        mode) or ``{"period1": <epoch>, "period2": <epoch>, "interval": ...}``
        (explicit-window mode). Raises :class:`FatalProviderError` on any
        invalid combination.
        """
        code, max_lookback = YF_INTERVALS[interval]

        # Named-period mode: only interval + range are sent.
        if start is None and end is None:
            rng = period or _DEFAULT_PERIOD
            if rng not in VALID_PERIODS:
                raise FatalProviderError(f"yahoo finance invalid period: {rng!r}")
            return {"interval": code, "range": rng}

        # Explicit-window mode: start is mandatory, end defaults to now.
        if start is None:
            raise FatalProviderError(
                "yahoo finance 'start' is required when 'end' is provided"
            )

        now = dt.datetime.now(dt.UTC)
        start_dt = self._parse_date(start, name="start")
        end_dt = self._parse_date(end, name="end") if end is not None else now

        if start_dt >= end_dt:
            raise FatalProviderError(
                f"yahoo finance start ({start_dt.date()}) must be strictly "
                f"before end ({end_dt.date()})"
            )

        # Yahoo rejects future end timestamps with an error; clamp to now
        # so callers can pass ``end=today`` without special-casing.
        end_dt = min(end_dt, now)

        if max_lookback is not None:
            span_days = (end_dt - start_dt).days
            if span_days > max_lookback:
                raise FatalProviderError(
                    f"yahoo finance interval {interval!r} supports at most "
                    f"{max_lookback} days; requested {span_days} days"
                )

        return {
            "interval": code,
            "period1": int(start_dt.timestamp()),
            "period2": int(end_dt.timestamp()),
        }

    # ------------------------------------------------------------------ #
    # Parsing                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _empty_ohlcv() -> pl.DataFrame:
        """Empty frame carrying the canonical schema (height 0)."""
        return pl.DataFrame(
            schema={
                "timestamp": pl.Datetime("us", "UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
            }
        )

    @classmethod
    def _parse_chart(cls, payload: dict[str, Any]) -> pl.DataFrame:
        """Parse a Yahoo chart-API payload into a canonical Polars frame.

        Handles the full spectrum of Yahoo responses:

        * Top-level / chart-level ``error`` → :class:`FatalProviderError`.
        * Missing ``result`` or empty ``timestamp`` → empty schema frame.
        * Per-bar ``null`` OHLCV values → rows with a null ``close`` are
          dropped (consistent with the pandas providers' ``normalise_ohlcv``).
        * Mismatched series lengths (defensive) → aligned to the timestamp
          length with trailing nulls.
        """
        chart = (payload or {}).get("chart") or {}
        err = chart.get("error")
        if err:
            message = err.get("description") if isinstance(err, dict) else str(err)
            raise FatalProviderError(f"yahoo finance error: {message or err!r}")

        results = chart.get("result") or []
        if not results:
            return cls._empty_ohlcv()

        result = results[0] or {}
        timestamps: list[int] = result.get("timestamp") or []
        if not timestamps:
            return cls._empty_ohlcv()

        indicators = result.get("indicators") or {}
        quotes = indicators.get("quote") or []
        quote: dict[str, Any] = quotes[0] if quotes else {}

        n = len(timestamps)

        def _aligned(key: str) -> list[Any]:
            series = quote.get(key)
            if series is None:
                return [None] * n
            if len(series) == n:
                return list(series)
            # Defensive: Yahoo occasionally returns a short trailing tail.
            return list(series[:n]) + [None] * max(0, n - len(series))

        try:
            df = pl.DataFrame(
                {
                    "timestamp": pl.Series(timestamps, dtype=pl.Int64),
                    "open": _aligned("open"),
                    "high": _aligned("high"),
                    "low": _aligned("low"),
                    "close": _aligned("close"),
                    "volume": _aligned("volume"),
                }
            )
        except (pl.ShapeError, ValueError, TypeError) as exc:
            raise FatalProviderError(
                f"yahoo finance returned a malformed chart payload: {type(exc).__name__}"
            ) from exc

        df = df.with_columns(pl.from_epoch("timestamp", time_unit="s"))
        df = df.with_columns(pl.col("timestamp").dt.replace_time_zone("UTC"))
        df = df.with_columns(
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Int64),
        )
        # Drop bars without a close (incomplete sessions / gaps) and ensure a
        # deterministic ascending order with one row per timestamp.
        df = (
            df.drop_nulls("close")
            .unique(subset=["timestamp"], keep="first")
            .sort("timestamp")
        )
        return df.select(list(POLARS_OHLCV_COLUMNS))
