"""Focused unit tests for :mod:`engine.data.yahoo_provider`.

Covers the three acceptance paths the task calls out, plus the rate-limit and
configuration behaviours:

* **Happy path** — AAPL daily data parses into a polars frame with the
  canonical ``timestamp, open, high, low, close, volume`` columns.
* **Invalid symbol** — both a Yahoo ``chart.error`` and an HTTP 404 return an
  *empty* schema'd frame (graceful, no exception).
* **HTTP error handling** — 5xx → :class:`DataProviderError`; transport
  failures (timeout / connection reset) → :class:`DataProviderError`.
* **Rate limit (429)** — the ``Retry-After`` header is honoured, the request
  is retried, and exhausting retries raises :class:`RateLimitError` carrying
  ``retry_after``.
* **Configuration** — intervals (``1d``/``1h``/``5m``) and ``start``/``end``
  date ranges map to the correct Yahoo query params.

All HTTP I/O is faked with :class:`httpx.MockTransport` — no network access.
``asyncio_mode = auto`` (see ``pyproject.toml``) lets async tests run without
per-function decorators.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import polars as pl
import pytest

from engine.data.provider import DataValidationError
from engine.data.yahoo_provider import (
    POLARS_OHLCV_COLUMNS,
    RateLimitError,
    YahooFinanceProvider,
    YahooProviderError,
    normalize_symbol,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

CHART_URL_PREFIX = "https://query1.finance.yahoo.com/v8/finance/chart/"


def _client_for(handler: Any) -> httpx.AsyncClient:
    """Build an AsyncClient backed by a MockTransport handler."""
    return httpx.AsyncClient(
        base_url="https://query1.finance.yahoo.com",
        transport=httpx.MockTransport(handler),
    )


def _aapl_daily_payload() -> dict[str, Any]:
    """A minimal but realistic Yahoo v8 chart payload for AAPL daily bars."""
    # Two daily bars: 2026-01-02 and 2026-01-05 (Mon).
    timestamps = [
        int(datetime(2026, 1, 2, tzinfo=UTC).timestamp()),
        int(datetime(2026, 1, 5, tzinfo=UTC).timestamp()),
    ]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "AAPL"},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [189.0, 192.0],
                                "high": [191.5, 194.0],
                                "low": [188.0, 191.0],
                                "close": [190.5, 193.0],
                                "volume": [1_000_000, 1_200_000],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _ok_handler(payload: dict[str, Any]):
    """Return a handler that always responds 200 with ``payload``."""

    def handler(_request: httpx.Request) -> httpx.Response:
        import json

        return httpx.Response(200, content=json.dumps(payload).encode())

    return handler


# ===========================================================================
# Happy path — AAPL daily data
# ===========================================================================


async def test_happy_path_aapl_daily_returns_canonical_columns():
    """A successful chart response yields the canonical OHLCV frame.

    Columns must be exactly ``timestamp, open, high, low, close, volume`` in
    that order; ``timestamp`` is a tz-aware UTC Datetime; rows are ascending.
    """
    async with _client_for(_ok_handler(_aapl_daily_payload())) as client:
        provider = YahooFinanceProvider(client=client)
        df = await provider.fetch_ohlcv("AAPL", period="1mo", interval="1d")

    assert list(df.columns) == list(POLARS_OHLCV_COLUMNS)
    assert df.schema["timestamp"] == pl.Datetime("us", "UTC")
    assert df.height == 2
    # Ascending by timestamp.
    assert df["timestamp"].is_sorted()
    # Concrete values for the first bar.
    row = df.row(0, named=True)
    assert row["open"] == 189.0
    assert row["high"] == 191.5
    assert row["low"] == 188.0
    assert row["close"] == 190.5
    assert row["volume"] == 1_000_000
    assert row["timestamp"] == datetime(2026, 1, 2, tzinfo=UTC)


async def test_happy_path_drops_null_close_rows():
    """Bars with a null ``close`` (session halt / protected period) are dropped."""
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [
                        int(datetime(2026, 1, 2, tzinfo=UTC).timestamp()),
                        int(datetime(2026, 1, 3, tzinfo=UTC).timestamp()),
                    ],
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0, 11.0],
                                "high": [11.0, 12.0],
                                "low": [9.5, 10.5],
                                "close": [10.5, None],  # second bar incomplete
                                "volume": [100, 0],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    async with _client_for(_ok_handler(payload)) as client:
        provider = YahooFinanceProvider(client=client)
        df = await provider.fetch_ohlcv("AAPL")

    assert df.height == 1
    assert df.row(0, named=True)["close"] == 10.5


async def test_happy_path_request_uses_correct_interval_param():
    """The provider must send our interval names mapped to Yahoo tokens."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        import json

        return httpx.Response(200, content=json.dumps(_aapl_daily_payload()).encode())

    async with _client_for(handler) as client:
        provider = YahooFinanceProvider(client=client)
        await provider.fetch_ohlcv("AAPL", interval="1h")

    # 1h → Yahoo "60m".
    assert seen["interval"] == "60m"


# ===========================================================================
# Invalid symbol → empty DataFrame
# ===========================================================================


async def test_invalid_symbol_chart_error_returns_empty_frame():
    """A Yahoo ``chart.error`` for an unknown symbol returns an empty frame.

    This is the graceful handling contract: callers see "no data" instead of
    an exception for a symbol Yahoo doesn't know.
    """
    payload = {
        "chart": {
            "result": None,
            "error": {
                "code": "Not Found",
                "description": "No data found, symbol may be delisted",
            },
        }
    }
    async with _client_for(_ok_handler(payload)) as client:
        provider = YahooFinanceProvider(client=client)
        df = await provider.fetch_ohlcv("NOSUCHSYMBOL")

    assert df.is_empty()
    # Empty but still schema'd so downstream code can rely on column names.
    assert list(df.columns) == list(POLARS_OHLCV_COLUMNS)


async def test_invalid_symbol_http_404_returns_empty_frame():
    """An HTTP 404 (unknown/delisted symbol) returns an empty frame, not raise."""
    cache_disabled = YahooFinanceProvider(client=None, enable_cache=False)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with _client_for(handler) as client:
        df = await cache_disabled.fetch_ohlcv("ZZZZ", client=client)

    assert df.is_empty()
    assert list(df.columns) == list(POLARS_OHLCV_COLUMNS)


async def test_empty_result_payload_returns_empty_frame():
    """A 200 with an empty ``result`` list returns a schema'd empty frame."""
    payload = {"chart": {"result": [], "error": None}}
    async with _client_for(_ok_handler(payload)) as client:
        provider = YahooFinanceProvider(client=client)
        df = await provider.fetch_ohlcv("AAPL")

    assert df.is_empty()
    assert list(df.columns) == list(POLARS_OHLCV_COLUMNS)


# ===========================================================================
# HTTP error handling
# ===========================================================================


async def test_http_500_raises_data_provider_error():
    """A 5xx response surfaces as a DataProviderError (YahooProviderError)."""
    provider = YahooFinanceProvider(client=None)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    async with _client_for(handler) as client:
        with pytest.raises(YahooProviderError) as exc_info:
            await provider.fetch_ohlcv("AAPL", client=client)

    # YahooProviderError IS-A DataProviderError.
    from engine.data.yahoo_provider import DataProviderError

    assert isinstance(exc_info.value, DataProviderError)
    assert "503" in str(exc_info.value)


async def test_network_timeout_raises_data_provider_error():
    """A transport timeout surfaces as a DataProviderError."""
    provider = YahooFinanceProvider(client=None)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    async with _client_for(handler) as client:
        with pytest.raises(YahooProviderError, match="timed out"):
            await provider.fetch_ohlcv("AAPL", client=client)


async def test_network_connection_error_raises_data_provider_error():
    """A connection-level failure surfaces as a DataProviderError."""
    provider = YahooFinanceProvider(client=None)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client_for(handler) as client:
        with pytest.raises(YahooProviderError, match="network"):
            await provider.fetch_ohlcv("AAPL", client=client)


async def test_non_json_2xx_raises_data_provider_error():
    """A 2xx body that isn't JSON surfaces as a DataProviderError."""
    provider = YahooFinanceProvider(client=None)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>")

    async with _client_for(handler) as client:
        with pytest.raises(YahooProviderError, match="non-JSON"):
            await provider.fetch_ohlcv("AAPL", client=client)


# ===========================================================================
# Rate-limit (429) handling with Retry-After
# ===========================================================================


async def test_rate_limit_429_retries_then_succeeds():
    """A 429 with ``Retry-After`` is retried and the later 200 is returned.

    Validates that the provider honours the header, sleeps between attempts
    (sleep is faked via monkeypatch so the test stays fast), and ultimately
    surfaces the successful response rather than raising.
    """
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        import json

        return httpx.Response(200, content=json.dumps(_aapl_daily_payload()).encode())

    # Patch asyncio.sleep so the real Retry-After wait does not slow the test.
    import engine.data.yahoo_provider as yp

    slept: list[float] = []
    original_sleep = yp.asyncio.sleep

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)
        await original_sleep(0)

    yp.asyncio.sleep = _fake_sleep  # type: ignore[assignment]
    try:
        async with _client_for(handler) as client:
            provider = YahooFinanceProvider(client=client, max_retries=3)
            df = await provider.fetch_ohlcv("AAPL")
    finally:
        yp.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert attempts["n"] == 2  # one 429 + one success
    assert slept == [1.0]  # honoured the Retry-After header exactly
    assert df.height == 2  # parsed the retried success payload


async def test_rate_limit_429_exhausted_raises_rate_limit_error_with_retry_after():
    """When 429 persists past ``max_retries``, RateLimitError carries retry_after."""
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        # Delta-seconds form.
        return httpx.Response(429, headers={"Retry-After": "30"})

    import engine.data.yahoo_provider as yp

    original_sleep = yp.asyncio.sleep

    async def _fake_sleep(delay: float) -> None:
        assert delay <= 30.0  # respected the cap
        await original_sleep(0)

    yp.asyncio.sleep = _fake_sleep  # type: ignore[assignment]
    try:
        async with _client_for(handler) as client:
            provider = YahooFinanceProvider(client=client, max_retries=2)
            with pytest.raises(RateLimitError) as exc_info:
                await provider.fetch_ohlcv("AAPL")
    finally:
        yp.asyncio.sleep = original_sleep  # type: ignore[assignment]

    # initial attempt + 2 retries == 3 total.
    assert attempts["n"] == 3
    assert exc_info.value.retry_after == 30.0
    assert "rate-limited" in str(exc_info.value).lower()


async def test_rate_limit_429_http_date_retry_after_parsed():
    """An HTTP-date ``Retry-After`` is parsed into seconds and retried once."""
    from datetime import timedelta

    future = datetime.now(tz=UTC) + timedelta(seconds=2)
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")

    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": http_date})
        import json

        return httpx.Response(200, content=json.dumps(_aapl_daily_payload()).encode())

    import engine.data.yahoo_provider as yp

    original_sleep = yp.asyncio.sleep

    async def _fake_sleep(delay: float) -> None:
        await original_sleep(0)

    yp.asyncio.sleep = _fake_sleep  # type: ignore[assignment]
    try:
        async with _client_for(handler) as client:
            provider = YahooFinanceProvider(client=client, max_retries=3)
            df = await provider.fetch_ohlcv("AAPL")
    finally:
        yp.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert attempts["n"] == 2
    assert df.height == 2


async def test_rate_limit_429_without_header_uses_backoff_under_cap():
    """No ``Retry-After`` → exponential backoff, but never above ``max_retry_wait``."""
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429)  # no Retry-After

    import engine.data.yahoo_provider as yp

    original_sleep = yp.asyncio.sleep
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await original_sleep(0)

    yp.asyncio.sleep = _fake_sleep  # type: ignore[assignment]
    try:
        async with _client_for(handler) as client:
            provider = YahooFinanceProvider(client=client, max_retries=2, max_retry_wait=5.0)
            with pytest.raises(RateLimitError) as exc_info:
                await provider.fetch_ohlcv("AAPL")
    finally:
        yp.asyncio.sleep = original_sleep  # type: ignore[assignment]

    # Two backoff sleeps (before retries 1→2 and 2→3); all within the cap.
    assert len(sleeps) == 2
    assert all(0 < s <= 5.0 for s in sleeps)
    # No Retry-After header → the error carries a None hint.
    assert exc_info.value.retry_after is None


# ===========================================================================
# Configuration — intervals & date ranges
# ===========================================================================


@pytest.mark.parametrize(
    ("our_interval", "yahoo_token"),
    [("1d", "1d"), ("1h", "60m"), ("5m", "5m")],
)
async def test_intervals_map_to_yahoo_tokens(our_interval: str, yahoo_token: str):
    """The task-required intervals 1d/1h/5m map to the correct Yahoo tokens."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        import json

        return httpx.Response(200, content=json.dumps(_aapl_daily_payload()).encode())

    async with _client_for(handler) as client:
        provider = YahooFinanceProvider(client=client)
        await provider.fetch_ohlcv("AAPL", interval=our_interval)

    assert seen["interval"] == yahoo_token


async def test_date_range_emits_period1_period2_epoch_bounds():
    """``start``/``end`` win over ``period`` and are sent as epoch bounds."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        import json

        return httpx.Response(200, content=json.dumps(_aapl_daily_payload()).encode())

    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 6, 30, tzinfo=UTC)

    async with _client_for(handler) as client:
        provider = YahooFinanceProvider(client=client)
        await provider.fetch_ohlcv("AAPL", start=start, end=end, interval="1d")

    assert "period1" in seen and "period2" in seen
    assert "range" not in seen  # start/end took precedence
    assert int(seen["period1"]) == int(start.timestamp())
    # end is clamped to "now" if in the future; here it's in the past so exact.
    assert int(seen["period2"]) == int(end.timestamp())
    assert seen["interval"] == "1d"


async def test_date_window_requires_both_start_and_end():
    """Passing only ``start`` (or only ``end``) is rejected, not silently used."""
    provider = YahooFinanceProvider(client=None)
    with pytest.raises(DataValidationError, match="both start and end"):
        await provider.fetch_ohlcv("AAPL", start="2025-01-01")


async def test_unknown_interval_rejected():
    """An unsupported interval is rejected before any network call."""
    provider = YahooFinanceProvider(client=None)
    with pytest.raises(DataValidationError, match="interval"):
        await provider.fetch_ohlcv("AAPL", interval="2h")


async def test_unknown_period_rejected():
    provider = YahooFinanceProvider(client=None)
    with pytest.raises(DataValidationError, match="period"):
        await provider.fetch_ohlcv("AAPL", period="7y")


# ===========================================================================
# Symbol validation
# ===========================================================================


def test_normalize_symbol_strips_and_uppercases():
    assert normalize_symbol("  aapl ") == "AAPL"


@pytest.mark.parametrize("bad", ["", "AA/BB", "A..B", "a b", "BAD!"])
def test_normalize_symbol_rejects_malformed(bad: str):
    with pytest.raises(DataValidationError):
        normalize_symbol(bad)


def test_validate_returns_true_for_valid_symbol():
    provider = YahooFinanceProvider(client=None)
    assert provider.validate("AAPL") is True


def test_validate_rejects_path_traversal_symbol():
    provider = YahooFinanceProvider(client=None)
    with pytest.raises(DataValidationError):
        provider.validate("../etc/passwd")


# ===========================================================================
# Sync bridge + cache
# ===========================================================================


def test_load_data_sync_bridge_returns_polars_frame():
    """``load_data`` (sync) drives the async fetch and returns a polars frame."""
    import json

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(_aapl_daily_payload()).encode())

    client = _client_for(handler)
    provider = YahooFinanceProvider(client=client, enable_cache=False)

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client.__aenter__())
        df = provider.load_data("AAPL", period="1mo", interval="1d", client=client)
    finally:
        loop.run_until_complete(client.__aexit__(None, None, None))
        loop.close()

    assert list(df.columns) == list(POLARS_OHLCV_COLUMNS)
    assert df.height == 2


async def test_cache_returns_same_frame_on_repeat_call():
    """Two identical fetches hit the network once (in-process cache)."""
    payload = _aapl_daily_payload()
    import json

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=json.dumps(payload).encode())

    async with _client_for(handler) as client:
        provider = YahooFinanceProvider(client=client, enable_cache=True)
        first = await provider.fetch_ohlcv("AAPL", period="1mo", interval="1d")
        second = await provider.fetch_ohlcv("AAPL", period="1mo", interval="1d")

    assert calls["n"] == 1
    assert first.equals(second)
