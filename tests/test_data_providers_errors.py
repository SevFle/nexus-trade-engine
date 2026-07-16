"""Error-handling & edge-case coverage for the data provider layer.

These tests target the highest-risk *uncovered* branches in the provider
failure-recovery, rate-limit-backoff, malformed-data and
provider-switching/fallback paths — specifically:

* ``HTTPProviderBase._request_json`` translating low-level ``httpx`` failures
  (timeout / connection reset / non-JSON body / 4xx / 429) into the typed
  :class:`TransientProviderError` / :class:`FatalProviderError` contract the
  retry + failover layers depend on.
* :class:`DataProviderRegistry` fail-over on a *fatal* error, the
  "no provider for asset class" → ``None`` recovery on ``get_latest_price``,
  and the "all candidates returned empty" soft-miss path.
* :func:`normalise_ohlcv` recovering a naive ``DatetimeIndex`` to UTC.

All HTTP I/O is faked with ``httpx.MockTransport`` — no network access.
"""

from __future__ import annotations

from typing import Any

import httpx
import pandas as pd
import pytest

from engine.data.providers._cache import ProviderCache
from engine.data.providers._http import HTTPProviderBase, normalise_ohlcv
from engine.data.providers.base import (
    OHLCV_COLUMNS,
    AssetClass,
    DataProviderCapability,
    FatalProviderError,
    HealthCheckResult,
    HealthStatus,
    IDataProvider,
    RateLimit,
    TransientProviderError,
)
from engine.data.providers.registry import (
    DataProviderRegistry,
    ProviderRegistration,
)

# ---------- shared helpers (mirrors tests/test_data_providers.py) ----------


def _make_cache() -> ProviderCache:
    ProviderCache.reset_for_tests()
    return ProviderCache(url=None)


def _mock_transport(handler: Any) -> httpx.AsyncClient:
    """Build a stand-alone async client backed by a MockTransport handler."""
    return httpx.AsyncClient(base_url="http://mock", transport=httpx.MockTransport(handler))


def _make_base(client: httpx.AsyncClient, cache: ProviderCache) -> HTTPProviderBase:
    """An HTTPProviderBase with rate-limiting disabled for snappy tests."""
    cap = DataProviderCapability(
        name="t",
        asset_classes=frozenset({AssetClass.EQUITY}),
        rate_limit=RateLimit(requests_per_minute=0),
    )
    return HTTPProviderBase(cap, "http://mock", client=client, cache=cache)


class _FakeProvider(IDataProvider):
    """Minimal in-memory provider whose behaviour is decided at construction."""

    def __init__(
        self,
        name: str,
        asset_classes: set[AssetClass],
        *,
        ohlcv: pd.DataFrame | None = None,
        price: float | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.capability = DataProviderCapability(
            name=name,
            asset_classes=frozenset(asset_classes),
        )
        self._ohlcv = ohlcv
        self._price = price
        self._raises = raises
        self.calls: list[str] = []

    async def get_ohlcv(self, symbol, period="1y", interval="1d"):
        self.calls.append(f"ohlcv:{symbol}")
        if self._raises:
            raise self._raises
        return self._ohlcv if self._ohlcv is not None else pd.DataFrame()

    async def get_latest_price(self, symbol):
        self.calls.append(f"price:{symbol}")
        if self._raises:
            raise self._raises
        return self._price

    async def get_multiple_prices(self, symbols):
        return {}

    async def get_options_chain(self, symbol, expiry=None):
        return pd.DataFrame()

    async def get_orderbook(self, symbol, depth=20):
        return pd.DataFrame()

    def stream_prices(self, symbols):
        raise FatalProviderError("not implemented")

    async def health_check(self):
        return HealthCheckResult(name=self.capability.name, status=HealthStatus.UP)


def _ohlcv_df(rows: int = 2) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.5] * rows,
            "volume": [1000] * rows,
        },
        index=idx,
    )


# ===========================================================================
# 1. provider failure recovery — httpx transport failures → typed errors
# ===========================================================================


@pytest.mark.asyncio
async def test_provider_timeout_becomes_transient_error_after_retry():
    """A read timeout must surface as a retryable TransientProviderError.

    Covers ``_http.py`` ``except httpx.TimeoutException`` branch — the
    recovery contract that ``call_with_retry`` and registry fail-over depend
    on (a timeout must NOT be classified as fatal, or a blip would take a
    provider out of the pool permanently).
    """
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ReadTimeout("read timed out")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        base = _make_base(client, cache)
        with pytest.raises(TransientProviderError, match="timeout"):
            await base._request_json("GET", "/bars")


@pytest.mark.asyncio
async def test_provider_network_error_becomes_transient_error():
    """A connection-level failure must surface as TransientProviderError.

    Covers ``_http.py`` ``except httpx.RequestError`` branch (connection
    refused / DNS / reset). Like timeouts, these are recoverable so the
    retry layer must see them, not a fatal error.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        base = _make_base(client, cache)
        with pytest.raises(TransientProviderError, match="network"):
            await base._request_json("GET", "/bars")


# ===========================================================================
# 2. malformed data handling
# ===========================================================================


@pytest.mark.asyncio
async def test_non_json_response_raises_fatal():
    """A 2xx body that is not valid JSON must raise FatalProviderError.

    Covers ``_http.py`` ``except ValueError`` → ``returned non-JSON`` branch.
    This is the guard against a misconfigured/compromised upstream returning
    HTML error pages or partial payloads that would otherwise produce a
    cryptic ``json.JSONDecodeError`` deep in an adapter.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        base = _make_base(client, cache)
        with pytest.raises(FatalProviderError, match="non-JSON"):
            await base._request_json("GET", "/bars")


@pytest.mark.asyncio
async def test_http_4xx_non_auth_raises_fatal_with_redacted_preview():
    """A non-auth 4xx must raise FatalProviderError carrying a redacted preview.

    Covers ``_http.py`` ``status >= HTTP_CLIENT_ERROR_MIN`` branch (404 / 400 /
    422 …). Asserts the preview is attached (so ops can diagnose) and that an
    embedded API key in the body is redacted before being folded into the
    error message.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            text='{"error":"not found","apiKey=AKIAIOSFODNN7EXAMPLE"}',
        )

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        base = _make_base(client, cache)
        with pytest.raises(FatalProviderError, match="HTTP 404") as exc_info:
            await base._request_json("GET", "/bars")
    # The diagnostic preview is present but the secret was scrubbed.
    assert "not found" in str(exc_info.value)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(exc_info.value)


def test_normalise_ohlcv_localizes_naive_datetimeindex_to_utc():
    """A naive (tz-less) DatetimeIndex must be localized, not converted.

    Covers ``normalise_ohlcv`` ``elif df.index.tz is None`` branch — the path
    a provider hits when it returns a tz-naive frame (e.g. parsed from a CSV
    without an explicit tz). The result must be UTC and ascending.
    """
    raw = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.1, 2.1],
            "low": [0.9, 1.9],
            "close": [1.0, 2.0],
            "volume": [100, 200],
        },
        # tz-naive DatetimeIndex — exercises the tz_localize("UTC") branch
        index=pd.to_datetime(["2026-01-02", "2026-01-01"]),
    )
    out = normalise_ohlcv(raw)
    assert list(out.columns) == list(OHLCV_COLUMNS)
    assert str(out.index.tz) == "UTC"
    assert out.index.is_monotonic_increasing


# ===========================================================================
# 3. rate-limit backoff
# ===========================================================================


@pytest.mark.asyncio
async def test_http_429_rate_limit_retries_then_raises_transient():
    """A 429 must be retried (transient) and surface as TransientProviderError.

    Covers the ``status in TRANSIENT_STATUS`` branch for 429 specifically and
    confirms ``call_with_retry`` exhausts ``max_attempts`` before giving up —
    so a rate-limited provider is retried in-process *and* then failed over
    by the registry rather than dropped on the first 429.
    """
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, text="slow down")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        base = _make_base(client, cache)
        with pytest.raises(TransientProviderError, match="HTTP 429"):
            await base._request_json("GET", "/bars")
    # default DEFAULT_MAX_ATTEMPTS == 3
    assert attempts["n"] == 3


# ===========================================================================
# 4. provider switching / fallback logic
# ===========================================================================


@pytest.mark.asyncio
async def test_registry_failover_on_fatal_error():
    """A FatalProviderError on the primary must skip to the next candidate.

    Covers ``registry.py`` ``except FatalProviderError`` (``fatal_skip``)
    branch. The transient-failover path is already tested elsewhere, but the
    *fatal* failover (bad creds / unsupported asset on primary) is the
    highest-risk uncovered branch for provider switching: if it raised
    instead of continuing, one misconfigured primary would take the whole
    asset class down.
    """
    registry = DataProviderRegistry()
    primary = _FakeProvider(
        "primary",
        {AssetClass.EQUITY},
        raises=FatalProviderError("invalid credentials"),
    )
    fallback = _FakeProvider(
        "fallback",
        {AssetClass.EQUITY},
        ohlcv=_ohlcv_df(),
    )
    registry.register(ProviderRegistration(provider=primary, priority=1))
    registry.register(ProviderRegistration(provider=fallback, priority=10))

    df, served_by = await registry.get_ohlcv_traced("AAPL", asset_class=AssetClass.EQUITY)
    assert served_by == "fallback"
    assert not df.empty
    assert primary.calls and fallback.calls


@pytest.mark.asyncio
async def test_registry_get_latest_price_returns_none_when_no_provider():
    """With no provider for the asset class, ``get_latest_price`` yields ``None``.

    Covers ``registry.py`` ``except NoProviderAvailableError: return None``
    branch in :meth:`get_latest_price`. The traced variant raises, but the
    public convenience method must swallow the "nobody configured" case so a
    503 never escapes a simple price lookup (callers map ``None`` → 404).
    """
    registry = DataProviderRegistry()
    registry.register(ProviderRegistration(provider=_FakeProvider("c", {AssetClass.CRYPTO})))

    price = await registry.get_latest_price("AAPL", asset_class=AssetClass.EQUITY)
    assert price is None


@pytest.mark.asyncio
async def test_registry_returns_last_empty_result_when_all_candidates_empty():
    """When every candidate returns an empty frame, the empty result is returned.

    Covers ``registry.py`` ``if saw_empty: return last_empty, last_empty_name``
    branch — the soft-miss path. An empty DataFrame means "symbol unknown",
    not "providers broken", so the registry surfaces the empty frame (letting
    callers map to 404) rather than raising ``NoProviderAvailableError``.
    """
    registry = DataProviderRegistry()
    empty = pd.DataFrame(columns=list(OHLCV_COLUMNS))
    registry.register(
        ProviderRegistration(
            provider=_FakeProvider("a", {AssetClass.EQUITY}, ohlcv=empty), priority=1
        )
    )
    registry.register(
        ProviderRegistration(
            provider=_FakeProvider("b", {AssetClass.EQUITY}, ohlcv=empty), priority=2
        )
    )

    df, served_by = await registry.get_ohlcv_traced("DELISTED", asset_class=AssetClass.EQUITY)
    assert df.empty
    assert served_by == "b"  # last candidate tried
