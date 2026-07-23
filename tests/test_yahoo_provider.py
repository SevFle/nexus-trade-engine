"""Happy-path tests for :mod:`engine.data.yahoo_provider`.

:class:`engine.data.yahoo_provider.YahooFinanceProvider` is the historical
OHLCV adapter that fetches bars from the public Yahoo Finance v8 chart API
and returns them as a :class:`polars.DataFrame` implementing the
:class:`engine.data.provider.IDataProvider` contract.

These tests never hit the network: the outbound ``GET /v8/finance/chart/...``
is intercepted by :class:`httpx.MockTransport`, which returns a canned chart
payload. This keeps the suite hermetic and deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import polars as pl
import pytest

from engine.data.provider import DataValidationError, IDataProvider
from engine.data.yahoo_provider import (
    POLARS_OHLCV_COLUMNS,
    YahooFinanceProvider,
    YahooProviderError,
    normalize_symbol,
)

# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

# Three daily bars for AAPL. The third bar has a null close, which Yahoo
# emits for halted / look-ahead-protected sessions; the provider must drop it
# so downstream indicators never see a half-formed bar.
_TS = [1_700_000_000, 1_700_086_400, 1_700_172_800]
_CHART_PAYLOAD: dict[str, Any] = {
    "chart": {
        "result": [
            {
                "meta": {"symbol": "AAPL"},
                "timestamp": _TS,
                "indicators": {
                    "quote": [
                        {
                            "open": [100.0, 101.0, None],
                            "high": [105.0, 106.0, None],
                            "low": [99.0, 100.5, None],
                            "close": [104.0, 105.0, None],
                            "volume": [1_000_000, 1_100_000, None],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}


def _mock_transport(payload: dict[str, Any] = _CHART_PAYLOAD) -> httpx.MockTransport:
    """Build a MockTransport that replies with ``payload`` for any request."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _build_provider(transport: httpx.MockTransport) -> YahooFinanceProvider:
    """Wire a provider to a MockTransport-backed AsyncClient (no network)."""
    client = httpx.AsyncClient(
        transport=transport,
        base_url="https://query1.finance.yahoo.com",
    )
    return YahooFinanceProvider(client=client, enable_cache=False)


@pytest.fixture
async def make_yahoo_provider():
    """Factory that yields :class:`YahooFinanceProvider` instances over mock transports.

    Every instance built through this fixture is torn down via the provider's
    public :meth:`YahooFinanceProvider.aclose` method, so tests never reach
    into ``provider._client`` to clean up. Pass an explicit ``transport`` (for
    example an :class:`httpx.MockTransport` that returns 404/503) for the
    error-path tests; the default returns the happy-path chart payload.
    """
    built: list[YahooFinanceProvider] = []

    def _factory(transport: httpx.MockTransport | None = None) -> YahooFinanceProvider:
        provider = _build_provider(transport or _mock_transport())
        built.append(provider)
        return provider

    yield _factory

    for provider in built:
        await provider.aclose()


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


async def test_fetch_ohlcv_returns_canonical_schema(make_yahoo_provider) -> None:
    """A successful Yahoo chart response is parsed into the canonical OHLCV frame.

    This is the provider's happy path: a mocked ``GET`` returns three daily
    bars (one with a null close that must be dropped), and the resulting
    :class:`polars.DataFrame` must:

    * carry exactly the canonical ``date, open, high, low, close, volume``
      columns in that order,
    * have a tz-aware (UTC) ``Datetime`` timestamp column,
    * be sorted ascending by ``date``,
    * drop the null-close bar, and
    * round-trip the real OHLCV values from the payload.
    """
    provider = make_yahoo_provider()

    df = await provider.fetch_ohlcv("aapl", period="1mo", interval="1d")

    # --- schema -----------------------------------------------------------
    assert set(df.columns) == set(POLARS_OHLCV_COLUMNS)
    assert df.columns == list(POLARS_OHLCV_COLUMNS), "columns must be in canonical order"
    assert df.schema["date"] == pl.Datetime("us", "UTC")
    for col in ("open", "high", "low", "close"):
        assert df.schema[col] == pl.Float64
    assert df.schema["volume"] == pl.Int64

    # --- null-close bar dropped, two clean bars remain --------------------
    assert df.height == 2

    # --- ascending by date ------------------------------------------------
    dates = df["date"].to_list()
    assert dates == sorted(dates)

    # --- values round-trip from the payload -------------------------------
    assert df["open"].to_list() == [100.0, 101.0]
    assert df["high"].to_list() == [105.0, 106.0]
    assert df["low"].to_list() == [99.0, 100.5]
    assert df["close"].to_list() == [104.0, 105.0]
    assert df["volume"].to_list() == [1_000_000, 1_100_000]

    # first timestamp maps to the epoch seconds Yahoo returned (UTC)
    assert df["date"][0] == datetime.fromtimestamp(_TS[0], tz=UTC)


async def test_fetch_ohlcv_hits_correct_yahoo_endpoint(make_yahoo_provider) -> None:
    """The request must target ``/v8/finance/chart/{SYMBOL}`` with the right params."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=_CHART_PAYLOAD)

    provider = make_yahoo_provider(httpx.MockTransport(handler))
    await provider.fetch_ohlcv("MSFT", period="3mo", interval="1d")

    assert captured["path"] == "/v8/finance/chart/MSFT"
    assert captured["params"]["range"] == "3mo"
    assert captured["params"]["interval"] == "1d"


def test_load_data_sync_bridge_returns_same_frame(make_yahoo_provider) -> None:
    """``load_data`` drives the async fetch synchronously and returns the frame."""
    provider = make_yahoo_provider()
    df = provider.load_data("AAPL", period="1y", interval="1d")
    assert df.height == 2
    assert df.columns == list(POLARS_OHLCV_COLUMNS)


def test_implements_idata_provider_interface(make_yahoo_provider) -> None:
    """The provider is a recognised :class:`IDataProvider` implementation."""
    assert issubclass(YahooFinanceProvider, IDataProvider)
    assert YahooFinanceProvider.name == "yahoo"

    provider = make_yahoo_provider()
    # ``validate`` must accept a valid ticker and reject a bad one.
    assert provider.validate("AAPL") is True
    with pytest.raises(DataValidationError):
        provider.validate("../etc/passwd")


# --------------------------------------------------------------------------- #
# error-path sanity (kept minimal — focus is the happy path above)
# --------------------------------------------------------------------------- #


async def test_fetch_ohlcv_raises_on_http_client_error(make_yahoo_provider) -> None:
    """A 4xx (unknown/delisted symbol) surfaces as :class:`DataValidationError`."""
    transport = httpx.MockTransport(lambda _r: httpx.Response(404, text="not found"))
    provider = make_yahoo_provider(transport)
    with pytest.raises(DataValidationError):
        await provider.fetch_ohlcv("ZZZZZ", period="1mo")


async def test_fetch_ohlcv_raises_on_server_error(make_yahoo_provider) -> None:
    """A 5xx Yahoo outage surfaces as :class:`YahooProviderError`."""
    transport = httpx.MockTransport(lambda _r: httpx.Response(503, text="busy"))
    provider = make_yahoo_provider(transport)
    with pytest.raises(YahooProviderError):
        await provider.fetch_ohlcv("AAPL", period="1mo")


def test_normalize_symbol_rejects_path_injection() -> None:
    """Symbol validation is the SSRF guard for the chart path segment."""
    assert normalize_symbol(" aapl ") == "AAPL"
    with pytest.raises(DataValidationError):
        normalize_symbol("EV/IL")
    with pytest.raises(DataValidationError):
        normalize_symbol("..")


# --------------------------------------------------------------------------- #
# lifecycle / teardown
# --------------------------------------------------------------------------- #


async def test_aclose_is_idempotent() -> None:
    """Calling :meth:`aclose` more than once must never raise.

    Hosts and test fixtures reach for the public teardown hook (sometimes from
    a ``finally`` plus an explicit cleanup), so a repeated close has to be a
    no-op. Before the fix ``self._client`` was never cleared, so the second
    call re-invoked ``client.aclose()`` on an already-closed client.
    """
    provider = _build_provider(_mock_transport())

    await provider.aclose()  # first close — closes the injected client
    await provider.aclose()  # second close — must be a no-op, not raise

    # The reference is cleared so the provider no longer points at a closed
    # client (which is what lets fetch_ohlcv rebuild one on demand).
    assert provider._client is None


async def test_fetch_ohlcv_works_after_aclose(monkeypatch) -> None:
    """Fetching after teardown lazily rebuilds a usable client.

    Before the fix :meth:`aclose` left ``self._client`` pointing at the closed
    client, so the next :meth:`fetch_ohlcv` failed with
    ``RuntimeError: Cannot send a request, as the client has been closed``.
    Now teardown clears the reference and the next fetch builds a fresh,
    short-lived client.

    The lazily-built client (see ``_get_chart``) has no transport of its own,
    so we intercept ``httpx.AsyncClient`` construction and route it through a
    :class:`httpx.MockTransport` to keep the test hermetic — this is exactly
    the lazy-creation path the fix unlocks.
    """
    transport = _mock_transport()
    real_async_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        kwargs["base_url"] = "https://query1.finance.yahoo.com"
        return real_async_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    provider = _build_provider(_mock_transport())  # injected (mock) client
    await provider.aclose()
    assert provider._client is None

    df = await provider.fetch_ohlcv("AAPL", period="1mo", interval="1d")

    assert df.columns == list(POLARS_OHLCV_COLUMNS)
    assert df.height == 2  # null-close bar dropped, two clean bars remain
    assert df["close"].to_list() == [104.0, 105.0]

    await provider.aclose()  # teardown of the lazily-built client is safe too
