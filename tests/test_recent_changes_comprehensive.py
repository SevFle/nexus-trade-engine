"""Comprehensive tests for recently changed code — edge cases, boundary values,
property-based invariants, and integration coverage.

Targets (from git log HEAD~2..HEAD):
  - engine/core/benchmark_comparison.py
  - engine/core/cumulative_returns.py
  - engine/core/rolling_benchmark.py
  - engine/reference/model.py
  - engine/reference/seed.py
  - engine/api/routes/reference.py
  - engine/core/backtest_runner.py (date tz fix)
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from engine.api.routes.reference import (
    _MAX_LIMIT,
    _MAX_QUERY_LEN,
    _serialize_yahoo,
    get_search_index,
    router as reference_router,
)
from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.core.benchmark_comparison import (
    beta,
    capture_ratio,
    correlation,
    down_capture_ratio,
    jensen_alpha,
    up_capture_ratio,
)
from engine.core.cumulative_returns import (
    active_returns,
    beating_benchmark_pct,
    cumulative_returns,
    equity_curve_from_returns,
    log_returns,
    returns_from_equity,
    tracking_error,
)
from engine.core.rolling_benchmark import (
    rolling_alpha,
    rolling_beta,
    rolling_information_ratio,
    rolling_tracking_error,
)
from engine.data.feeds import MarketDataProvider
from engine.reference.model import (
    Classification,
    InstrumentIds,
    Listing,
    RefInstrument,
    Venue,
)
from engine.reference.search import SearchIndex
from engine.reference.seed import _INSTRUMENTS, seed_index


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


def _make_tz_aware_df(
    n_days: int = 10, base_price: float = 100.0, seed: int = 42
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    returns = rng.normal(0.001, 0.02, n_days)
    closes = base_price * np.cumprod(1 + returns)
    closes[0] = base_price
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000},
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


def _make_tz_naive_df(
    n_days: int = 10, base_price: float = 100.0, seed: int = 42
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


# ---------------------------------------------------------------------------
# benchmark_comparison — edge cases
# ---------------------------------------------------------------------------


class TestBetaEdgeCases:
    def test_two_identical_points_zero_variance(self):
        assert beta([0.05, 0.05], [0.05, 0.05]) == 0.0

    def test_large_values_stable(self):
        port = [1000.0, 2000.0, 3000.0]
        bench = [500.0, 1000.0, 1500.0]
        assert beta(port, bench) == pytest.approx(2.0, rel=1e-6)

    def test_very_small_values(self):
        port = [1e-10, 2e-10, 3e-10]
        bench = [1e-10, 1e-10, 1e-10]
        assert beta(port, bench) == 0.0

    def test_negative_bench_beta(self):
        bench = [-0.01, 0.02, -0.03, 0.01]
        port = [-0.02, 0.04, -0.06, 0.02]
        assert beta(port, bench) == pytest.approx(2.0)

    def test_all_zeros_returns_zero(self):
        assert beta([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]) == 0.0


class TestJensenAlphaEdgeCases:
    def test_positive_risk_free_rate(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [b + 0.005 for b in bench]
        a_zero_rf = jensen_alpha(port, bench, risk_free_rate=0.0)
        a_pos_rf = jensen_alpha(port, bench, risk_free_rate=0.02)
        assert a_zero_rf != a_pos_rf

    def test_negative_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            jensen_alpha([0.01, 0.02], [0.01, 0.02], annualisation_factor=-1)

    def test_alpha_with_custom_annualisation(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [b + 0.005 for b in bench]
        a1 = jensen_alpha(port, bench, annualisation_factor=1)
        a252 = jensen_alpha(port, bench, annualisation_factor=252)
        assert a252 == pytest.approx(a1 * 252, rel=1e-9)

    def test_high_risk_free_rate(self):
        bench = [0.01, 0.02, 0.03]
        port = [0.01, 0.02, 0.03]
        a = jensen_alpha(port, bench, risk_free_rate=0.5)
        assert a == pytest.approx(0.0, abs=1e-10)


class TestCaptureRatioEdgeCases:
    def test_up_capture_zero_benchmark_compound(self):
        port = [0.01, 0.01, -0.01]
        bench = [0.01, -0.01, 0.01]
        result = up_capture_ratio(port, bench)
        assert isinstance(result, float)

    def test_down_capture_zero_benchmark_compound(self):
        port = [-0.01, 0.01, -0.01]
        bench = [-0.01, 0.01, -0.01]
        result = down_capture_ratio(port, bench)
        assert isinstance(result, float)

    def test_up_capture_mixed_bars(self):
        port = [0.05, -0.02, 0.08, 0.03]
        bench = [0.03, -0.01, 0.06, 0.02]
        result = up_capture_ratio(port, bench)
        assert result > 1.0

    def test_down_capture_defensive_portfolio(self):
        port = [-0.01, 0.02, -0.005, 0.01]
        bench = [-0.03, 0.02, -0.02, 0.01]
        result = down_capture_ratio(port, bench)
        assert result < 1.0

    def test_capture_ratio_both_zero(self):
        assert capture_ratio([], []) == 0.0

    def test_capture_ratio_asymmetric(self):
        bench = [0.10, -0.10]
        port = [0.20, -0.05]
        result = capture_ratio(port, bench)
        assert result > 1.0


class TestCorrelationEdgeCases:
    def test_partial_correlation(self):
        port = [1.0, 2.0, 3.0, 4.0]
        bench = [2.0, 3.0, 2.0, 5.0]
        r = correlation(port, bench)
        assert -1.0 <= r <= 1.0

    def test_correlation_symmetry(self):
        port = [0.01, -0.02, 0.03, -0.01, 0.02]
        bench = [0.03, -0.01, 0.02, -0.03, 0.01]
        assert correlation(port, bench) == pytest.approx(correlation(bench, port))

    def test_correlation_with_negative_values(self):
        port = [-1.0, -2.0, -3.0]
        bench = [-2.0, -4.0, -6.0]
        assert correlation(port, bench) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# cumulative_returns — edge cases
# ---------------------------------------------------------------------------


class TestCumulativeReturnsEdgeCases:
    def test_all_zero_returns(self):
        out = cumulative_returns([0.0, 0.0, 0.0])
        assert all(v == pytest.approx(0.0) for v in out)

    def test_single_large_loss(self):
        out = cumulative_returns([-0.50])
        assert out == [pytest.approx(-0.50)]

    def test_total_loss_returns_minus_one(self):
        out = cumulative_returns([-1.0])
        assert out == [pytest.approx(-1.0)]

    def test_alternating_gains_losses(self):
        out = cumulative_returns([0.10, -0.10, 0.10, -0.10])
        expected_final = (1.10 * 0.90 * 1.10 * 0.90) - 1.0
        assert out[-1] == pytest.approx(expected_final)

    def test_very_long_series(self):
        returns = [0.001] * 1000
        out = cumulative_returns(returns)
        assert len(out) == 1000
        assert out[-1] == pytest.approx((1.001**1000) - 1, rel=1e-6)


class TestEquityCurveEdgeCases:
    def test_zero_return_preserves_initial(self):
        out = equity_curve_from_returns([0.0, 0.0, 0.0], initial_value=1000.0)
        assert all(v == pytest.approx(1000.0) for v in out)

    def test_total_loss_equity_goes_to_zero(self):
        out = equity_curve_from_returns([-1.0], initial_value=100.0)
        assert out[1] == pytest.approx(0.0)

    def test_large_initial_value(self):
        out = equity_curve_from_returns([0.10], initial_value=1e12)
        assert out[1] == pytest.approx(1.1e12)

    def test_custom_initial_small(self):
        out = equity_curve_from_returns([0.50], initial_value=0.01)
        assert out[1] == pytest.approx(0.015)


class TestLogReturnsEdgeCases:
    def test_negative_return(self):
        out = log_returns([-0.5])
        assert out[0] == pytest.approx(math.log(0.5))

    def test_very_small_positive_return(self):
        out = log_returns([1e-15])
        assert out[0] == pytest.approx(1e-15, abs=1e-14)

    def test_large_positive_return(self):
        out = log_returns([1.0])
        assert out[0] == pytest.approx(math.log(2.0))

    def test_exactly_negative_one_boundary(self):
        with pytest.raises(ValueError):
            log_returns([-1.0])

    def test_slightly_below_negative_one(self):
        with pytest.raises(ValueError):
            log_returns([-1.00001])


class TestReturnsFromEquityEdgeCases:
    def test_constant_equity_zero_returns(self):
        out = returns_from_equity([100.0, 100.0, 100.0])
        assert all(v == pytest.approx(0.0) for v in out)

    def test_doubling_equity(self):
        out = returns_from_equity([100.0, 200.0, 400.0])
        assert out == [pytest.approx(1.0), pytest.approx(1.0)]

    def test_multi_zero_equity(self):
        out = returns_from_equity([0.0, 0.0, 100.0])
        assert out[0] == 0.0
        assert out[1] == 0.0


class TestActiveReturnsEdgeCases:
    def test_all_positive_active(self):
        port = [0.10, 0.10, 0.10]
        bench = [0.05, 0.05, 0.05]
        out = active_returns(port, bench)
        assert all(v == pytest.approx(0.05) for v in out)

    def test_all_negative_active(self):
        port = [0.01, 0.01]
        bench = [0.05, 0.05]
        out = active_returns(port, bench)
        assert all(v == pytest.approx(-0.04) for v in out)

    def test_mixed_active(self):
        port = [0.10, -0.05]
        bench = [0.05, 0.05]
        out = active_returns(port, bench)
        assert out[0] == pytest.approx(0.05)
        assert out[1] == pytest.approx(-0.10)


class TestTrackingErrorEdgeCases:
    def test_single_point_zero(self):
        assert tracking_error([0.05], [0.03]) == 0.0

    def test_large_annualisation_factor(self):
        out = tracking_error(
            [0.10, -0.05, 0.10, -0.05],
            [0.05, 0.05, 0.05, 0.05],
            annualisation_factor=10000,
        )
        assert out > 0

    def test_annualisation_scaling(self):
        port = [0.10, -0.05, 0.10, -0.05]
        bench = [0.05, 0.05, 0.05, 0.05]
        te1 = tracking_error(port, bench, annualisation_factor=1)
        te252 = tracking_error(port, bench, annualisation_factor=252)
        assert te252 == pytest.approx(te1 * math.sqrt(252), rel=1e-9)


class TestBeatingBenchmarkPctEdgeCases:
    def test_single_bar_beats(self):
        assert beating_benchmark_pct([0.06], [0.05]) == pytest.approx(1.0)

    def test_single_bar_doesnt_beat(self):
        assert beating_benchmark_pct([0.04], [0.05]) == pytest.approx(0.0)

    def test_exactly_equal_zero(self):
        assert beating_benchmark_pct([0.05], [0.05]) == pytest.approx(0.0)

    def test_half_beat_half(self):
        out = beating_benchmark_pct(
            [0.10, 0.01, 0.10, 0.01],
            [0.05, 0.05, 0.05, 0.05],
        )
        assert out == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# rolling_benchmark — additional edge cases
# ---------------------------------------------------------------------------


class TestRollingBetaEdgeCases:
    def test_window_equals_length(self):
        bench = [0.01, -0.02, 0.03]
        port = list(bench)
        out = rolling_beta(port, bench, 3)
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(1.0)

    def test_window_two(self):
        out = rolling_beta([0.01, 0.02, 0.03], [0.01, 0.02, 0.03], 2)
        assert out[0] is None
        assert out[1] is not None
        assert out[2] is not None

    def test_all_same_returns(self):
        out = rolling_beta([0.05, 0.05, 0.05, 0.05], [0.05, 0.05, 0.05, 0.05], 3)
        assert out[2] == 0.0

    def test_negative_window_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_beta([0.01], [0.01], -1)


class TestRollingAlphaEdgeCases:
    def test_with_risk_free_rate(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.01]
        port = [0.04, -0.01, 0.06, -0.02, 0.04, 0.02, -0.005]
        out_no_rf = rolling_alpha(port, bench, 4, risk_free_rate=0.0)
        out_with_rf = rolling_alpha(port, bench, 4, risk_free_rate=0.05)
        for a, b_val in zip(out_no_rf, out_with_rf, strict=True):
            if a is not None and b_val is not None:
                assert a != b_val

    def test_window_equals_length(self):
        bench = [0.01, -0.02, 0.03]
        port = list(bench)
        out = rolling_alpha(port, bench, 3)
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(0.0, abs=1e-12)

    def test_custom_annualisation(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [b + 0.001 for b in bench]
        a1 = rolling_alpha(port, bench, 3, annualisation_factor=1)
        a252 = rolling_alpha(port, bench, 3, annualisation_factor=252)
        for v1, v252 in zip(a1, a252, strict=True):
            if v1 is not None and v252 is not None:
                assert v252 == pytest.approx(v1 * 252, rel=1e-9)

    def test_negative_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            rolling_alpha([0.01, 0.02], [0.01, 0.02], 2, annualisation_factor=-5)


class TestRollingTrackingErrorEdgeCases:
    def test_window_equals_length(self):
        bench = [0.01, -0.02, 0.03]
        port = list(bench)
        out = rolling_tracking_error(port, bench, 3)
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(0.0, abs=1e-12)

    def test_annualisation_scaling(self):
        port = [0.10, -0.05, 0.10, -0.05, 0.10]
        bench = [0.05, 0.05, 0.05, 0.05, 0.05]
        te1 = rolling_tracking_error(port, bench, 3, annualisation_factor=1)
        te252 = rolling_tracking_error(port, bench, 3, annualisation_factor=252)
        for v1, v252 in zip(te1, te252, strict=True):
            if v1 is not None and v252 is not None:
                assert v252 == pytest.approx(v1 * math.sqrt(252), rel=1e-9)


class TestRollingInformationRatioEdgeCases:
    def test_window_equals_length(self):
        port = [0.06, 0.04, 0.05]
        bench = [0.01, 0.01, 0.01]
        out = rolling_information_ratio(port, bench, 3)
        assert out[:2] == [None, None]
        assert out[2] is not None
        assert out[2] > 0

    def test_negative_ir(self):
        port = [0.01, 0.01, 0.01]
        bench = [0.05, 0.06, 0.04]
        out = rolling_information_ratio(port, bench, 3)
        if out[2] is not None:
            assert out[2] < 0

    def test_zero_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            rolling_information_ratio(
                [0.01, 0.02], [0.01, 0.02], 2, annualisation_factor=0
            )

    def test_window_one_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_information_ratio([0.01], [0.01], 1)


# ---------------------------------------------------------------------------
# backtest_runner — timezone-aware fix verification
# ---------------------------------------------------------------------------


class TestBacktestRunnerTzFix:
    async def test_tz_aware_dataframe(self):
        df = _make_tz_aware_df(n_days=10)
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="test",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            random_seed=42,
        )

        class _HoldStrategy:
            name = "test"
            version = "1.0.0"
            def on_bar(self, state, portfolio):
                return []

        runner = BacktestRunner(config=config, strategy=_HoldStrategy(), provider=provider)
        result = await runner.run()
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)

    async def test_tz_naive_dataframe(self):
        df = _make_tz_naive_df(n_days=10)
        provider = _SyntheticProvider(df)
        config = BacktestConfig(
            strategy_name="test",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            random_seed=42,
        )

        class _HoldStrategy:
            name = "test"
            version = "1.0.0"
            def on_bar(self, state, portfolio):
                return []

        runner = BacktestRunner(config=config, strategy=_HoldStrategy(), provider=provider)
        result = await runner.run()
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)


# ---------------------------------------------------------------------------
# reference model — additional validation tests
# ---------------------------------------------------------------------------


class TestRefInstrumentValidation:
    def test_valid_instrument_all_fields(self):
        inst = RefInstrument(
            primary_ticker="AAPL",
            primary_venue="XNAS",
            asset_class="equity",
            name="Apple Inc.",
            currency="USD",
            lot_size=Decimal("100"),
            tick_size=Decimal("0.01"),
            ids=InstrumentIds(isin="US0378331005"),
            classification=Classification(gics_sector="Technology"),
        )
        assert inst.primary_ticker == "AAPL"
        assert inst.lot_size == Decimal("100")

    def test_invalid_ticker_special_chars(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="AAPL/LTC",
                primary_venue="XNAS",
                asset_class="equity",
                name="Bad",
            )

    def test_invalid_ticker_too_long(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="A" * 33,
                primary_venue="XNAS",
                asset_class="equity",
                name="Too Long Ticker",
            )

    def test_valid_ticker_with_dots(self):
        inst = RefInstrument(
            primary_ticker="BRK.B",
            primary_venue="XNYS",
            asset_class="equity",
            name="Berkshire",
        )
        assert inst.primary_ticker == "BRK.B"

    def test_valid_ticker_with_dash(self):
        inst = RefInstrument(
            primary_ticker="BTC-USD",
            primary_venue="XCRY",
            asset_class="crypto",
            name="Bitcoin",
        )
        assert inst.primary_ticker == "BTC-USD"

    def test_valid_ticker_with_equals(self):
        inst = RefInstrument(
            primary_ticker="EURUSD=X",
            primary_venue="XFXS",
            asset_class="forex",
            name="EUR/USD",
        )
        assert inst.primary_ticker == "EURUSD=X"

    def test_invalid_venue_length(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="AAPL",
                primary_venue="XX",
                asset_class="equity",
                name="Bad Venue",
            )

    def test_invalid_currency_length(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="AAPL",
                primary_venue="XNAS",
                asset_class="equity",
                name="Bad Currency",
                currency="US",
            )

    def test_all_asset_classes_valid(self):
        for ac in ("equity", "etf", "crypto", "crypto_perp", "crypto_future", "forex", "option", "future"):
            inst = RefInstrument(
                primary_ticker="TEST",
                primary_venue="XNAS",
                asset_class=ac,
                name=f"Test {ac}",
            )
            assert inst.asset_class == ac

    def test_invalid_asset_class(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="TEST",
                primary_venue="XNAS",
                asset_class="invalid",
                name="Bad",
            )

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="TEST",
                primary_venue="XNAS",
                asset_class="equity",
                name="",
            )

    def test_default_values(self):
        inst = RefInstrument(
            primary_ticker="TEST",
            primary_venue="XNAS",
            asset_class="equity",
            name="Test",
        )
        assert inst.active is True
        assert inst.currency == "USD"
        assert inst.lot_size == Decimal("1")
        assert inst.tick_size == Decimal("0.01")
        assert inst.listings == []
        assert inst.metadata == {}
        assert inst.ids == InstrumentIds()

    def test_validate_assignment(self):
        inst = RefInstrument(
            primary_ticker="TEST",
            primary_venue="XNAS",
            asset_class="equity",
            name="Test",
        )
        with pytest.raises(ValidationError):
            inst.primary_ticker = " BAD"


class TestInstrumentIds:
    def test_valid_isin(self):
        ids = InstrumentIds(isin="US0378331005")
        assert ids.isin == "US0378331005"

    def test_invalid_isin_too_short(self):
        with pytest.raises(ValidationError):
            InstrumentIds(isin="TOOSHORT")

    def test_invalid_isin_too_long(self):
        with pytest.raises(ValidationError):
            InstrumentIds(isin="WAYTOOLONGISIN")

    def test_valid_cusip(self):
        ids = InstrumentIds(cusip="037833100")
        assert ids.cusip == "037833100"

    def test_invalid_cusip_length(self):
        with pytest.raises(ValidationError):
            InstrumentIds(cusip="TOOLONG")

    def test_all_none_by_default(self):
        ids = InstrumentIds()
        assert ids.isin is None
        assert ids.cusip is None
        assert ids.figi is None
        assert ids.sedol is None
        assert ids.cik is None


class TestListingModel:
    def test_valid_listing(self):
        listing = Listing(
            venue="XNAS", ticker="AAPL", currency="USD", active_from=date(2020, 1, 1)
        )
        assert listing.is_active is True

    def test_inactive_listing(self):
        listing = Listing(
            venue="XNAS",
            ticker="FB",
            currency="USD",
            active_from=date(2012, 5, 18),
            active_to=date(2023, 6, 1),
        )
        assert listing.is_active is False

    def test_invalid_venue(self):
        with pytest.raises(ValidationError):
            Listing(
                venue="INVALID",
                ticker="AAPL",
                currency="USD",
                active_from=date(2020, 1, 1),
            )


class TestVenueModel:
    def test_valid_venue(self):
        v = Venue(mic="XNAS", name="Nasdaq", country="US", timezone="America/New_York")
        assert v.mic == "XNAS"

    def test_invalid_country_length(self):
        with pytest.raises(ValidationError):
            Venue(mic="XNAS", name="Test", country="USA", timezone="UTC")


# ---------------------------------------------------------------------------
# seed — coverage deepening
# ---------------------------------------------------------------------------


class TestSeedIndexDeep:
    def test_all_seed_records_unique_ids(self):
        ids = set()
        for row in _INSTRUMENTS:
            inst = RefInstrument(**row)
            assert inst.id not in ids
            ids.add(inst.id)

    def test_no_duplicate_tickers(self):
        tickers = [r["primary_ticker"] for r in _INSTRUMENTS]
        assert len(tickers) == len(set(tickers))

    def test_crypto_tickers_contain_hyphen_or_not(self):
        crypto = [r for r in _INSTRUMENTS if r["asset_class"] == "crypto"]
        assert len(crypto) > 0
        with_hyphen = [r for r in crypto if "-" in r["primary_ticker"]]
        without_hyphen = [r for r in crypto if "-" not in r["primary_ticker"]]
        assert len(with_hyphen) > 0
        assert len(without_hyphen) > 0

    def test_seed_count_matches_actual_length(self):
        idx = SearchIndex()
        count = seed_index(idx)
        assert count == len(_INSTRUMENTS)
        assert count > 100

    def test_seed_search_case_insensitive(self):
        idx = SearchIndex()
        seed_index(idx)
        upper = idx.search("AAPL")
        lower = idx.search("aapl")
        assert len(upper) > 0
        assert len(lower) > 0

    def test_seed_venue_codes_valid(self):
        valid_venues = {"XNAS", "XNYS", "XASE", "XCRY", "XFXS", "XOTC"}
        for row in _INSTRUMENTS:
            assert row["primary_venue"] in valid_venues

    def test_seed_forex_tickers_contain_equals(self):
        forex = [r for r in _INSTRUMENTS if r["asset_class"] == "forex"]
        assert len(forex) > 0
        for r in forex:
            assert "=" in r["primary_ticker"]


# ---------------------------------------------------------------------------
# reference API suggest — additional integration tests
# ---------------------------------------------------------------------------


class TestSuggestEndpointIntegration:
    @pytest.fixture
    async def client(self):
        app = FastAPI()
        app.include_router(reference_router, prefix="/api/v1/reference")
        idx = SearchIndex()
        seed_index(idx)
        app.dependency_overrides[get_search_index] = lambda: idx
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_suggest_returns_suggestions(self, client: AsyncClient):
        r = await client.get("/api/v1/reference/suggest", params={"q": "AAPL"})
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        assert len(body["suggestions"]) > 0
        assert body["suggestions"][0]["symbol"] == "AAPL"

    async def test_suggest_partial_ticker(self, client: AsyncClient):
        r = await client.get("/api/v1/reference/suggest", params={"q": "APP"})
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        tickers = [s["symbol"] for s in body["suggestions"]]
        assert "AAPL" in tickers

    async def test_suggest_by_name(self, client: AsyncClient):
        r = await client.get("/api/v1/reference/suggest", params={"q": "Apple"})
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        assert any("Apple" in s["name"] for s in body["suggestions"])

    async def test_suggest_asset_class_filter(self, client: AsyncClient):
        r = await client.get(
            "/api/v1/reference/suggest",
            params={"q": "BTC", "asset_class": "crypto"},
        )
        assert r.status_code == HTTPStatus.OK
        for s in r.json()["suggestions"]:
            assert s["record"]["asset_class"] == "crypto"

    async def test_suggest_etf_filter(self, client: AsyncClient):
        r = await client.get(
            "/api/v1/reference/suggest",
            params={"q": "SPY", "asset_class": "etf"},
        )
        assert r.status_code == HTTPStatus.OK
        for s in r.json()["suggestions"]:
            assert s["record"]["asset_class"] == "etf"

    async def test_suggest_empty_query_400(self, client: AsyncClient):
        r = await client.get("/api/v1/reference/suggest", params={"q": ""})
        assert r.status_code == HTTPStatus.BAD_REQUEST

    async def test_suggest_limit_one(self, client: AsyncClient):
        r = await client.get(
            "/api/v1/reference/suggest", params={"q": "A", "limit": 1}
        )
        assert r.status_code == HTTPStatus.OK
        assert len(r.json()["suggestions"]) <= 1

    async def test_suggest_limit_zero_422(self, client: AsyncClient):
        r = await client.get(
            "/api/v1/reference/suggest", params={"q": "A", "limit": 0}
        )
        assert r.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_suggest_no_results_returns_empty_list(self, client: AsyncClient):
        with patch(
            "engine.api.routes.reference._yahoo_search",
            new_callable=AsyncMock,
            return_value=[],
        ):
            r = await client.get(
                "/api/v1/reference/suggest",
                params={"q": "ZZZZZZZZZ_NONEXISTENT"},
            )
            assert r.status_code == HTTPStatus.OK
            assert r.json()["suggestions"] == []

    async def test_suggestion_has_expected_fields(self, client: AsyncClient):
        r = await client.get("/api/v1/reference/suggest", params={"q": "AAPL"})
        body = r.json()
        if body["suggestions"]:
            s = body["suggestions"][0]
            assert "symbol" in s
            assert "name" in s
            assert "display" in s
            assert "score" in s
            assert "record" in s
            rec = s["record"]
            assert "id" in rec
            assert "primary_ticker" in rec
            assert "primary_venue" in rec
            assert "asset_class" in rec


class TestSerializeYahooAdditional:
    def test_serialization_structure_completeness(self):
        item = {
            "symbol": "NVDA",
            "shortname": "NVIDIA",
            "quoteType": "EQUITY",
            "exchange": "XNAS",
            "currency": "USD",
        }
        result = _serialize_yahoo(item)
        assert set(result.keys()) >= {"symbol", "name", "display", "completion", "score", "record"}
        rec = result["record"]
        assert set(rec.keys()) >= {"id", "primary_ticker", "primary_venue", "asset_class", "name", "currency"}

    def test_yahoo_default_exchange(self):
        item = {"symbol": "X", "quoteType": "EQUITY", "exchange": ""}
        result = _serialize_yahoo(item)
        assert result["record"]["primary_venue"] == ""


# ---------------------------------------------------------------------------
# Property-based invariants
# ---------------------------------------------------------------------------


class TestMathInvariants:
    def test_cumulative_then_equity_round_trip(self):
        original_returns = [0.05, -0.02, 0.10, -0.03, 0.01]
        equity = equity_curve_from_returns(original_returns, initial_value=1000.0)
        recovered = returns_from_equity(equity)
        for o, r in zip(original_returns, recovered, strict=True):
            assert r == pytest.approx(o, rel=1e-12)

    def test_log_returns_inverse_property(self):
        returns = [0.05, -0.02, 0.10, -0.03]
        log_r = log_returns(returns)
        for simple, log_val in zip(returns, log_r, strict=True):
            assert math.exp(log_val) == pytest.approx(1.0 + simple, rel=1e-12)

    def test_beta_of_scaled_portfolio(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        for scale in [0.5, 1.0, 1.5, 2.0, -1.0]:
            port = [scale * b for b in bench]
            assert beta(port, bench) == pytest.approx(scale, rel=1e-9)

    def test_tracking_error_vs_manual_calculation(self):
        port = [0.10, -0.05, 0.08, -0.02, 0.06]
        bench = [0.05, 0.03, 0.04, 0.02, 0.01]
        active = [p - b for p, b in zip(port, bench, strict=True)]
        n = len(active)
        mean_active = sum(active) / n
        var = sum((x - mean_active) ** 2 for x in active) / (n - 1)
        expected_te = math.sqrt(var) * math.sqrt(252)
        actual_te = tracking_error(port, bench)
        assert actual_te == pytest.approx(expected_te, rel=1e-9)

    def test_rolling_beta_consistent_with_full_period(self):
        port = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.02, 0.03]
        bench = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.02, 0.03]
        full_beta = beta(port, bench)
        rolling = rolling_beta(port, bench, len(port))
        assert rolling[-1] == pytest.approx(full_beta, rel=1e-9)

    def test_cumulative_returns_monotonic_with_positive_returns(self):
        returns = [0.01, 0.02, 0.03, 0.04, 0.05]
        cum = cumulative_returns(returns)
        for i in range(1, len(cum)):
            assert cum[i] > cum[i - 1]
