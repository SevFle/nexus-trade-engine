"""Tests for /api/v1/market-data routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from engine.api.routes.market_data import detect_asset_class
from engine.data.providers import (
    AssetClass,
    DataProviderCapability,
    HealthCheckResult,
    HealthStatus,
    IDataProvider,
    NoProviderAvailableError,
    ProviderRegistration,
    get_registry,
    reset_registry_for_tests,
)


def _make_df() -> pd.DataFrame:
    """Two-bar OHLCV frame with a tz-aware UTC index."""
    idx = pd.to_datetime(
        ["2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"], utc=True
    )
    return pd.DataFrame(
        {
            "open": [100.0, 102.0],
            "high": [105.0, 106.0],
            "low": [99.0, 101.0],
            "close": [104.0, 105.5],
            "volume": [1_000_000.0, 1_200_000.0],
        },
        index=idx,
    )


class _FakeProvider(IDataProvider):
    """Minimal in-memory provider used to exercise the route end-to-end.

    Has knobs to drive each branch — empty result, raise on price, raise an
    error class — without touching the network.
    """

    def __init__(
        self,
        name: str,
        asset_classes: frozenset[AssetClass],
        *,
        df: pd.DataFrame | None = None,
        latest_price: float | None = 105.5,
    ) -> None:
        self.capability = DataProviderCapability(
            name=name, asset_classes=asset_classes
        )
        self._df = df if df is not None else _make_df()
        self._latest = latest_price
        self.calls: list[tuple[str, str, str]] = []

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        self.calls.append((symbol, period, interval))
        return self._df

    async def get_latest_price(self, symbol: str) -> float | None:
        return self._latest

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: self._latest for s in symbols if self._latest is not None}

    async def get_options_chain(
        self, symbol: str, expiry: str | None = None
    ) -> pd.DataFrame:
        return pd.DataFrame()

    async def get_orderbook(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        return pd.DataFrame()

    def stream_prices(self, symbols: list[str]) -> AsyncIterator[dict[str, float]]:
        raise NotImplementedError

    async def health_check(self) -> HealthCheckResult:
        return HealthCheckResult(name=self.capability.name, status=HealthStatus.UP)


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Each test gets a fresh process-wide registry singleton."""
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def _register(provider: _FakeProvider, *, priority: int = 1) -> None:
    get_registry().register(
        ProviderRegistration(
            provider=provider,
            priority=priority,
            asset_classes=provider.capability.asset_classes,
        )
    )


# ---------- detect_asset_class ----------


class TestDetectAssetClass:
    def test_equity_default(self):
        assert detect_asset_class("AAPL") == AssetClass.EQUITY

    def test_equity_with_dot(self):
        assert detect_asset_class("BRK.B") == AssetClass.EQUITY

    def test_equity_dash_non_crypto_quote(self):
        # Dash with non-crypto suffix stays equity (e.g. Berkshire Hathaway B class)
        assert detect_asset_class("BRK-B") == AssetClass.EQUITY

    def test_crypto_dash_pair(self):
        assert detect_asset_class("BTC-USD") == AssetClass.CRYPTO

    def test_crypto_slash_pair(self):
        assert detect_asset_class("ETH/USDT") == AssetClass.CRYPTO

    def test_forex_yahoo_suffix(self):
        assert detect_asset_class("EURUSD=X") == AssetClass.FOREX

    def test_forex_slash_pair(self):
        assert detect_asset_class("EUR/USD") == AssetClass.FOREX


# ---------- /bars endpoint ----------


class TestBarsEndpoint:
    @pytest.mark.asyncio
    async def test_happy_path_equity(self, client):
        provider = _FakeProvider(
            "fake-equity", frozenset({AssetClass.EQUITY})
        )
        _register(provider)

        res = await client.get("/api/v1/market-data/AAPL/bars")
        assert res.status_code == 200, res.text

        body = res.json()
        assert body["symbol"] == "AAPL"
        assert body["asset_class"] == "equity"
        assert body["provider"] == "fake-equity"
        assert body["interval"] == "1d"
        assert body["period"] == "1y"
        assert len(body["bars"]) == 2
        first = body["bars"][0]
        assert first["open"] == 100.0
        assert first["high"] == 105.0
        assert first["close"] == 104.0
        assert first["timestamp"].startswith("2026-01-02")

    @pytest.mark.asyncio
    async def test_passes_period_and_interval_to_provider(self, client):
        provider = _FakeProvider(
            "fake-equity", frozenset({AssetClass.EQUITY})
        )
        _register(provider)

        res = await client.get(
            "/api/v1/market-data/AAPL/bars",
            params={"period": "5y", "interval": "1wk"},
        )
        assert res.status_code == 200
        assert provider.calls == [("AAPL", "5y", "1wk")]

    @pytest.mark.asyncio
    async def test_routes_crypto_pair_by_asset_class(self, client):
        equity = _FakeProvider("fake-equity", frozenset({AssetClass.EQUITY}))
        crypto = _FakeProvider("fake-crypto", frozenset({AssetClass.CRYPTO}))
        _register(equity)
        _register(crypto)

        res = await client.get("/api/v1/market-data/BTC-USD/bars")
        assert res.status_code == 200
        body = res.json()
        assert body["asset_class"] == "crypto"
        assert body["provider"] == "fake-crypto"
        # Equity provider must not have been called.
        assert equity.calls == []
        assert crypto.calls and crypto.calls[0][0] == "BTC-USD"

    @pytest.mark.asyncio
    async def test_provider_override_pins_adapter(self, client):
        primary = _FakeProvider(
            "primary", frozenset({AssetClass.EQUITY})
        )
        secondary = _FakeProvider(
            "secondary", frozenset({AssetClass.EQUITY})
        )
        _register(primary, priority=1)
        _register(secondary, priority=2)

        res = await client.get(
            "/api/v1/market-data/AAPL/bars", params={"provider": "secondary"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["provider"] == "secondary"
        assert primary.calls == []
        assert secondary.calls and secondary.calls[0][0] == "AAPL"

    @pytest.mark.asyncio
    async def test_provider_override_unknown_returns_404(self, client):
        _register(
            _FakeProvider("primary", frozenset({AssetClass.EQUITY}))
        )
        res = await client.get(
            "/api/v1/market-data/AAPL/bars", params={"provider": "nope"}
        )
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_no_provider_for_asset_class_returns_503(self, client):
        # Only a forex provider registered; equity lookup must 503.
        _register(_FakeProvider("fx", frozenset({AssetClass.FOREX})))
        res = await client.get("/api/v1/market-data/AAPL/bars")
        assert res.status_code == 503

    @pytest.mark.asyncio
    async def test_invalid_symbol_returns_400(self, client):
        _register(_FakeProvider("fx", frozenset({AssetClass.EQUITY})))
        # Space is outside the SYMBOL_PATTERN allowlist.
        res = await client.get("/api/v1/market-data/AA%20PL/bars")
        assert res.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_asset_class_param_returns_400(self, client):
        _register(_FakeProvider("eq", frozenset({AssetClass.EQUITY})))
        res = await client.get(
            "/api/v1/market-data/AAPL/bars",
            params={"asset_class": "totally-fake"},
        )
        assert res.status_code == 400

    @pytest.mark.asyncio
    async def test_explicit_asset_class_overrides_detection(self, client):
        # BTC-USD would auto-detect as crypto, but caller forces forex.
        _register(_FakeProvider("fx", frozenset({AssetClass.FOREX})))
        res = await client.get(
            "/api/v1/market-data/BTC-USD/bars",
            params={"asset_class": "forex"},
        )
        assert res.status_code == 200
        assert res.json()["asset_class"] == "forex"

    @pytest.mark.asyncio
    async def test_empty_dataframe_returns_empty_list(self, client):
        provider = _FakeProvider(
            "fake-equity",
            frozenset({AssetClass.EQUITY}),
            df=pd.DataFrame(),
        )
        _register(provider)
        res = await client.get("/api/v1/market-data/ZZZZ/bars")
        # Registry returns empty df from last candidate; route returns 200 + [].
        assert res.status_code == 200
        assert res.json()["bars"] == []


# ---------- /quote endpoint ----------


class TestQuoteEndpoint:
    @pytest.mark.asyncio
    async def test_happy_path(self, client):
        _register(
            _FakeProvider(
                "fake-equity",
                frozenset({AssetClass.EQUITY}),
                latest_price=187.42,
            )
        )
        res = await client.get("/api/v1/market-data/AAPL/quote")
        assert res.status_code == 200
        body = res.json()
        assert body == {
            "symbol": "AAPL",
            "asset_class": "equity",
            "provider": "fake-equity",
            "price": 187.42,
        }

    @pytest.mark.asyncio
    async def test_no_price_returns_404(self, client):
        _register(
            _FakeProvider(
                "fake-equity",
                frozenset({AssetClass.EQUITY}),
                latest_price=None,
            )
        )
        res = await client.get("/api/v1/market-data/UNLISTED/quote")
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_provider_override(self, client):
        _register(
            _FakeProvider("primary", frozenset({AssetClass.EQUITY}), latest_price=1.0),
            priority=1,
        )
        _register(
            _FakeProvider(
                "secondary", frozenset({AssetClass.EQUITY}), latest_price=2.0
            ),
            priority=2,
        )
        res = await client.get(
            "/api/v1/market-data/AAPL/quote", params={"provider": "secondary"}
        )
        assert res.status_code == 200
        assert res.json()["provider"] == "secondary"
        assert res.json()["price"] == 2.0

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_404(self, client):
        _register(_FakeProvider("primary", frozenset({AssetClass.EQUITY})))
        res = await client.get(
            "/api/v1/market-data/AAPL/quote", params={"provider": "ghost"}
        )
        assert res.status_code == 404


class TestRegistryNotConfigured:
    @pytest.mark.asyncio
    async def test_bars_503_when_no_providers(self, client):
        # No providers registered for any asset class.
        with pytest.raises(NoProviderAvailableError):
            # sanity: registry truly is empty
            await get_registry().get_ohlcv("AAPL")
        res = await client.get("/api/v1/market-data/AAPL/bars")
        assert res.status_code == 503
