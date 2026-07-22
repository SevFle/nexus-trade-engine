"""Polars-native historical OHLCV provider for Yahoo Finance.

:class:`YahooFinanceProvider` is a historical-data adapter in the same family
as :class:`~engine.data.csv_provider.CSVHistoricalDataProvider`: it implements
the synchronous :class:`~engine.data.provider.IDataProvider` contract
(``load_data`` / ``validate``) and returns :class:`polars.DataFrame` objects.

Unlike the CSV provider (which reads bars from disk), this adapter fetches
historical bars from the public Yahoo Finance v8 chart API over HTTP using
:class:`httpx.AsyncClient`. The async fetch lives in :meth:`fetch_ohlcv`; the
synchronous ``load_data`` is a thin bridge that drives that coroutine so the
provider can be dropped into the offline/historical pipeline that expects the
CSV-style interface.

The returned frames have the canonical lowercase OHLCV columns::

    timestamp, open, high, low, close, volume

where ``timestamp`` is a tz-aware (UTC) polars ``Datetime`` and the frame is
sorted ascending. Rows with a null ``close`` (Yahoo inserts these for session
halts and look-ahead protected periods) are dropped so downstream indicators
never see a half-formed bar.

Error contract
--------------

* **Malformed input symbol / bad parameters** → :class:`DataValidationError`
  (e.g. path-traversal attempts, unknown interval). This fails fast because an
  empty frame must never mask a malformed/abusive request.
* **API-confirmed invalid symbol** (HTTP 404 / a Yahoo ``chart.error`` for an
  unknown ticker) → an empty schema'd :class:`polars.DataFrame`. Graceful:
  callers can treat "no data" uniformly.
* **Network / transport failures** (timeout, connection reset, DNS) and HTTP
  5xx → :class:`DataProviderError` (subclass :class:`YahooProviderError`).
* **HTTP 429 rate-limit** → the ``Retry-After`` header (seconds or HTTP-date)
  is honoured, the request is retried with exponential backoff, and — only
  when retries are exhausted — a :class:`RateLimitError` is raised carrying
  the last advised ``retry_after``.

This module is deliberately independent of the live market-data
:class:`~engine.data.providers.yahoo.YahooDataProvider` (pandas-based, wired
into the live registry) — the two coexist without sharing a base class.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx
import polars as pl
import structlog

from engine.data.provider import DataValidationError, IDataProvider

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()

#: Yahoo Finance v8 chart endpoint host. No API key required.
YAHOO_BASE_URL = "https://query1.finance.yahoo.com"

#: Canonical column order produced by this provider. The timestamp column is
#: ``timestamp`` to match :data:`engine.data.provider.OHLCV_COLUMNS`.
POLARS_OHLCV_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

#: Empty-frame schema so callers can rely on column names/dtypes even when the
#: API returns no bars (e.g. an invalid symbol).
_EMPTY_SCHEMA: dict[str, pl.DataType] = {
    "timestamp": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}

#: Yahoo ``range`` tokens accepted as the ``period`` argument.
VALID_PERIODS: frozenset[str] = frozenset(
    {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
)

#: Map our interval names to the Yahoo ``interval`` query tokens. The provider
#: explicitly supports the task-required ``1d``, ``1h`` and ``5m`` intervals.
INTERVAL_MAP: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",
    "1d": "1d",
    "1wk": "1wk",
    "1mo": "1mo",
}

#: Intervals finer than ``1d`` are subject to Yahoo's intraday lookback cap.
_INTRADAY_INTERVALS: frozenset[str] = frozenset({"1m", "5m", "15m", "30m", "1h"})

#: Yahoo only serves ~60 days of intraday history; enforce that on the client
#: side so a wide intraday window fails fast with a clear error instead of
#: silently returning a truncated/clamped frame.
_MAX_INTRADAY_LOOKBACK_DAYS = 60

#: Maximum number of 429 retries after which a :class:`RateLimitError` is
#: raised. The initial request is always attempted, so ``max_retries=3`` → up
#: to 4 total attempts.
DEFAULT_MAX_RETRIES = 3

#: Upper bound on a single retry sleep (seconds) so a hostile ``Retry-After``
#: can never block the event loop for minutes.
DEFAULT_MAX_RETRY_WAIT_S = 30.0

#: Base for the exponential backoff between 429 retries when no
#: ``Retry-After`` hint is supplied.
DEFAULT_BACKOFF_BASE_S = 1.0

# Symbol allow-list. Deliberately excludes ``/`` (a URL path separator) and
# ``..`` (path traversal) so a hostile symbol can never be interpolated into
# the chart path. The class also rejects these explicitly *before* the regex
# as a defence-in-depth invariant. Hyphen is placed last so it is a literal.
_SYMBOL_RE = re.compile(r"^[A-Z0-9._=^-]{1,32}$")

DEFAULT_TIMEOUT_S = 10.0

#: HTTP status code returned by Yahoo when the client is rate-limited.
HTTP_TOO_MANY_REQUESTS = 429
#: HTTP status code at/above which a response is treated as a Yahoo server
#: error (5xx) and surfaced as :class:`DataProviderError`.
_HTTP_SERVER_ERROR_STATUS = 500
#: HTTP status code at/above which a response is treated as a client error.
_HTTP_CLIENT_ERROR_STATUS = 400
#: HTTP status code returned for an unknown / delisted symbol.
_HTTP_NOT_FOUND = 404


class DataProviderError(RuntimeError):
    """Base for all data-provider failures surfaced by this adapter.

    Network/transport errors, HTTP 5xx responses and exhausted rate-limit
    retries all derive from this class so callers can catch the whole family
    with a single ``except DataProviderError``.
    """


class YahooProviderError(DataProviderError):
    """Transient infrastructure failure (network, HTTP 5xx, timeout).

    A subclass of :class:`DataProviderError` so it satisfies
    ``isinstance(exc, DataProviderError)`` while remaining distinguishable.
    """


class RateLimitError(DataProviderError):
    """Raised when Yahoo returns 429 and all retries are exhausted.

    Attributes:
        retry_after: the last ``Retry-After`` value advised by Yahoo, in
            seconds, or ``None`` when no header was present.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def normalize_symbol(symbol: str) -> str:
    """Validate and canonicalise a ticker symbol for Yahoo Finance.

    Strips surrounding whitespace and upper-cases the symbol, then rejects any
    value containing a path separator (``/``), a traversal sequence (``..``),
    or characters outside the safe ticker alphabet. This is the single guard
    that prevents SSRF/path-injection via the ``/v8/finance/chart/{symbol}``
    path segment.

    Raises:
        DataValidationError: if ``symbol`` is not a usable string.
    """
    if not isinstance(symbol, str):
        raise DataValidationError(f"invalid symbol: {symbol!r} (expected str)")
    cleaned = symbol.strip().upper()
    # Defence-in-depth: reject path separators / traversal first, in one pass,
    # so a later regex tweak can never smuggle them into the URL.
    if "/" in cleaned or ".." in cleaned:
        raise DataValidationError(f"invalid symbol: {symbol!r}")
    if not cleaned or not _SYMBOL_RE.fullmatch(cleaned):
        raise DataValidationError(f"invalid symbol: {symbol!r}")
    return cleaned


def _to_epoch_seconds(value: date | datetime | str | int | float) -> int:
    """Coerce a date/datetime/ISO-string/epoch into integer epoch seconds.

    Naive datetimes are assumed to be UTC (callers should pass tz-aware values,
    but we normalise defensively rather than raising).
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, (int, float)):
        return int(value)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DataValidationError(f"invalid date string: {value!r}") from exc
    else:  # pragma: no cover - guarded by callers
        raise DataValidationError(f"unsupported date value: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _parse_retry_after(header: str | None) -> float | None:
    """Parse an HTTP ``Retry-After`` header into seconds.

    Supports both the delta-seconds form (``"120"``) and the HTTP-date form
    (``"Fri, 31 Dec 2025 23:59:59 GMT"``). Returns ``None`` when the header is
    absent or unparseable. Negative/zero deltas are clamped to ``None`` so the
    caller can fall back to its own backoff.
    """
    if not header:
        return None
    value = header.strip()
    if not value:
        return None
    # Delta-seconds: a non-negative integer.
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return seconds if seconds > 0 else None
    # HTTP-date form (RFC 7231).
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = when.timestamp() - datetime.now(tz=UTC).timestamp()
    return delta if delta > 0 else None


class YahooFinanceProvider(IDataProvider):
    """Historical OHLCV bars from Yahoo Finance via the v8 chart API.

    Implements the :class:`~engine.data.provider.IDataProvider` contract used
    by the CSV provider (``load_data`` / ``validate``) but sources its bars
    from Yahoo over HTTP instead of a local file. The async fetch logic lives
    in :meth:`fetch_ohlcv`; synchronous callers use :meth:`load_data`, which
    drives that coroutine on a fresh event loop (or a worker-thread loop when
    a loop is already running, e.g. inside an async app).

    Parameters
    ----------
    client:
        Optional pre-built :class:`httpx.AsyncClient` (e.g. one wired to a
        :class:`httpx.MockTransport` in tests). When ``None`` a short-lived
        client is created per request.
    timeout:
        Per-request timeout in seconds for the owned client.
    max_retries:
        Maximum number of retries on a 429 response before raising
        :class:`RateLimitError`.
    max_retry_wait:
        Cap (seconds) on a single retry sleep derived from ``Retry-After`` /
        backoff, so a hostile header can never stall the event loop.
    enable_cache:
        When ``True``, parsed frames are memoised in-process keyed by
        ``(symbol, range, interval)`` so repeated ``load_data`` calls within a
        process avoid extra network round-trips.
    """

    name = "yahoo"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT_S,
        enable_cache: bool = True,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._max_retry_wait = float(max_retry_wait)
        self._owns_client = client is None
        self._enable_cache = enable_cache
        self._cache: dict[str, pl.DataFrame] = {}

    # ------------------------------------------------------------------
    # IDataProvider (historical / polars interface)
    # ------------------------------------------------------------------

    def validate(self, source: str | Path, **_kwargs: Any) -> bool:
        """Return ``True`` when ``source`` is a valid Yahoo ticker symbol.

        Mirrors :meth:`CSVHistoricalDataProvider.validate`'s contract: bad
        input raises :class:`DataValidationError`, valid input returns ``True``.
        """
        normalize_symbol(str(source))
        return True

    def load_data(
        self,
        source: str | Path,
        *,
        period: str = "1y",
        interval: str = "1d",
        client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> pl.DataFrame:
        """Load historical OHLCV bars for ``source`` (a ticker symbol).

        Drives :meth:`fetch_ohlcv` synchronously. Extra ``start`` / ``end``
        window bounds may be passed in ``kwargs`` and are forwarded.

        Returns a :class:`polars.DataFrame` with columns
        ``timestamp, open, high, low, close, volume`` sorted ascending by
        ``timestamp``.
        """
        coro = self.fetch_ohlcv(
            str(source),
            period=period,
            interval=interval,
            client=client,
            **kwargs,
        )
        return self._run_sync(coro)

    # ------------------------------------------------------------------
    # Async fetch + parse
    # ------------------------------------------------------------------

    async def fetch_ohlcv(
        self,
        symbol: str,
        *,
        period: str = "1y",
        interval: str = "1d",
        start: date | datetime | str | int | float | None = None,
        end: date | datetime | str | int | float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> pl.DataFrame:
        """Fetch and parse OHLCV bars for ``symbol`` from Yahoo Finance.

        Uses :class:`httpx.AsyncClient`. When ``start``/``end`` are given they
        take precedence over ``period`` and are sent as ``period1``/``period2``
        epoch bounds; otherwise ``period`` is sent as the ``range`` token.
        """
        sym = normalize_symbol(symbol)
        self._validate_period(period)
        yahoo_interval = self._validate_interval(interval)

        params = self._build_params(period, yahoo_interval, interval, start=start, end=end)
        cache_key = self._cache_key(sym, params)
        if self._enable_cache and cache_key in self._cache:
            return self._cache[cache_key]

        payload = await self._get_chart(sym, params, client=client)
        df = self._parse_chart(payload, symbol=sym)
        if self._enable_cache and not df.is_empty():
            self._cache[cache_key] = df
        return df

    async def _get_chart(
        self,
        symbol: str,
        params: dict[str, Any],
        *,
        client: httpx.AsyncClient | None,
    ) -> Mapping[str, Any]:
        """Issue the GET to ``/v8/finance/chart/{symbol}`` and return JSON.

        Handles the full status-code matrix:

        * 429 → honour ``Retry-After``, retry with backoff, then
          :class:`RateLimitError`.
        * 5xx → :class:`YahooProviderError`.
        * 4xx (incl. 404) → return an empty mapping so a downstream empty
          :class:`pl.DataFrame` is produced (graceful invalid-symbol handling).
        * transport/timeout errors → :class:`YahooProviderError`.
        """
        owns_client = client is None
        active_client = client if client is not None else self._client
        if active_client is None:
            active_client = httpx.AsyncClient(
                base_url=YAHOO_BASE_URL,
                timeout=self._timeout,
                headers={"User-Agent": "nexus-trade-engine/1.0"},
            )
        try:
            return await self._fetch_with_retries(active_client, symbol, params)
        finally:
            if owns_client and client is None and active_client is not None:
                # We created this client ourselves; close it to free sockets.
                # Best-effort: never let a teardown error mask a real one.
                try:
                    await active_client.aclose()
                except Exception:  # pragma: no cover - defensive
                    logger.debug("yahoo client close failed", symbol=symbol)

    async def _fetch_with_retries(
        self,
        client: httpx.AsyncClient,
        symbol: str,
        params: dict[str, Any],
    ) -> Mapping[str, Any]:
        """Run the chart GET with a 429 retry/backoff loop.

        Non-429 outcomes short-circuit immediately. ``asyncio.sleep`` is only
        awaited between retries, never after a terminal outcome.
        """
        last_retry_after: float | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.get(
                    f"/v8/finance/chart/{symbol}",
                    params=params,
                )
            except httpx.TimeoutException as exc:
                raise YahooProviderError(f"yahoo request timed out for {symbol}") from exc
            except httpx.RequestError as exc:
                raise YahooProviderError(
                    f"yahoo network error for {symbol}: {type(exc).__name__}"
                ) from exc

            if response.status_code == HTTP_TOO_MANY_REQUESTS:
                last_retry_after = _parse_retry_after(response.headers.get("retry-after"))
                if attempt < self._max_retries:
                    delay = self._retry_delay(last_retry_after, attempt)
                    logger.warning(
                        "yahoo rate-limited (429); retrying",
                        symbol=symbol,
                        attempt=attempt + 1,
                        retry_after=last_retry_after,
                        delay=round(delay, 3),
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "yahoo rate-limited (429); retries exhausted",
                    symbol=symbol,
                    attempts=attempt + 1,
                )
                raise RateLimitError(
                    f"yahoo rate-limited for {symbol} after {attempt + 1} attempts",
                    retry_after=last_retry_after,
                )

            if response.status_code >= _HTTP_SERVER_ERROR_STATUS:
                raise YahooProviderError(f"yahoo server error {response.status_code} for {symbol}")
            if response.status_code >= _HTTP_CLIENT_ERROR_STATUS:
                # 404 / 400 typically means an unknown or delisted symbol —
                # surface as an empty frame rather than an exception so callers
                # can treat "no data" uniformly.
                logger.info(
                    "yahoo symbol not served",
                    symbol=symbol,
                    status=response.status_code,
                )
                return {}

            try:
                return response.json()
            except ValueError as exc:
                raise YahooProviderError(f"yahoo returned non-JSON for {symbol}") from exc

        # Unreachable: the loop either returns, raises, or continues — but keep
        # an explicit guard so a future refactor can't silently fall through.
        raise YahooProviderError(  # pragma: no cover - defensive
            f"yahoo request for {symbol} exited retry loop unexpectedly"
        )

    def _retry_delay(self, retry_after: float | None, attempt: int) -> float:
        """Compute the sleep before the next 429 retry.

        Prefer the server's ``Retry-After`` hint; otherwise use exponential
        backoff with jitter. The result is always clamped to
        :attr:`_max_retry_wait`.
        """
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self._max_retry_wait)
        backoff = DEFAULT_BACKOFF_BASE_S * (2**attempt)
        # Deterministic-ish jitter so callers don't thunder-herd in lock-step.
        jitter = backoff * 0.1
        return min(backoff + jitter, self._max_retry_wait)

    @staticmethod
    def _parse_chart(
        payload: Mapping[str, Any] | None, *, symbol: str | None = None
    ) -> pl.DataFrame:
        """Parse a Yahoo v8 chart payload into a polars DataFrame.

        * A non-null ``chart.error`` is treated as an invalid/unknown symbol:
          a warning is logged and an empty schema'd frame is returned (graceful
          handling) rather than raising.
        * Missing ``result`` or empty ``timestamp`` → empty schema'd frame.
        * Rows with a null ``close`` (session halts / protected bars) are
          dropped, matching the pandas live-provider behaviour.
        * The result is sorted ascending by ``timestamp``.
        """
        chart = (payload or {}).get("chart") or {}
        error = chart.get("error")
        if error:
            description = error.get("description") if isinstance(error, Mapping) else str(error)
            # Yahoo returns a chart error (rather than a 4xx) for an
            # unknown/delisted symbol; treat it as "no data" so callers see an
            # empty frame instead of an exception.
            logger.warning(
                "yahoo chart error (returning empty frame)",
                symbol=symbol,
                description=description,
            )
            return pl.DataFrame(schema=_EMPTY_SCHEMA)

        results = chart.get("result") or []
        if not results:
            return pl.DataFrame(schema=_EMPTY_SCHEMA)

        result = results[0] or {}
        timestamps: list[int] = list(result.get("timestamp") or [])
        if not timestamps:
            return pl.DataFrame(schema=_EMPTY_SCHEMA)

        indicators = result.get("indicators") or {}
        quotes = indicators.get("quote") or [{}]
        quote = quotes[0] if quotes else {}

        # Convert epoch seconds → tz-aware UTC datetimes (polars-native).
        timestamp_col = pl.from_epoch(
            pl.Series(timestamps, dtype=pl.Int64), time_unit="s"
        ).dt.replace_time_zone("UTC")

        df = pl.DataFrame(
            {
                "timestamp": timestamp_col,
                "open": pl.Series(quote.get("open"), dtype=pl.Float64),
                "high": pl.Series(quote.get("high"), dtype=pl.Float64),
                "low": pl.Series(quote.get("low"), dtype=pl.Float64),
                "close": pl.Series(quote.get("close"), dtype=pl.Float64),
                "volume": pl.Series(quote.get("volume"), dtype=pl.Int64),
            }
        )
        # Drop half-formed bars (null close). Yahoo emits these for market
        # sessions that haven't produced a print yet.
        df = df.filter(pl.col("close").is_not_null())
        return df.sort("timestamp")

    # ------------------------------------------------------------------
    # Validation + param building helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_period(period: str) -> None:
        if period not in VALID_PERIODS:
            raise DataValidationError(
                f"yahoo invalid period {period!r}; expected one of {sorted(VALID_PERIODS)}"
            )

    @staticmethod
    def _validate_interval(interval: str) -> str:
        if interval not in INTERVAL_MAP:
            raise DataValidationError(
                f"yahoo invalid interval {interval!r}; expected one of {sorted(INTERVAL_MAP)}"
            )
        return INTERVAL_MAP[interval]

    @staticmethod
    def _build_params(
        period: str,
        yahoo_interval: str,
        interval: str,
        *,
        start: date | datetime | str | int | float | None,
        end: date | datetime | str | int | float | None,
    ) -> dict[str, Any]:
        """Build the Yahoo chart query params.

        ``start``/``end`` win over ``period`` (sent as ``period1``/``period2``
        epoch bounds). When only ``end`` is given we reject it (ambiguous).
        """
        if start is None and end is None:
            return {"range": period, "interval": yahoo_interval}
        if start is not None and end is None:
            raise DataValidationError("yahoo date window requires both start and end")
        if start is None and end is not None:
            raise DataValidationError("yahoo date window requires both start and end")

        assert start is not None  # for type checkers
        assert end is not None  # for type checkers
        start_s = _to_epoch_seconds(start)
        end_s = _to_epoch_seconds(end)
        if start_s > end_s:
            raise DataValidationError(
                f"yahoo date window start ({start_s}) is after end ({end_s})"
            )
        if start_s == end_s:
            raise DataValidationError(
                "yahoo date window start equals end; window must be non-empty"
            )

        now_s = int(datetime.now(tz=UTC).timestamp())
        # Clamp a future end to "now" so Yahoo doesn't 400 on an obvious typo.
        end_s = min(end_s, now_s)
        # Enforce Yahoo's intraday lookback cap on the client side.
        if interval in _INTRADAY_INTERVALS:
            max_start = now_s - _MAX_INTRADAY_LOOKBACK_DAYS * 86_400
            if start_s < max_start:
                raise DataValidationError(
                    f"yahoo intraday interval {interval!r} supports at most "
                    f"{_MAX_INTRADAY_LOOKBACK_DAYS} days of history"
                )

        return {
            "period1": start_s,
            "period2": end_s,
            "interval": yahoo_interval,
        }

    def _cache_key(self, symbol: str, params: dict[str, Any]) -> str:
        """Deterministic in-process cache key for a (symbol, params) request."""
        parts = [symbol]
        parts.extend(f"{key}={params[key]}" for key in sorted(params))
        return "|".join(parts)

    # ------------------------------------------------------------------
    # sync ↔ async bridge
    # ------------------------------------------------------------------

    @staticmethod
    def _run_sync(coro: Any) -> pl.DataFrame:
        """Run ``coro`` to completion from synchronous code.

        When no event loop is running we use :func:`asyncio.run`. When a loop
        *is* running (e.g. we were called from inside an async app) we run the
        coroutine on a dedicated worker thread's fresh loop, because
        :func:`asyncio.run` cannot be nested.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_RETRY_WAIT_S",
    "INTERVAL_MAP",
    "POLARS_OHLCV_COLUMNS",
    "VALID_PERIODS",
    "DataProviderError",
    "RateLimitError",
    "YahooFinanceProvider",
    "YahooProviderError",
    "normalize_symbol",
]
