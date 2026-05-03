"""Comprehensive tests for recently changed code, the PyJWT migration,
resilience retry fix (3.11 compat), and coverage deepening.

Targets:
  - engine/api/auth/jwt.py — PyJWT replacement (CVE fix)
  - engine/data/providers/_resilience.py — call_with_retry (3.12 syntax fix)
  - engine/core/rolling_benchmark.py — uncovered _beta_window / _stdev paths
  - engine/core/backtest_runner.py — sell PnL, warmup skip, zero capital
  - engine/observability/metrics.py — thread safety, timer, singleton
  - engine/reference/model.py — uncovered ValueError path (lines 146-147)
  - engine/api/routes/reference.py — Yahoo fallback with asset_class filter
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from engine.api.auth.jwt import (
    ALGORITHM,
    create_access_token,
    decode_token,
    generate_refresh_token,
    get_refresh_token_expiry,
    hash_token,
)
from engine.api.routes.reference import (
    _serialize_yahoo,
    get_search_index,
    router as reference_router,
)
from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.core.rolling_benchmark import (
    _beta_window,
    _stdev,
    rolling_alpha,
    rolling_beta,
    rolling_information_ratio,
    rolling_tracking_error,
)
from engine.data.feeds import MarketDataProvider
from engine.data.providers._resilience import call_with_retry
from engine.data.providers.base import (
    FatalProviderError,
    RateLimit,
    TransientProviderError,
)
from engine.observability.metrics import (
    NullBackend,
    RecordingBackend,
    get_metrics,
    set_metrics,
)
from engine.reference.model import RefInstrument
from engine.reference.search import SearchIndex
from engine.reference.seed import seed_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SyntheticProvider(MarketDataProvider):
    def __init__(self, df: pd.DataFrame):
        self._df = df

    async def get_latest_price(self, symbol: str) -> float | None:
        if self._df.empty:
            return None
        return float(self._df["close"].iloc[-1])

    async def get_ohlcv(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        return self._df

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        if self._df.empty:
            return {}
        return {symbols[0]: float(self._df["close"].iloc[-1])}


def _make_df(
    n_days: int = 60, base_price: float = 100.0, seed: int = 42
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = datetime(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    returns = rng.normal(0.001, 0.02, n_days)
    closes = base_price * np.cumprod(1 + returns)
    closes[0] = base_price
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000},
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


class _HoldStrategy:
    name = "hold"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return []


class _BuySellStrategy:
    name = "buysell"
    version = "1.0.0"

    def __init__(self):
        self._bought = False
        self._sold = False

    def on_bar(self, state, portfolio):
        from engine.core.signal import Signal

        if not self._bought and portfolio.cash > 50000:
            self._bought = True
            return [Signal.buy(symbol="AAPL", strategy_id=self.name, quantity=10)]
        if self._bought and not self._sold and len(portfolio.positions) > 0:
            self._sold = True
            return [Signal.sell(symbol="AAPL", strategy_id=self.name, quantity=10)]
        return []


# ---------------------------------------------------------------------------
# PyJWT migration tests
# ---------------------------------------------------------------------------


class TestPyJWTMigration:
    def test_create_and_decode_roundtrip(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key-pyjwt")
        from engine.config import Settings

        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(
            sub="user-123", email="a@b.com", role="admin", provider="local"
        )
        assert isinstance(token, str)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "user-123"
        assert payload["email"] == "a@b.com"
        assert payload["role"] == "admin"
        assert payload["provider"] == "local"
        assert payload["type"] == "access"
        assert "iat" in payload
        assert "exp" in payload

    def test_decode_expired_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key-pyjwt")
        from engine.config import Settings

        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(
            sub="user-123",
            email="a@b.com",
            role="admin",
            expires_delta=timedelta(seconds=-1),
        )
        assert decode_token(token) is None

    def test_decode_tampered_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key-pyjwt")
        from engine.config import Settings

        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(sub="user-123", email="a@b.com", role="admin")
        tampered = token + "X"
        assert decode_token(tampered) is None

    def test_decode_wrong_secret_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "secret-A")
        from engine.config import Settings

        settings_a = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings_a)
        token = create_access_token(sub="user-123", email="a@b.com", role="admin")

        monkeypatch.setenv("NEXUS_SECRET_KEY", "secret-B")
        settings_b = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings_b)
        assert decode_token(token) is None

    def test_decode_with_previous_key_rotation(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "old-secret")
        from engine.config import Settings

        old_settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", old_settings)
        token = create_access_token(sub="user-123", email="a@b.com", role="admin")

        monkeypatch.setenv("NEXUS_SECRET_KEY", "new-secret")
        monkeypatch.setenv("NEXUS_SECRET_KEY_PREVIOUS", "old-secret")
        new_settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", new_settings)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "user-123"

    def test_token_wrong_type_returns_none(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key-pyjwt")
        from engine.config import Settings

        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        payload = {
            "sub": "user-123",
            "email": "a@b.com",
            "role": "admin",
            "type": "refresh",
            "iat": datetime.now(tz=UTC),
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
        assert decode_token(token) is None

    def test_custom_expiry_delta(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key-pyjwt")
        from engine.config import Settings

        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(
            sub="user-123",
            email="a@b.com",
            role="user",
            expires_delta=timedelta(hours=12),
        )
        payload = decode_token(token)
        assert payload is not None
        expected_exp = datetime.now(tz=UTC) + timedelta(hours=12)
        actual_exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
        assert abs((actual_exp - expected_exp).total_seconds()) < 5

    def test_generate_refresh_token_length(self):
        token = generate_refresh_token()
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_generate_refresh_token_unique(self):
        assert generate_refresh_token() != generate_refresh_token()

    def test_hash_token_deterministic(self):
        t = "test-token"
        assert hash_token(t) == hash_token(t)

    def test_hash_token_different_inputs(self):
        assert hash_token("a") != hash_token("b")

    def test_hash_token_sha256_format(self):
        h = hash_token("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_get_refresh_token_expiry_future(self, monkeypatch):
        monkeypatch.setenv("NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS", "30")
        from engine.config import Settings

        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        expiry = get_refresh_token_expiry()
        expected = datetime.now(tz=UTC) + timedelta(days=30)
        assert abs((expiry - expected).total_seconds()) < 5

    def test_algorithm_is_hs256(self):
        assert ALGORITHM == "HS256"

    def test_custom_provider_in_token(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key-pyjwt")
        from engine.config import Settings

        settings = Settings()
        monkeypatch.setattr("engine.api.auth.jwt.settings", settings)

        token = create_access_token(
            sub="u1", email="a@b.com", role="user", provider="google"
        )
        payload = decode_token(token)
        assert payload is not None
        assert payload["provider"] == "google"


# ---------------------------------------------------------------------------
# call_with_retry (resilience fix)
# ---------------------------------------------------------------------------


class TestCallWithRetry:
    async def test_success_first_attempt(self):
        result = await call_with_retry(
            lambda: asyncio.sleep(0, result="ok"),
            provider="test",
            max_attempts=3,
        )
        assert result == "ok"

    async def test_transient_then_success(self):
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TransientProviderError("temp")
            return "recovered"

        result = await call_with_retry(flaky, provider="test", max_attempts=3, base_delay_s=0.001)
        assert result == "recovered"
        assert call_count == 3

    async def test_timeout_then_success(self):
        call_count = 0

        async def timeout_then_ok():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise TimeoutError("timed out")
            return "ok"

        result = await call_with_retry(
            timeout_then_ok, provider="test", max_attempts=3, base_delay_s=0.001
        )
        assert result == "ok"

    async def test_fatal_propagates_immediately(self):
        async def fatal():
            raise FatalProviderError("fatal")

        with pytest.raises(FatalProviderError, match="fatal"):
            await call_with_retry(fatal, provider="test", max_attempts=3)

    async def test_exhausted_attempts_raises_last(self):
        async def always_transient():
            raise TransientProviderError("always fails")

        with pytest.raises(TransientProviderError, match="always fails"):
            await call_with_retry(
                always_transient, provider="test", max_attempts=2, base_delay_s=0.001
            )

    async def test_custom_delays(self):
        times = []

        async def fail_twice():
            times.append(time.monotonic())
            if len(times) <= 2:
                raise TransientProviderError("retry")
            return "done"

        await call_with_retry(
            fail_twice,
            provider="test",
            max_attempts=4,
            base_delay_s=0.05,
            max_delay_s=1.0,
        )
        assert len(times) == 3


# ---------------------------------------------------------------------------
# Rolling benchmark — uncovered internal paths
# ---------------------------------------------------------------------------


class TestBetaWindowUncovered:
    def test_single_point_returns_zero(self):
        assert _beta_window([0.05], [0.05]) == 0.0

    def test_empty_returns_zero(self):
        assert _beta_window([], []) == 0.0

    def test_zero_variance_returns_zero(self):
        assert _beta_window([0.05, 0.05], [0.01, 0.01]) == 0.0

    def test_normal_case(self):
        port = [0.01, -0.02, 0.03]
        bench = [0.01, -0.02, 0.03]
        assert _beta_window(port, bench) == pytest.approx(1.0)


class TestStdevUncovered:
    def test_empty_returns_zero(self):
        assert _stdev([]) == 0.0

    def test_single_element_returns_zero(self):
        assert _stdev([1.0]) == 0.0

    def test_two_elements(self):
        result = _stdev([1.0, 3.0])
        assert result == pytest.approx(1.41421356, rel=1e-5)

    def test_zero_ddof(self):
        result = _stdev([1.0, 2.0, 3.0], ddof=0)
        assert result > 0


# ---------------------------------------------------------------------------
# BacktestRunner — sell PnL, warmup skip, zero capital
# ---------------------------------------------------------------------------


class TestBacktestRunnerSellPnL:
    async def test_sell_trade_has_realized_pnl(self):
        df = _make_df(n_days=60, base_price=100.0)
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="buysell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            random_seed=42,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider
        )
        result = await runner.run()
        sell_trades = [t for t in result.trades if t.get("side") == "sell"]
        assert len(sell_trades) >= 1
        for t in sell_trades:
            assert "realized_pnl" in t

    async def test_buy_trade_realized_pnl_zero(self):
        df = _make_df(n_days=60, base_price=100.0)
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="buysell",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            random_seed=42,
        )
        runner = BacktestRunner(
            config=config, strategy=_BuySellStrategy(), provider=provider
        )
        result = await runner.run()
        buy_trades = [t for t in result.trades if t.get("side") == "buy"]
        assert len(buy_trades) >= 1
        for t in buy_trades:
            assert t.get("realized_pnl") == 0.0


class TestBacktestRunnerZeroCapital:
    async def test_zero_initial_capital_no_division_error(self):
        df = _make_df(n_days=10, base_price=100.0)
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=0.0,
            random_seed=42,
        )
        runner = BacktestRunner(
            config=config, strategy=_HoldStrategy(), provider=provider
        )
        result = await runner.run()
        assert result.total_return_pct == 0.0


class TestBacktestRunnerWarmup:
    async def test_min_bars_above_data_length_graceful(self):
        df = _make_df(n_days=5, base_price=100.0)
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            min_bars=200,
            random_seed=42,
        )
        runner = BacktestRunner(
            config=config, strategy=_HoldStrategy(), provider=provider
        )
        result = await runner.run()
        assert result.equity_curve == []
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)


# ---------------------------------------------------------------------------
# Observability metrics — thread safety, timer, singleton
# ---------------------------------------------------------------------------


class TestMetricsBackendThreadSafety:
    def test_concurrent_counter_increments(self):
        backend = RecordingBackend()
        n = 1000

        def increment():
            for _ in range(n):
                backend.counter("test.metric")

        threads = [threading.Thread(target=increment) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        key = ("test.metric", ())
        assert backend.counters[key] == pytest.approx(n * 4)

    def test_concurrent_gauge_last_write_wins(self):
        backend = RecordingBackend()

        def write_gauge(val):
            backend.gauge("test.gauge", val)

        threads = [threading.Thread(target=write_gauge, args=(float(i),)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        key = ("test.gauge", ())
        assert backend.gauges[key] in {float(i) for i in range(100)}

    def test_concurrent_histogram_appends(self):
        backend = RecordingBackend()
        n = 500

        def observe():
            for _ in range(n):
                backend.histogram("test.latency", 1.0)

        threads = [threading.Thread(target=observe) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        key = ("test.latency", ())
        assert len(backend.histograms[key]) == n * 4

    def test_timer_records_elapsed(self):
        backend = RecordingBackend()
        with backend.timer("test.timer"):
            time.sleep(0.05)
        key = ("test.timer", ())
        assert len(backend.histograms[key]) == 1
        assert backend.histograms[key][0] >= 40.0

    def test_null_backend_no_error(self):
        backend = NullBackend()
        backend.counter("x")
        backend.gauge("y", 1.0)
        backend.histogram("z", 0.5)

    def test_null_backend_timer(self):
        backend = NullBackend()
        with backend.timer("x"):
            time.sleep(0.01)

    def test_null_backend_empty_name_raises(self):
        backend = NullBackend()
        with pytest.raises(ValueError, match="non-empty"):
            backend.counter("")

    def test_recording_backend_empty_name_raises(self):
        backend = RecordingBackend()
        with pytest.raises(ValueError, match="non-empty"):
            backend.gauge("", 1.0)

    def test_recording_backend_whitespace_name_raises(self):
        backend = RecordingBackend()
        with pytest.raises(ValueError, match="non-empty"):
            backend.histogram("   ", 1.0)

    def test_set_and_get_metrics(self):
        backend = RecordingBackend()
        set_metrics(backend)
        assert get_metrics() is backend
        set_metrics(NullBackend())

    def test_tags_canonical_ordering(self):
        backend = RecordingBackend()
        backend.counter("m", tags={"b": "2", "a": "1"})
        assert ("m", (("a", "1"), ("b", "2"))) in backend.counters

    def test_none_tags(self):
        backend = RecordingBackend()
        backend.counter("m", tags=None)
        assert ("m", ()) in backend.counters

    def test_timer_with_tags(self):
        backend = RecordingBackend()
        with backend.timer("t", tags={"env": "test"}):
            time.sleep(0.01)
        key = ("t", (("env", "test"),))
        assert key in backend.histograms
        assert len(backend.histograms[key]) == 1


# ---------------------------------------------------------------------------
# Reference model — uncovered ValueError path (lines 146-147)
# ---------------------------------------------------------------------------


class TestRefInstrumentWhitespaceValidatorMessage:
    def test_whitespace_only_ticker_error_message(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="   ",
                primary_venue="XNAS",
                asset_class="equity",
                name="Test",
            )

    def test_leading_space_ticker_error_message(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker=" AAPL",
                primary_venue="XNAS",
                asset_class="equity",
                name="Test",
            )

    def test_trailing_newline_ticker_rejected(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="AAPL\n",
                primary_venue="XNAS",
                asset_class="equity",
                name="Test",
            )

    def test_ticker_with_plus_sign(self):
        inst = RefInstrument(
            primary_ticker="C+",
            primary_venue="XNYS",
            asset_class="equity",
            name="Citigroup Plus",
        )
        assert inst.primary_ticker == "C+"

    def test_ticker_with_colon(self):
        inst = RefInstrument(
            primary_ticker="AAPL:US",
            primary_venue="XNAS",
            asset_class="equity",
            name="Apple US",
        )
        assert inst.primary_ticker == "AAPL:US"


# ---------------------------------------------------------------------------
# Reference API suggest — Yahoo fallback with asset_class filter on Yahoo results
# ---------------------------------------------------------------------------


class TestSuggestYahooFallbackAssetClass:
    @pytest.fixture
    async def fallback_client(self):
        app = FastAPI()
        app.include_router(reference_router, prefix="/api/v1/reference")
        app.dependency_overrides[get_search_index] = SearchIndex
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_yahoo_results_filtered_by_equity_class(self, fallback_client: AsyncClient):
        yahoo_results = [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "display": "AAPL — Apple",
                "completion": "Apple",
                "score": 80,
                "record": {
                    "id": "",
                    "primary_ticker": "AAPL",
                    "primary_venue": "XNAS",
                    "asset_class": "equity",
                    "name": "Apple",
                    "currency": "USD",
                },
            },
            {
                "symbol": "BTC-USD",
                "name": "Bitcoin",
                "display": "BTC-USD — Bitcoin",
                "completion": "Bitcoin",
                "score": 60,
                "record": {
                    "id": "",
                    "primary_ticker": "BTC-USD",
                    "primary_venue": "XCRY",
                    "asset_class": "crypto",
                    "name": "Bitcoin",
                    "currency": "USD",
                },
            },
        ]
        with patch(
            "engine.api.routes.reference._yahoo_search",
            new_callable=AsyncMock,
            return_value=yahoo_results,
        ):
            r = await fallback_client.get(
                "/api/v1/reference/suggest",
                params={"q": "test", "asset_class": "equity"},
            )
            assert r.status_code == HTTPStatus.OK
            for s in r.json()["suggestions"]:
                assert s["record"]["asset_class"] == "equity"

    async def test_yahoo_results_capped_at_limit(self, fallback_client: AsyncClient):
        yahoo_results = [
            {
                "symbol": f"T{i}",
                "name": f"Test {i}",
                "display": f"T{i}",
                "completion": f"Test {i}",
                "score": 60,
                "record": {
                    "id": "",
                    "primary_ticker": f"T{i}",
                    "primary_venue": "XNAS",
                    "asset_class": "equity",
                    "name": f"Test {i}",
                    "currency": "USD",
                },
            }
            for i in range(20)
        ]
        with patch(
            "engine.api.routes.reference._yahoo_search",
            new_callable=AsyncMock,
            return_value=yahoo_results,
        ):
            r = await fallback_client.get(
                "/api/v1/reference/suggest",
                params={"q": "test", "limit": 3},
            )
            assert r.status_code == HTTPStatus.OK
            assert len(r.json()["suggestions"]) <= 3


# ---------------------------------------------------------------------------
# Rolling benchmark — additional integration
# ---------------------------------------------------------------------------


class TestRollingBenchmarkIntegration:
    def test_rolling_beta_negative_window_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_beta([0.01], [0.01], -1)

    def test_rolling_alpha_with_zero_risk_free(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01]
        port = [b + 0.005 for b in bench]
        out = rolling_alpha(port, bench, 3, risk_free_rate=0.0)
        for v in out[2:]:
            assert v is not None
            assert v > 0

    def test_rolling_tracking_error_constant_active(self):
        port = [0.06, 0.06, 0.06, 0.06]
        bench = [0.01, 0.01, 0.01, 0.01]
        out = rolling_tracking_error(port, bench, 3)
        for v in out[2:]:
            assert v == pytest.approx(0.0, abs=1e-12)

    def test_rolling_ir_positive_outperformance(self):
        port = [0.10, 0.08, 0.12, 0.09, 0.11]
        bench = [0.01, 0.01, 0.01, 0.01, 0.01]
        out = rolling_information_ratio(port, bench, 3)
        for v in out[2:]:
            assert v is not None
            assert v > 0

    def test_rolling_beta_shorter_than_window_all_none(self):
        out = rolling_beta([0.01, 0.02], [0.01, 0.02], 5)
        assert all(v is None for v in out)

    def test_rolling_alpha_shorter_than_window_all_none(self):
        out = rolling_alpha([0.01, 0.02], [0.01, 0.02], 5)
        assert all(v is None for v in out)

    def test_rolling_te_shorter_than_window_all_none(self):
        out = rolling_tracking_error([0.01, 0.02], [0.01, 0.02], 5)
        assert all(v is None for v in out)

    def test_rolling_ir_shorter_than_window_all_none(self):
        out = rolling_information_ratio([0.01, 0.02], [0.01, 0.02], 5)
        assert all(v is None for v in out)


# ---------------------------------------------------------------------------
# Observability metrics — Protocol compliance
# ---------------------------------------------------------------------------


class TestMetricsProtocolCompliance:
    def test_null_backend_satisfies_protocol(self):
        from engine.observability.metrics import MetricsBackend

        assert isinstance(NullBackend(), MetricsBackend)

    def test_recording_backend_satisfies_protocol(self):
        from engine.observability.metrics import MetricsBackend

        assert isinstance(RecordingBackend(), MetricsBackend)

    def test_counter_with_tags(self):
        backend = RecordingBackend()
        backend.counter("req", tags={"method": "GET", "path": "/api"})
        backend.counter("req", tags={"path": "/api", "method": "GET"})
        key = ("req", (("method", "GET"), ("path", "/api")))
        assert backend.counters[key] == pytest.approx(2.0)

    def test_counter_default_value(self):
        backend = RecordingBackend()
        backend.counter("x")
        assert backend.counters[("x", ())] == 1.0

    def test_counter_custom_value(self):
        backend = RecordingBackend()
        backend.counter("x", value=5.0)
        assert backend.counters[("x", ())] == 5.0

    def test_counter_accumulates(self):
        backend = RecordingBackend()
        backend.counter("x", value=3.0)
        backend.counter("x", value=7.0)
        assert backend.counters[("x", ())] == 10.0

    def test_gauge_overwrites(self):
        backend = RecordingBackend()
        backend.gauge("g", 1.0)
        backend.gauge("g", 2.0)
        assert backend.gauges[("g", ())] == 2.0

    def test_histogram_appends(self):
        backend = RecordingBackend()
        backend.histogram("h", 1.0)
        backend.histogram("h", 2.0)
        assert backend.histograms[("h", ())] == [1.0, 2.0]


# ---------------------------------------------------------------------------
# Seed — deeper coverage
# ---------------------------------------------------------------------------


class TestSeedDeepCoverage:
    def test_seed_instruments_have_correct_venue_length(self):
        idx = SearchIndex()
        count = seed_index(idx)
        assert count > 100
        for rec in idx._records:
            assert len(rec.primary_venue) == 4

    def test_seed_search_by_prefix(self):
        idx = SearchIndex()
        seed_index(idx)
        results = idx.search("MS")
        tickers = [r.primary_ticker for r in results]
        assert "MSFT" in tickers

    def test_seed_suggest_by_name_prefix(self):
        idx = SearchIndex()
        seed_index(idx)
        suggestions = idx.suggest("Nvidia")
        names = [s.record.name for s in suggestions]
        assert any("NVIDIA" in n for n in names)

    def test_seed_suggest_crypto(self):
        idx = SearchIndex()
        seed_index(idx)
        suggestions = idx.suggest("BTC", asset_class="crypto")
        for s in suggestions:
            assert s.record.asset_class == "crypto"

    def test_seed_suggest_forex(self):
        idx = SearchIndex()
        seed_index(idx)
        suggestions = idx.suggest("EUR", asset_class="forex")
        for s in suggestions:
            assert s.record.asset_class == "forex"
