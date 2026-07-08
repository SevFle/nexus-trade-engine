"""Unit tests for the Polars-native :class:`YahooFinanceProvider`.

All HTTP traffic is mocked via :class:`httpx.MockTransport` so the suite is
fully hermetic (no network, no API key). Covers the happy path, API errors,
empty-data responses, symbol normalisation, and the full matrix of date-range
validation edge cases.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import polars as pl
import pytest

from engine.data.providers._cache import ProviderCache
from engine.data.providers.base import FatalProviderError, TransientProviderError
from engine.data.providers.yahoo import (
    POLARS_OHLCV_COLUMNS,
    YahooFinanceProvider,
)

# ---------- helpers ----------


def _mock_transport(handler: Any) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(base_url="http://mock", transport=transport)


def _make_cache() -> ProviderCache:
    ProviderCache.reset_for_tests()
    return ProviderCache(url=None)


def _chart_payload(
    *,
    timestamps: list[int] | None = None,
    quote: dict[str, Any] | None = None,
    error: Any = None,
) -> dict[str, Any]:
    timestamps = [1735689600, 1735776000] if timestamps is None else timestamps
    quote = {
        "open": [100.0, 101.0],
        "high": [101.0, 102.0],
        "low": [99.0, 100.0],
        "close": [100.5, 101.5],
        "volume": [1000, 2000],
    } | (quote or {})
    return {
        "chart": {
            "error": error,
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {"quote": [quote]},
                }
            ],
        }
    }


# ---------- parsing ----------


def test_parse_chart_happy_path_returns_polars_dataframe():
    df = YahooFinanceProvider._parse_chart(_chart_payload())
    assert isinstance(df, pl.DataFrame)
    assert df.columns == list(POLARS_OHLCV_COLUMNS)
    assert df.height == 2
    assert df.schema["timestamp"] == pl.Datetime("us", "UTC")
    assert df.schema["close"] == pl.Float64
    assert df.schema["volume"] == pl.Int64
    # ascending order
    assert df["timestamp"].is_sorted()


def test_parse_chart_drops_null_close_rows():
    payload = _chart_payload(
        quote={
            "open": [100.0, 101.0],
            "high": [101.0, None],
            "low": [99.0, None],
            "close": [100.5, None],
            "volume": [1000, None],
        }
    )
    df = YahooFinanceProvider._parse_chart(payload)
    assert df.height == 1
    assert df["close"].null_count() == 0
    assert df["close"].item() == 100.5


def test_parse_chart_missing_result_returns_empty_schema():
    df = YahooFinanceProvider._parse_chart({"chart": {"result": None}})
    assert isinstance(df, pl.DataFrame)
    assert df.is_empty()
    assert df.columns == list(POLARS_OHLCV_COLUMNS)


def test_parse_chart_empty_timestamp_returns_empty_schema():
    df = YahooFinanceProvider._parse_chart(
        _chart_payload(timestamps=[], quote={"open": [], "high": [], "low": [], "close": [], "volume": []})
    )
    assert df.is_empty()
    assert df.columns == list(POLARS_OHLCV_COLUMNS)


def test_parse_chart_error_string_raises_fatal():
    with pytest.raises(FatalProviderError, match="yahoo finance error"):
        YahooFinanceProvider._parse_chart({"chart": {"error": "Invalid symbol"}})


def test_parse_chart_error_object_raises_fatal_with_description():
    payload = _chart_payload(
        error={"code": "Bad Request", "description": "No data found, symbol may be delisted"}
    )
    with pytest.raises(FatalProviderError, match="No data found"):
        YahooFinanceProvider._parse_chart(payload)


def test_parse_chart_aligns_misaligned_series():
    # timestamps longer than quote series -> trailing nulls tolerated.
    payload = _chart_payload(
        timestamps=[1735689600, 1735776000, 1735862400],
        quote={
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1000],
        },
    )
    df = YahooFinanceProvider._parse_chart(payload)
    # only the one complete bar survives (null close dropped).
    assert df.height == 1


# ---------- symbol normalisation ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aapl", "AAPL"),
        (" brk.b ", "BRK-B"),
        ("msft", "MSFT"),
        ("^GSPC", "^GSPC"),
    ],
)
def test_normalize_symbol(raw: str, expected: str) -> None:
    assert YahooFinanceProvider.normalize_symbol(raw) == expected


def test_normalize_symbol_rejects_empty() -> None:
    with pytest.raises(FatalProviderError, match="empty"):
        YahooFinanceProvider.normalize_symbol("   ")


def test_normalize_symbol_rejects_non_string() -> None:
    with pytest.raises(FatalProviderError, match="must be a string"):
        YahooFinanceProvider.normalize_symbol(123)  # type: ignore[arg-type]


def test_normalize_symbol_rejects_path_traversal() -> None:
    with pytest.raises(FatalProviderError):
        YahooFinanceProvider.normalize_symbol("../etc/passwd")


def test_normalize_symbol_rejects_embedded_host() -> None:
    # SSRF guard: a symbol that would redirect the request off-base.
    with pytest.raises(FatalProviderError):
        YahooFinanceProvider.normalize_symbol("http://attacker.example")


# ---------- full async happy path ----------


async def test_get_ohlcv_happy_path_via_mock_transport() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_chart_payload())

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        df = await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
        await provider.aclose()

    assert isinstance(df, pl.DataFrame)
    assert df.columns == list(POLARS_OHLCV_COLUMNS)
    assert df.height == 2
    assert "/v8/finance/chart/AAPL" in seen["path"]
    assert seen["params"]["range"] == "1d" or seen["params"]["range"] == "1mo"
    assert seen["params"]["interval"] == "1d"


async def test_get_ohlcv_explicit_window_sends_epoch_bounds() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_chart_payload())

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        await provider.get_ohlcv(
            "MSFT",
            start="2024-01-01",
            end="2024-03-01",
            interval="1d",
        )
        await provider.aclose()

    assert seen["params"]["interval"] == "1d"
    assert "period1" in seen["params"]
    assert "period2" in seen["params"]
    assert int(seen["params"]["period1"]) == 1704067200  # 2024-01-01 UTC
    assert int(seen["params"]["period2"]) == 1709251200  # 2024-03-01 UTC
    assert "range" not in seen["params"]


async def test_get_ohlcv_caches_response_single_network_call() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_chart_payload())

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        first = await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
        second = await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
        await provider.aclose()

    assert calls["n"] == 1
    assert first.equals(second)


async def test_get_ohlcv_empty_chart_returns_empty_frame() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chart": {"result": None, "error": None}})

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        df = await provider.get_ohlcv("UNKNOWN", period="1mo", interval="1d")
        await provider.aclose()

    assert isinstance(df, pl.DataFrame)
    assert df.is_empty()
    assert df.columns == list(POLARS_OHLCV_COLUMNS)


async def test_get_ohlcv_404_raises_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        with pytest.raises(FatalProviderError, match="HTTP 404"):
            await provider.get_ohlcv("NOPE", period="1mo", interval="1d")
        await provider.aclose()


async def test_get_ohlcv_401_raises_fatal_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        with pytest.raises(FatalProviderError, match="auth error"):
            await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
        await provider.aclose()


async def test_get_ohlcv_500_raises_transient_after_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        with pytest.raises(TransientProviderError, match="HTTP 500"):
            await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
        await provider.aclose()

    # 5xx is retried up to DEFAULT_MAX_ATTEMPTS (3).
    assert calls["n"] == 3


async def test_get_ohlcv_chart_error_body_raises_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"chart": {"error": {"code": 404, "description": "No data found"}}},
        )

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        with pytest.raises(FatalProviderError, match="No data found"):
            await provider.get_ohlcv("DELISTED", period="1mo", interval="1d")
        await provider.aclose()


async def test_get_ohlcv_non_json_response_raises_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        with pytest.raises(FatalProviderError, match="non-JSON"):
            await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
        await provider.aclose()


# ---------- convenience methods ----------


async def test_get_latest_price_returns_last_close() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chart_payload())

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        price = await provider.get_latest_price("AAPL")
        await provider.aclose()

    assert price == 101.5


async def test_get_latest_price_none_when_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chart": {"result": None}})

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        price = await provider.get_latest_price("AAPL")
        await provider.aclose()

    assert price is None


async def test_health_check_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chart_payload())

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        result = await provider.health_check()
        await provider.aclose()

    assert result.name == "yahoo-finance"
    assert result.status.value == "up"
    assert result.latency_ms is not None


# ---------- validation edge cases ----------


async def test_invalid_interval_raises_fatal() -> None:
    cache = _make_cache()
    async with _mock_transport(lambda r: httpx.Response(200, json=_chart_payload())) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        with pytest.raises(FatalProviderError, match="unsupported interval"):
            await provider.get_ohlcv("AAPL", interval="2d")
        await provider.aclose()


async def test_invalid_period_raises_fatal() -> None:
    cache = _make_cache()
    async with _mock_transport(lambda r: httpx.Response(200, json=_chart_payload())) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        with pytest.raises(FatalProviderError, match="invalid period"):
            await provider.get_ohlcv("AAPL", period="weird")
        await provider.aclose()


def test_date_range_start_after_end_raises() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    with pytest.raises(FatalProviderError, match="strictly before end"):
        provider._resolve_range(
            start="2024-06-01", end="2024-01-01", interval="1d", period=None
        )


def test_date_range_start_equals_end_raises() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    with pytest.raises(FatalProviderError, match="strictly before end"):
        provider._resolve_range(
            start="2024-01-01", end="2024-01-01", interval="1d", period=None
        )


def test_date_range_end_without_start_raises() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    with pytest.raises(FatalProviderError, match="'start' is required"):
        provider._resolve_range(start=None, end="2024-01-01", interval="1d", period=None)


def test_date_range_invalid_date_string_raises() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    with pytest.raises(FatalProviderError, match="invalid start date"):
        provider._resolve_range(start="not-a-date", end="2024-01-01", interval="1d", period=None)


def test_date_range_invalid_end_type_raises() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    with pytest.raises(FatalProviderError, match="must be str/date/datetime"):
        provider._resolve_range(
            start="2024-01-01", end=12345, interval="1d", period=None  # type: ignore[arg-type]
        )


def test_date_range_default_period_when_none() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    params = provider._resolve_range(start=None, end=None, interval="1d", period=None)
    assert params == {"interval": "1d", "range": "1y"}


def test_date_range_intraday_lookback_cap_enforced() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    # 1m bars are capped at 30 days; ~5 months far exceeds that.
    with pytest.raises(FatalProviderError, match="at most 30 days"):
        provider._resolve_range(
            start="2024-01-01", end="2024-06-01", interval="1m", period=None
        )


def test_date_range_daily_has_no_lookback_cap() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    # 10-year daily window is valid.
    params = provider._resolve_range(
        start="2014-01-01", end="2024-01-01", interval="1d", period=None
    )
    assert params["interval"] == "1d"
    # Boundaries are emitted as Unix epochs at 00:00 UTC. Compute the
    # expected values from datetimes rather than naive day arithmetic so
    # the assertion stays correct across leap days (2016, 2020).
    assert int(params["period1"]) == int(dt.datetime(2014, 1, 1, tzinfo=dt.UTC).timestamp())
    assert int(params["period2"]) == int(dt.datetime(2024, 1, 1, tzinfo=dt.UTC).timestamp())


def test_date_range_accepts_date_and_datetime_objects() -> None:
    provider = YahooFinanceProvider(client=_mock_transport(lambda r: httpx.Response(200)))
    params = provider._resolve_range(
        start=dt.date(2024, 1, 1),
        end=dt.datetime(2024, 2, 1, 12, 0, tzinfo=dt.UTC),
        interval="1d",
        period=None,
    )
    assert "period1" in params and "period2" in params
    assert int(params["period1"]) == 1704067200


async def test_date_range_future_end_clamped_to_now() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_chart_payload())

    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=400)
    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooFinanceProvider(client=client, cache=cache)
        await provider.get_ohlcv(
            "AAPL",
            start="2020-01-01",
            end=future.isoformat(),
            interval="1d",
        )
        await provider.aclose()

    now_epoch = int(dt.datetime.now(dt.UTC).timestamp())
    assert int(seen["params"]["period2"]) <= now_epoch + 5  # allow slack


# ---------- export sanity ----------


def test_provider_exported_from_package() -> None:
    from engine.data.providers import YahooFinanceProvider as Exported

    assert Exported is YahooFinanceProvider
