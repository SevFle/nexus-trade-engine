"""Pure-mock unit tests for the pluggable data provider system."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pandas as pd
import pytest

from engine.data.providers import (
    AssetClass,
    DataProviderRegistry,
    HealthStatus,
    NoProviderAvailable,
    ProviderRegistration,
    parse_config,
    reset_registry_for_tests,
)
from engine.data.providers._cache import ProviderCache
from engine.data.providers._http import normalise_ohlcv
from engine.data.providers._resilience import TokenBucket, call_with_retry
from engine.data.providers.alpaca_data import AlpacaDataProvider
from engine.data.providers.base import (
    OHLCV_COLUMNS,
    DataProviderCapability,
    FatalProviderError,
    HealthCheckResult,
    IDataProvider,
    RateLimit,
    TransientProviderError,
)
from engine.data.providers.binance import BinanceDataProvider
from engine.data.providers.coingecko import CoinGeckoDataProvider
from engine.data.providers.config import (
    ProviderConfig,
    build_provider,
    configure_registry,
)
from engine.data.providers.oanda import OandaDataProvider
from engine.data.providers.polygon import PolygonDataProvider
from engine.data.providers.yahoo import YahooDataProvider

# ---------- helpers ----------


def _mock_transport(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(base_url="http://mock", transport=transport)


def _make_cache() -> ProviderCache:
    ProviderCache.reset_for_tests()
    return ProviderCache(url=None)


def _ohlcv_df(rows: int = 5) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(rows)],
            "high": [105.0 + i for i in range(rows)],
            "low": [95.0 + i for i in range(rows)],
            "close": [102.0 + i for i in range(rows)],
            "volume": [1_000 * (i + 1) for i in range(rows)],
        },
        index=idx,
    )


class _FakeProvider(IDataProvider):
    """Configurable fake provider used by registry tests."""

    def __init__(
        self,
        name: str,
        asset_classes: set[AssetClass],
        *,
        ohlcv: pd.DataFrame | None = None,
        price: float | None = None,
        raises: BaseException | None = None,
        health: HealthStatus = HealthStatus.UP,
    ) -> None:
        self.capability = DataProviderCapability(
            name=name,
            asset_classes=frozenset(asset_classes),
            supports_options_chain=AssetClass.OPTIONS in asset_classes,
            supports_orderbook=AssetClass.CRYPTO in asset_classes,
        )
        self._ohlcv = ohlcv if ohlcv is not None else _ohlcv_df()
        self._price = price
        self._raises = raises
        self._health = health
        self.calls: list[str] = []

    async def get_ohlcv(self, symbol, period="1y", interval="1d"):
        self.calls.append(f"ohlcv:{symbol}")
        if self._raises:
            raise self._raises
        return self._ohlcv

    async def get_latest_price(self, symbol):
        self.calls.append(f"price:{symbol}")
        if self._raises:
            raise self._raises
        return self._price

    async def get_multiple_prices(self, symbols):
        self.calls.append(f"prices:{','.join(symbols)}")
        if self._raises:
            raise self._raises
        return {s: float(self._price or 0.0) for s in symbols}

    async def get_options_chain(self, symbol, expiry=None):
        if self._raises:
            raise self._raises
        return pd.DataFrame([{"strike": 100.0, "expiry": "2026-12-19"}])

    async def get_orderbook(self, symbol, depth=20):
        if self._raises:
            raise self._raises
        return pd.DataFrame([{"price": 100.0, "size": 1.0, "side": "bid"}])

    def stream_prices(self, symbols):
        raise FatalProviderError("not implemented")

    async def health_check(self):
        return HealthCheckResult(name=self.capability.name, status=self._health)


# ---------- normalisation ----------


def test_normalise_ohlcv_drops_nan_and_sorts():
    raw = pd.DataFrame(
        {
            "Open": [1.0, 2.0, 3.0],
            "High": [1.1, 2.1, 3.1],
            "Low": [0.9, 1.9, 2.9],
            "Close": [None, 2.05, 3.05],
            "Volume": [100, 200, 300],
        },
        index=pd.to_datetime(
            ["2026-01-03", "2026-01-01", "2026-01-02"], utc=True
        ),
    )
    out = normalise_ohlcv(raw)
    assert list(out.columns) == list(OHLCV_COLUMNS)
    assert len(out) == 2
    assert out.index.is_monotonic_increasing
    assert str(out.index.tz) == "UTC"


def test_normalise_ohlcv_missing_columns_raises():
    bad = pd.DataFrame({"open": [1], "close": [1]})
    with pytest.raises(FatalProviderError):
        normalise_ohlcv(bad)


def test_normalise_ohlcv_empty_returns_empty():
    out = normalise_ohlcv(pd.DataFrame())
    assert out.empty
    assert list(out.columns) == list(OHLCV_COLUMNS)


# ---------- token bucket / retry ----------


@pytest.mark.asyncio
async def test_token_bucket_unlimited_passes_through():
    bucket = TokenBucket(RateLimit(requests_per_minute=0))
    for _ in range(20):
        await bucket.acquire()  # no waits


@pytest.mark.asyncio
async def test_token_bucket_throttles_after_burst():
    bucket = TokenBucket(RateLimit(requests_per_minute=600, burst=2))
    # Burst of 2 immediate, third must wait.
    await bucket.acquire()
    await bucket.acquire()
    waited = await asyncio.wait_for(bucket.acquire(), timeout=1.0)
    assert waited is None  # acquire() returns None on success


@pytest.mark.asyncio
async def test_call_with_retry_recovers_after_transient():
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientProviderError("boom")
        return "ok"

    result = await call_with_retry(
        flaky, provider="t", base_delay_s=0.0, max_delay_s=0.0
    )
    assert result == "ok"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_call_with_retry_does_not_retry_fatal():
    attempts = {"n": 0}

    async def fatal() -> str:
        attempts["n"] += 1
        raise FatalProviderError("nope")

    with pytest.raises(FatalProviderError):
        await call_with_retry(fatal, provider="t", base_delay_s=0.0)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_call_with_retry_gives_up_after_max_attempts():
    attempts = {"n": 0}

    async def always() -> str:
        attempts["n"] += 1
        raise TransientProviderError("nope")

    with pytest.raises(TransientProviderError):
        await call_with_retry(
            always, provider="t", max_attempts=3, base_delay_s=0.0
        )
    assert attempts["n"] == 3


# ---------- in-memory cache ----------


@pytest.mark.asyncio
async def test_provider_cache_dataframe_roundtrip():
    cache = _make_cache()
    df = _ohlcv_df()
    key = ProviderCache.make_key("yahoo", "ohlcv", symbol="AAPL")
    await cache.set_dataframe(key, df, ttl_seconds=10)
    out = await cache.get_dataframe(key)
    assert out is not None
    pd.testing.assert_frame_equal(out, df, check_freq=False, check_dtype=False)


@pytest.mark.asyncio
async def test_provider_cache_skips_empty_dataframe():
    cache = _make_cache()
    key = ProviderCache.make_key("yahoo", "ohlcv", symbol="X")
    await cache.set_dataframe(key, pd.DataFrame(), ttl_seconds=10)
    assert await cache.get_dataframe(key) is None


@pytest.mark.asyncio
async def test_provider_cache_keys_are_deterministic():
    a = ProviderCache.make_key("yahoo", "ohlcv", symbol="AAPL", period="1y")
    b = ProviderCache.make_key("yahoo", "ohlcv", period="1y", symbol="AAPL")
    assert a == b


# ---------- registry routing & failover ----------


@pytest.mark.asyncio
async def test_registry_routes_by_priority():
    registry = DataProviderRegistry()
    primary = _FakeProvider("primary", {AssetClass.EQUITY}, price=100.0)
    fallback = _FakeProvider("fallback", {AssetClass.EQUITY}, price=200.0)
    registry.register(ProviderRegistration(provider=primary, priority=1))
    registry.register(ProviderRegistration(provider=fallback, priority=10))

    price = await registry.get_latest_price("AAPL", AssetClass.EQUITY)
    assert price == 100.0
    assert primary.calls
    assert not fallback.calls


@pytest.mark.asyncio
async def test_registry_failover_on_transient_error():
    registry = DataProviderRegistry()
    primary = _FakeProvider(
        "primary",
        {AssetClass.EQUITY},
        raises=TransientProviderError("rate limited"),
    )
    fallback = _FakeProvider("fallback", {AssetClass.EQUITY}, price=99.0)
    registry.register(ProviderRegistration(provider=primary, priority=1))
    registry.register(ProviderRegistration(provider=fallback, priority=10))

    price = await registry.get_latest_price("AAPL", AssetClass.EQUITY)
    assert price == 99.0
    assert primary.calls
    assert fallback.calls


@pytest.mark.asyncio
async def test_registry_skips_provider_without_capability():
    registry = DataProviderRegistry()
    no_options = _FakeProvider("a", {AssetClass.EQUITY})
    options_able = _FakeProvider(
        "b",
        {AssetClass.OPTIONS},
    )
    registry.register(ProviderRegistration(provider=no_options, priority=1))
    registry.register(ProviderRegistration(provider=options_able, priority=2))

    chain = await registry.get_options_chain("AAPL", asset_class=AssetClass.OPTIONS)
    assert not chain.empty


@pytest.mark.asyncio
async def test_registry_no_provider_for_asset_class():
    registry = DataProviderRegistry()
    only_crypto = _FakeProvider("c", {AssetClass.CRYPTO})
    registry.register(ProviderRegistration(provider=only_crypto, priority=1))

    with pytest.raises(NoProviderAvailable):
        await registry.get_ohlcv("EURUSD", asset_class=AssetClass.FOREX)


@pytest.mark.asyncio
async def test_registry_health_returns_per_provider_status():
    registry = DataProviderRegistry()
    up = _FakeProvider("up", {AssetClass.EQUITY}, health=HealthStatus.UP)
    down = _FakeProvider("down", {AssetClass.EQUITY}, health=HealthStatus.DOWN)
    registry.register(ProviderRegistration(provider=up, priority=1))
    registry.register(ProviderRegistration(provider=down, priority=2))

    result = await registry.health()
    statuses = {r.name: r.status for r in result}
    assert statuses == {"up": HealthStatus.UP, "down": HealthStatus.DOWN}


@pytest.mark.asyncio
async def test_registry_get_multiple_prices_fallover_returns_dict():
    registry = DataProviderRegistry()
    only_failing = _FakeProvider(
        "x",
        {AssetClass.EQUITY},
        raises=TransientProviderError("down"),
    )
    registry.register(ProviderRegistration(provider=only_failing, priority=1))

    out = await registry.get_multiple_prices(["AAPL"], AssetClass.EQUITY)
    assert out == {}


@pytest.mark.asyncio
async def test_registry_rejects_duplicate_registration():
    registry = DataProviderRegistry()
    p = _FakeProvider("dup", {AssetClass.EQUITY})
    registry.register(ProviderRegistration(provider=p, priority=1))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(ProviderRegistration(provider=p, priority=2))


# ---------- adapter HTTP transport tests ----------


def _yahoo_payload() -> dict[str, Any]:
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": [1735689600, 1735776000],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0, 101.0],
                                "high": [101.0, 102.0],
                                "low": [99.0, 100.0],
                                "close": [100.5, 101.5],
                                "volume": [1000, 2000],
                            }
                        ]
                    },
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_yahoo_adapter_parses_chart():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "AAPL" in request.url.path
        return httpx.Response(200, json=_yahoo_payload())

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooDataProvider(client=client, cache=cache)
        df = await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
    assert list(df.columns) == list(OHLCV_COLUMNS)
    assert len(df) == 2


@pytest.mark.asyncio
async def test_yahoo_adapter_caches_response():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_yahoo_payload())

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooDataProvider(client=client, cache=cache)
        await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
        await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_yahoo_adapter_invalid_period_raises_fatal():
    cache = _make_cache()
    provider = YahooDataProvider(client=_mock_transport(lambda r: httpx.Response(200, json={})), cache=cache)
    with pytest.raises(FatalProviderError):
        await provider.get_ohlcv("AAPL", period="weird", interval="1d")
    await provider.aclose()


@pytest.mark.asyncio
async def test_yahoo_get_multiple_prices_parses_quote():
    payload = {
        "quoteResponse": {
            "result": [
                {"symbol": "AAPL", "regularMarketPrice": 150.0},
                {"symbol": "MSFT", "regularMarketPrice": 300.0},
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "symbols=AAPL" in str(request.url)
        return httpx.Response(200, json=payload)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = YahooDataProvider(client=client, cache=cache)
        out = await provider.get_multiple_prices(["AAPL", "MSFT"])
    assert out == {"AAPL": 150.0, "MSFT": 300.0}


@pytest.mark.asyncio
async def test_polygon_adapter_parses_aggs():
    payload = {
        "results": [
            {"t": 1735689600000, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000},
            {"t": 1735776000000, "o": 100.5, "h": 102.0, "l": 100.0, "c": 101.5, "v": 2000},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v2/aggs/ticker/AAPL/range/1/day/" in request.url.path
        assert request.headers.get("authorization") == "Bearer secret"
        return httpx.Response(200, json=payload)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = PolygonDataProvider(api_key="secret", client=client, cache=cache)
        df = await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
    assert len(df) == 2


@pytest.mark.asyncio
async def test_polygon_requires_key():
    with pytest.raises(FatalProviderError):
        PolygonDataProvider(api_key="")


@pytest.mark.asyncio
async def test_polygon_auth_error_is_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
        with pytest.raises(FatalProviderError):
            await provider.get_ohlcv("AAPL", period="1mo", interval="1d")


@pytest.mark.asyncio
async def test_polygon_5xx_retries_then_fails():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="upstream down")

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
        with pytest.raises(TransientProviderError):
            await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_alpaca_adapter_parses_bars():
    payload = {
        "bars": [
            {
                "t": "2026-01-01T00:00:00Z",
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.5,
                "v": 1000,
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("apca-api-key-id") == "k"
        return httpx.Response(200, json=payload)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = AlpacaDataProvider(api_key="k", api_secret="s", client=client, cache=cache)
        df = await provider.get_ohlcv("AAPL", period="1mo", interval="1d")
    assert len(df) == 1


@pytest.mark.asyncio
async def test_alpaca_requires_credentials():
    with pytest.raises(FatalProviderError):
        AlpacaDataProvider(api_key="", api_secret="")


@pytest.mark.asyncio
async def test_binance_adapter_parses_klines():
    payload = [
        [1735689600000, "10.0", "11.0", "9.0", "10.5", "1000", 1735776000000, "10500", 5, "500", "5250", "0"],
        [1735776000000, "10.5", "12.0", "10.0", "11.5", "2000", 1735862400000, "23000", 5, "500", "5250", "0"],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/v3/klines" in request.url.path
        return httpx.Response(200, json=payload)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = BinanceDataProvider(client=client, cache=cache)
        df = await provider.get_ohlcv("BTCUSDT", period="1mo", interval="1d")
    assert len(df) == 2
    assert df["close"].iloc[-1] == 11.5


@pytest.mark.asyncio
async def test_binance_orderbook_parses_levels():
    payload = {
        "bids": [["100.0", "1.0"], ["99.5", "2.0"]],
        "asks": [["100.5", "1.5"], ["101.0", "0.8"]],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = BinanceDataProvider(client=client, cache=cache)
        df = await provider.get_orderbook("BTCUSDT", depth=5)
    assert set(df["side"]) == {"bid", "ask"}
    assert len(df) == 4


@pytest.mark.asyncio
async def test_coingecko_adapter_combines_ohlc_and_volume():
    ohlc = [
        [1735689600000, 100.0, 101.0, 99.0, 100.5],
        [1735776000000, 100.5, 102.0, 100.0, 101.5],
    ]
    market = {
        "total_volumes": [
            [1735689600000, 1234.5],
            [1735776000000, 5678.9],
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ohlc"):
            return httpx.Response(200, json=ohlc)
        return httpx.Response(200, json=market)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = CoinGeckoDataProvider(client=client, cache=cache)
        df = await provider.get_ohlcv("BTC", period="1mo", interval="1d")
    assert len(df) == 2
    assert df["volume"].sum() > 0


@pytest.mark.asyncio
async def test_oanda_adapter_parses_candles():
    payload = {
        "candles": [
            {
                "time": "2026-01-01T00:00:00.000000000Z",
                "complete": True,
                "volume": 100,
                "mid": {"o": "1.10", "h": "1.11", "l": "1.09", "c": "1.105"},
            },
            {
                "time": "2026-01-02T00:00:00.000000000Z",
                "complete": True,
                "volume": 200,
                "mid": {"o": "1.105", "h": "1.12", "l": "1.10", "c": "1.115"},
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer t"
        return httpx.Response(200, json=payload)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = OandaDataProvider(api_key="t", client=client, cache=cache)
        df = await provider.get_ohlcv("EUR/USD", period="1mo", interval="1d")
    assert len(df) == 2


@pytest.mark.asyncio
async def test_oanda_requires_key():
    with pytest.raises(FatalProviderError):
        OandaDataProvider(api_key="")


# ---------- config / build_provider ----------


def test_parse_config_expands_env_vars(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret")
    payload = {
        "data_providers": {
            "polygon": {
                "priority": 1,
                "asset_classes": ["equity"],
                "api_key": "${MY_KEY}",
            }
        }
    }
    configs = parse_config(payload)
    assert configs[0].name == "polygon"
    assert configs[0].options["api_key"] == "secret"


def test_parse_config_unknown_root_rejected():
    with pytest.raises(ValueError):
        parse_config({"data_providers": "not-a-dict"})


def test_build_provider_returns_correct_classes(monkeypatch):
    monkeypatch.setenv("NEXUS_POLYGON_API_KEY", "from-env")
    cfg = ProviderConfig(name="polygon", asset_classes=["equity"])
    provider = build_provider(cfg)
    assert isinstance(provider, PolygonDataProvider)


def test_configure_registry_skips_disabled():
    reset_registry_for_tests()
    registry = DataProviderRegistry()
    configs = [
        ProviderConfig(name="yahoo", enabled=False, asset_classes=["equity"]),
        ProviderConfig(name="coingecko", asset_classes=["crypto"]),
    ]
    out = configure_registry(configs, registry)
    names = [r.name for r in out.list_providers()]
    assert "coingecko" in names
    assert "yahoo" not in names


def test_configure_registry_skips_invalid_provider(caplog):
    reset_registry_for_tests()
    registry = DataProviderRegistry()
    configs = [
        ProviderConfig(name="polygon", asset_classes=["equity"]),  # missing key
    ]
    out = configure_registry(configs, registry)
    assert out.list_providers() == []


# ---------- security regression ----------


def test_validate_symbol_rejects_url_like_strings():
    from engine.data.providers._http import validate_symbol

    with pytest.raises(FatalProviderError):
        validate_symbol("http://attacker.example/x")
    with pytest.raises(FatalProviderError):
        validate_symbol("../../etc/passwd")
    with pytest.raises(FatalProviderError):
        validate_symbol("AAPL\nattack")
    # legitimate symbols pass
    assert validate_symbol("AAPL") == "AAPL"
    assert validate_symbol("BRK.B") == "BRK.B"
    assert validate_symbol("EUR/USD") == "EUR/USD"


def test_redact_secrets_strips_obvious_credentials():
    from engine.data.providers._http import redact_secrets

    assert "<redacted>" in redact_secrets("apiKey=AKIAIOSFODNN7EXAMPLE")
    assert "<redacted>" in redact_secrets("Authorization: Bearer abcdef0123456789abcdef0123456789")
    assert "<redacted>" in redact_secrets("signature=deadbeefdeadbeefdeadbeefdeadbeef")


@pytest.mark.asyncio
async def test_request_refuses_locked_auth_header_override():
    cache = _make_cache()
    async with _mock_transport(
        lambda r: httpx.Response(200, json={"results": {"p": 1.0}})
    ) as client:
        provider = PolygonDataProvider(api_key="secret", client=client, cache=cache)
        with pytest.raises(FatalProviderError, match="locked header"):
            await provider._request_json("GET", "/x", headers={"Authorization": "Bearer evil"})


@pytest.mark.asyncio
async def test_request_refuses_cross_host_path():
    cache = _make_cache()
    provider = YahooDataProvider(
        client=_mock_transport(lambda r: httpx.Response(200, json={})),
        cache=cache,
    )
    with pytest.raises(FatalProviderError, match="cross-host"):
        await provider._request_json("GET", "https://attacker.example/exfil")
    await provider.aclose()


# ---------- registry capability prefilter + empty failover ----------


@pytest.mark.asyncio
async def test_registry_prefilters_by_capability():
    from engine.data.providers.base import CapabilityNotSupportedError

    registry = DataProviderRegistry()
    no_options = _FakeProvider("a", {AssetClass.OPTIONS})
    # Force capability flag false
    no_options.capability = type(no_options.capability)(  # type: ignore[call-arg]
        name=no_options.capability.name,
        asset_classes=no_options.capability.asset_classes,
        supports_options_chain=False,
    )
    registry.register(ProviderRegistration(provider=no_options, priority=1))
    with pytest.raises(CapabilityNotSupportedError):
        await registry.get_options_chain("AAPL", asset_class=AssetClass.OPTIONS)


@pytest.mark.asyncio
async def test_registry_falls_over_when_first_returns_empty():
    """Empty DataFrame from primary should let secondary try."""
    registry = DataProviderRegistry()
    primary = _FakeProvider(
        "primary",
        {AssetClass.EQUITY},
        ohlcv=pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
    )
    fallback = _FakeProvider("fallback", {AssetClass.EQUITY})
    registry.register(ProviderRegistration(provider=primary, priority=1))
    registry.register(ProviderRegistration(provider=fallback, priority=10))
    df = await registry.get_ohlcv("AAPL", asset_class=AssetClass.EQUITY)
    assert not df.empty


# ---------- config env expansion ----------


def test_parse_config_raises_on_unset_env_var(monkeypatch):
    monkeypatch.delenv("THIS_DEFINITELY_DOES_NOT_EXIST", raising=False)
    payload = {
        "data_providers": {
            "polygon": {
                "asset_classes": ["equity"],
                "api_key": "${THIS_DEFINITELY_DOES_NOT_EXIST}",
            }
        }
    }
    with pytest.raises(ValueError, match="unset env var"):
        parse_config(payload)


def test_parse_config_blocks_env_in_non_secret_field():
    payload = {
        "data_providers": {
            "polygon": {
                "asset_classes": ["equity"],
                "rogue_field": "${EVIL}",
            }
        }
    }
    with pytest.raises(ValueError, match="env-expandable allowlist"):
        parse_config(payload)


# ---------- cache key + tz roundtrip ----------


def test_cache_key_rejects_non_primitive():
    from datetime import datetime as _dt

    with pytest.raises(TypeError):
        ProviderCache.make_key("p", "m", when=_dt.now())  # noqa: DTZ005


@pytest.mark.asyncio
async def test_cache_dataframe_preserves_utc_tz():
    cache = _make_cache()
    df = _ohlcv_df()
    key = ProviderCache.make_key("yahoo", "ohlcv", symbol="AAPL")
    await cache.set_dataframe(key, df, ttl_seconds=10)
    out = await cache.get_dataframe(key)
    assert out is not None
    assert isinstance(out.index, pd.DatetimeIndex)
    assert out.index.tz is not None
    assert str(out.index.tz) == "UTC"


# ---------- adapter-specific data correctness ----------


@pytest.mark.asyncio
async def test_binance_rejects_period_exceeding_limit():
    cache = _make_cache()
    provider = BinanceDataProvider(
        client=_mock_transport(lambda r: httpx.Response(200, json=[])),
        cache=cache,
    )
    with pytest.raises(FatalProviderError, match="exceeds single-call limit"):
        await provider.get_ohlcv("BTCUSDT", period="5y", interval="1d")
    await provider.aclose()


@pytest.mark.asyncio
async def test_polygon_excludes_today_from_path():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"results": []})

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
        await provider.get_ohlcv("AAPL", period="1mo", interval="1d")

    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date().isoformat()
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    assert today not in captured["path"]
    assert yesterday in captured["path"]


@pytest.mark.asyncio
async def test_oanda_normalises_symbol_in_cache_key():
    """Cache key should match for ``EUR/USD`` and ``EUR_USD`` forms."""
    payload = {
        "candles": [
            {
                "time": "2026-01-01T00:00:00.000000000Z",
                "complete": True,
                "volume": 100,
                "mid": {"o": "1.10", "h": "1.11", "l": "1.09", "c": "1.105"},
            }
        ]
    }
    requests = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests["n"] += 1
        return httpx.Response(200, json=payload)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = OandaDataProvider(api_key="t", client=client, cache=cache)
        await provider.get_ohlcv("EUR/USD", period="1mo", interval="1d")
        await provider.get_ohlcv("EUR_USD", period="1mo", interval="1d")
    assert requests["n"] == 1


@pytest.mark.asyncio
async def test_coingecko_logs_unknown_symbol(caplog):
    payload = {"bitcoin": {"usd": 50_000.0}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    cache = _make_cache()
    async with _mock_transport(handler) as client:
        provider = CoinGeckoDataProvider(client=client, cache=cache)
        out = await provider.get_multiple_prices(["BTC", "BOGUS"])
    assert "BTC" in out and "BOGUS" not in out


# ---------- adapter unsupported-feature stubs ----------


@pytest.mark.asyncio
async def test_yahoo_unsupported_features_raise():
    cache = _make_cache()
    provider = YahooDataProvider(client=_mock_transport(lambda r: httpx.Response(200, json={})), cache=cache)
    with pytest.raises(FatalProviderError):
        await provider.get_orderbook("AAPL")
    with pytest.raises(FatalProviderError):
        provider.stream_prices(["AAPL"])
    await provider.aclose()


@pytest.mark.asyncio
async def test_alpaca_unsupported_features_raise():
    cache = _make_cache()
    provider = AlpacaDataProvider(
        api_key="k",
        api_secret="s",
        client=_mock_transport(lambda r: httpx.Response(200, json={})),
        cache=cache,
    )
    with pytest.raises(FatalProviderError):
        await provider.get_options_chain("AAPL")
    with pytest.raises(FatalProviderError):
        await provider.get_orderbook("AAPL")
    with pytest.raises(FatalProviderError):
        provider.stream_prices(["AAPL"])
    await provider.aclose()


@pytest.mark.asyncio
async def test_polygon_unsupported_features_raise():
    cache = _make_cache()
    provider = PolygonDataProvider(
        api_key="x",
        client=_mock_transport(lambda r: httpx.Response(200, json={})),
        cache=cache,
    )
    with pytest.raises(FatalProviderError):
        await provider.get_orderbook("AAPL")
    with pytest.raises(FatalProviderError):
        provider.stream_prices(["AAPL"])
    await provider.aclose()


@pytest.mark.asyncio
async def test_binance_unsupported_features_raise():
    cache = _make_cache()
    provider = BinanceDataProvider(
        client=_mock_transport(lambda r: httpx.Response(200, json={})),
        cache=cache,
    )
    with pytest.raises(FatalProviderError):
        await provider.get_options_chain("BTCUSDT")
    with pytest.raises(FatalProviderError):
        provider.stream_prices(["BTCUSDT"])
    await provider.aclose()


@pytest.mark.asyncio
async def test_coingecko_unsupported_features_raise():
    cache = _make_cache()
    provider = CoinGeckoDataProvider(
        client=_mock_transport(lambda r: httpx.Response(200, json={})),
        cache=cache,
    )
    with pytest.raises(FatalProviderError):
        await provider.get_options_chain("BTC")
    with pytest.raises(FatalProviderError):
        await provider.get_orderbook("BTC")
    with pytest.raises(FatalProviderError):
        provider.stream_prices(["BTC"])
    await provider.aclose()


@pytest.mark.asyncio
async def test_oanda_unsupported_features_raise():
    cache = _make_cache()
    provider = OandaDataProvider(
        api_key="t",
        client=_mock_transport(lambda r: httpx.Response(200, json={})),
        cache=cache,
    )
    with pytest.raises(FatalProviderError):
        await provider.get_options_chain("EUR_USD")
    with pytest.raises(FatalProviderError):
        provider.stream_prices(["EUR_USD"])
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_latest_price_via_adapters_with_mocks():
    """Smoke test the latest-price path on every adapter."""
    cache = _make_cache()

    async with _mock_transport(
        lambda r: httpx.Response(200, json=_yahoo_payload())
    ) as client:
        yahoo = YahooDataProvider(client=client, cache=cache)
        price = await yahoo.get_latest_price("AAPL")
        assert price is not None

    async with _mock_transport(
        lambda r: httpx.Response(200, json={"results": {"p": 42.0}})
    ) as client:
        polygon = PolygonDataProvider(api_key="x", client=client, cache=cache)
        assert await polygon.get_latest_price("AAPL") == 42.0

    async with _mock_transport(
        lambda r: httpx.Response(200, json={"trade": {"p": 42.5}})
    ) as client:
        alpaca = AlpacaDataProvider(api_key="k", api_secret="s", client=client, cache=cache)
        assert await alpaca.get_latest_price("AAPL") == 42.5

    async with _mock_transport(
        lambda r: httpx.Response(200, json={"price": "100.0"})
    ) as client:
        binance = BinanceDataProvider(client=client, cache=cache)
        assert await binance.get_latest_price("BTCUSDT") == 100.0

    async with _mock_transport(
        lambda r: httpx.Response(200, json={"bitcoin": {"usd": 50_000.0}})
    ) as client:
        gecko = CoinGeckoDataProvider(client=client, cache=cache)
        assert await gecko.get_latest_price("BTC") == 50_000.0


@pytest.mark.asyncio
async def test_provider_health_check_path_succeeds():
    """`HTTPProviderBase.health_check` reports UP on success."""
    cache = _make_cache()

    async with _mock_transport(
        lambda r: httpx.Response(200, json={"ok": True})
    ) as client:
        provider = BinanceDataProvider(client=client, cache=cache)
        result = await provider.health_check()
    assert result.status == HealthStatus.UP


@pytest.mark.asyncio
async def test_provider_health_check_reports_down_on_fatal():
    cache = _make_cache()

    async with _mock_transport(
        lambda r: httpx.Response(401, text="bad")
    ) as client:
        provider = PolygonDataProvider(api_key="x", client=client, cache=cache)
        result = await provider.health_check()
    assert result.status == HealthStatus.DOWN


# ---------- legacy facade ----------


@pytest.mark.asyncio
async def test_legacy_get_data_provider_returns_yahoo():
    from engine.data.feeds import MarketDataProvider, get_data_provider

    provider = get_data_provider("yahoo")
    assert isinstance(provider, MarketDataProvider)
    assert hasattr(provider, "get_ohlcv")


@pytest.mark.asyncio
async def test_legacy_get_data_provider_unknown_raises():
    from engine.data.feeds import get_data_provider

    reset_registry_for_tests()
    with pytest.raises(ValueError, match="Unknown data provider"):
        get_data_provider("does-not-exist")


# ---------- health endpoint ----------


@pytest.mark.asyncio
async def test_health_provider_endpoint(monkeypatch):
    from engine.api.routes import health as health_module

    fake_registry = DataProviderRegistry()
    fake_registry.register(
        ProviderRegistration(
            provider=_FakeProvider("foo", {AssetClass.EQUITY}, health=HealthStatus.UP),
            priority=1,
        )
    )
    fake_registry.register(
        ProviderRegistration(
            provider=_FakeProvider("bar", {AssetClass.CRYPTO}, health=HealthStatus.DEGRADED),
            priority=1,
        )
    )
    monkeypatch.setattr(health_module, "get_registry", lambda: fake_registry)
    payload = await health_module.provider_health()
    assert payload["status"] == "degraded"
    assert set(payload["providers"]) == {"foo", "bar"}
