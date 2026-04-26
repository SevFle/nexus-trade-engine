"""Integration tests for Data Quality & Integrity Layer.

Uses synthetic OHLCV DataFrames — no mocks of the system under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.data.market_state import MarketStateBuilder
from engine.data.quality import (
    CorrectionRecord,
    DataQualityReport,
    DataValidator,
    GBpGBPRule,
    PriceBoundsRule,
    RatioSanityRule,
    StaleDataRule,
    ValidationConfig,
)

_TOLERANCE = 1e-10
_CUSTOM_PE = 300
_CUSTOM_STALE_DAYS = 3
_FROM_DICT_PE = 250
_CORRECTED_GBP = 5.80
_MAX_NORMALIZED_MEDIAN = 20.0
_CUSTOM_CONFIG_PE = 200


def _make_ohlcv_df(
    n_bars: int,
    start: datetime = datetime(2025, 1, 1, tzinfo=UTC),
    base_price: float = 100.0,
    base_volume: int = 500_000,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = [start + timedelta(days=i) for i in range(n_bars)]
    closes = base_price + np.cumsum(rng.normal(0, 1, n_bars))
    opens = closes + rng.normal(0, 0.5, n_bars)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.3, n_bars))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.3, n_bars))
    volumes = rng.integers(max(base_volume // 5, 1), base_volume * 2, n_bars)
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


def _make_fundamentals_df(
    n_quarters: int = 8,
    base_revenue: float = 1e9,
    base_book_value: float = 5e9,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    quarters = pd.date_range("2024-01-01", periods=n_quarters, freq="QS")
    revenue = base_revenue + rng.normal(0, base_revenue * 0.05, n_quarters)
    book_value = base_book_value + rng.normal(0, base_book_value * 0.02, n_quarters)
    return pd.DataFrame(
        {"revenue": revenue, "book_value": book_value},
        index=quarters,
    )


class TestValidationConfig:
    def test_default_config_has_sensible_defaults(self):
        config = ValidationConfig()
        assert config.max_pe_ratio > 0
        assert config.max_pb_ratio > 0
        assert config.stale_volume_threshold >= 0
        assert config.stale_consecutive_days > 0

    def test_custom_config_overrides_defaults(self):
        config = ValidationConfig(
            max_pe_ratio=_CUSTOM_PE, stale_consecutive_days=_CUSTOM_STALE_DAYS
        )
        assert config.max_pe_ratio == _CUSTOM_PE
        assert config.stale_consecutive_days == _CUSTOM_STALE_DAYS

    def test_from_dict(self):
        config = ValidationConfig.from_dict({"max_pe_ratio": _FROM_DICT_PE})
        assert config.max_pe_ratio == _FROM_DICT_PE

    def test_from_dict_empty_returns_defaults(self):
        config = ValidationConfig.from_dict({})
        default = ValidationConfig()
        assert config.max_pe_ratio == default.max_pe_ratio


class TestCorrectionRecord:
    def test_record_fields(self):
        record = CorrectionRecord(
            symbol="AAPL",
            field="close",
            original_value=-5.0,
            corrected_value=None,
            reason="negative_price",
            action="exclude",
        )
        assert record.symbol == "AAPL"
        assert record.action == "exclude"

    def test_record_accepts_correction(self):
        record = CorrectionRecord(
            symbol="BARC.L",
            field="close",
            original_value=580.0,
            corrected_value=_CORRECTED_GBP,
            reason="gbp_gbp_mismatch",
            action="normalize",
        )
        assert record.corrected_value == _CORRECTED_GBP


class TestDataQualityReport:
    def test_empty_report(self):
        report = DataQualityReport()
        assert len(report.corrections) == 0
        assert report.flagged_percentage == 0.0

    def test_add_correction(self):
        report = DataQualityReport()
        report.add_correction(
            symbol="AAPL",
            field="close",
            original_value=-5.0,
            corrected_value=None,
            reason="negative_price",
            action="exclude",
        )
        assert len(report.corrections) == 1

    def test_flagged_percentage(self):
        report = DataQualityReport()
        universe = ["AAPL", "MSFT", "GOOG"]
        report.add_correction("AAPL", "close", -1.0, None, "negative_price", "exclude")
        report.add_correction("AAPL", "volume", 0, None, "zero_volume", "exclude")
        pct = report.flagged_percentage_for_universe(universe)
        assert abs(pct - 100.0 / 3.0) < _TOLERANCE

    def test_should_warn_when_threshold_exceeded(self):
        report = DataQualityReport()
        universe = ["AAPL", "MSFT"]
        report.add_correction("AAPL", "close", -1.0, None, "negative_price", "exclude")
        report.add_correction("MSFT", "close", -2.0, None, "negative_price", "exclude")
        assert report.should_warn(universe, threshold_pct=10.0)

    def test_should_not_warn_below_threshold(self):
        report = DataQualityReport()
        universe = ["AAPL"] + [f"SYM{i}" for i in range(1, 20)]
        report.add_correction("AAPL", "close", -1.0, None, "negative_price", "exclude")
        assert not report.should_warn(universe, threshold_pct=10.0)

    def test_corrections_by_symbol(self):
        report = DataQualityReport()
        report.add_correction("AAPL", "close", -1.0, None, "negative_price", "exclude")
        report.add_correction("MSFT", "close", -2.0, None, "negative_price", "exclude")
        report.add_correction("AAPL", "volume", 0, None, "stale", "flag")
        by_symbol = report.corrections_by_symbol()
        _expected_aapl = 2
        _expected_msft = 1
        assert len(by_symbol["AAPL"]) == _expected_aapl
        assert len(by_symbol["MSFT"]) == _expected_msft


class TestPriceBoundsRule:
    def test_flags_negative_close(self):
        df = _make_ohlcv_df(20)
        df.iloc[5, df.columns.get_loc("close")] = -10.0
        rule = PriceBoundsRule()
        report = DataQualityReport()
        rule.apply(df, "AAPL", report)
        assert any(c.reason == "negative_price" for c in report.corrections)

    def test_flags_zero_close(self):
        df = _make_ohlcv_df(20)
        df.iloc[5, df.columns.get_loc("close")] = 0.0
        rule = PriceBoundsRule()
        report = DataQualityReport()
        rule.apply(df, "AAPL", report)
        assert any(c.reason == "negative_price" for c in report.corrections)

    def test_clean_data_passes(self):
        df = _make_ohlcv_df(20)
        rule = PriceBoundsRule()
        report = DataQualityReport()
        rule.apply(df, "AAPL", report)
        assert len(report.corrections) == 0

    def test_negative_price_rows_excluded(self):
        df = _make_ohlcv_df(20)
        df.iloc[5, df.columns.get_loc("close")] = -5.0
        rule = PriceBoundsRule()
        report = DataQualityReport()
        cleaned = rule.apply(df, "AAPL", report)
        neg_rows = cleaned[cleaned["close"] <= 0]
        assert len(neg_rows) == 0

    def test_custom_min_price(self):
        config = ValidationConfig(min_price=1.0)
        df = _make_ohlcv_df(20, base_price=0.5)
        rule = PriceBoundsRule(config=config)
        report = DataQualityReport()
        rule.apply(df, "PENNY", report)
        assert any(c.reason == "price_below_minimum" for c in report.corrections)


class TestRatioSanityRule:
    def test_flags_impossible_pe_ratio(self):
        df = _make_ohlcv_df(20, base_price=100.0)
        rule = RatioSanityRule()
        report = DataQualityReport()
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, 1e9], "eps": [0.001, 0.001]},
            index=pd.date_range("2024-01-01", periods=2, freq="QS"),
        )
        rule.apply(df, "AAPL", report, fundamentals=fundamentals)
        assert any(c.reason == "impossible_pe_ratio" for c in report.corrections)

    def test_clean_fundamentals_pass(self):
        df = _make_ohlcv_df(20, base_price=100.0)
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, 1e9], "eps": [5.0, 5.5]},
            index=pd.date_range("2024-01-01", periods=2, freq="QS"),
        )
        rule = RatioSanityRule()
        report = DataQualityReport()
        rule.apply(df, "AAPL", report, fundamentals=fundamentals)
        assert not any(c.reason == "impossible_pe_ratio" for c in report.corrections)

    def test_flags_negative_book_value_for_healthy_company(self):
        df = _make_ohlcv_df(20, base_price=100.0)
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, 1e9], "book_value": [-5e9, -5e9]},
            index=pd.date_range("2024-01-01", periods=2, freq="QS"),
        )
        rule = RatioSanityRule()
        report = DataQualityReport()
        rule.apply(df, "AAPL", report, fundamentals=fundamentals)
        assert any(c.reason == "impossible_pb_ratio" for c in report.corrections)

    def test_negative_revenue_flagged_when_adjacent_positive(self):
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, 1e9, -1e9, 1e9], "book_value": [5e9] * 4},
            index=pd.date_range("2024-01-01", periods=4, freq="QS"),
        )
        rule = RatioSanityRule()
        report = DataQualityReport()
        rule.apply_fundamentals("AAPL", report, fundamentals)
        assert any(c.reason == "negative_revenue" for c in report.corrections)

    def test_negative_revenue_interpolated(self):
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, 1e9, -1e9, 1e9], "book_value": [5e9] * 4},
            index=pd.date_range("2024-01-01", periods=4, freq="QS"),
        )
        rule = RatioSanityRule()
        report = DataQualityReport()
        cleaned = rule.apply_fundamentals("AAPL", report, fundamentals)
        assert (cleaned["revenue"] > 0).all()

    def test_negative_revenue_allowed_if_consistent(self):
        fundamentals = pd.DataFrame(
            {"revenue": [-1e9, -1.2e9, -0.8e9, -1.1e9], "book_value": [5e9] * 4},
            index=pd.date_range("2024-01-01", periods=4, freq="QS"),
        )
        rule = RatioSanityRule()
        report = DataQualityReport()
        rule.apply_fundamentals("AAPL", report, fundamentals)
        assert not any(c.reason == "negative_revenue" for c in report.corrections)


class TestGBpGBPRule:
    def test_detects_gbp_gbp_mismatch(self):
        df = _make_ohlcv_df(20, base_price=580.0)
        rule = GBpGBPRule()
        report = DataQualityReport()
        rule.apply(df, "BARC.L", report, sector_median_price=5.8)
        assert any(c.reason == "gbp_gbp_mismatch" for c in report.corrections)

    def test_normalizes_prices_by_factor_100(self):
        df = _make_ohlcv_df(20, base_price=580.0)
        rule = GBpGBPRule()
        report = DataQualityReport()
        cleaned = rule.apply(df, "BARC.L", report, sector_median_price=5.8)
        assert any(c.action == "normalize" for c in report.corrections)
        median_price = float(cleaned["close"].median())  # type: ignore[arg-type]
        assert median_price < _MAX_NORMALIZED_MEDIAN

    def test_does_not_flag_normal_prices(self):
        df = _make_ohlcv_df(20, base_price=5.8)
        rule = GBpGBPRule()
        report = DataQualityReport()
        rule.apply(df, "BARC.L", report, sector_median_price=5.8)
        assert len(report.corrections) == 0

    def test_does_not_flag_if_no_sector_median(self):
        df = _make_ohlcv_df(20, base_price=580.0)
        rule = GBpGBPRule()
        report = DataQualityReport()
        rule.apply(df, "BARC.L", report)
        assert len(report.corrections) == 0


class TestStaleDataRule:
    def test_flags_stale_data(self):
        df = _make_ohlcv_df(20, base_volume=500_000)
        stale_start = 10
        df.iloc[stale_start : stale_start + 6, df.columns.get_loc("volume")] = 0
        rule = StaleDataRule()
        report = DataQualityReport()
        rule.apply(df, "DEAD", report)
        assert any(c.reason == "stale_data" for c in report.corrections)

    def test_does_not_flag_brief_volume_dip(self):
        df = _make_ohlcv_df(20, base_volume=500_000)
        df.iloc[10:13, df.columns.get_loc("volume")] = 0
        rule = StaleDataRule()
        report = DataQualityReport()
        rule.apply(df, "AAPL", report)
        assert len(report.corrections) == 0

    def test_custom_stale_days(self):
        config = ValidationConfig(stale_consecutive_days=_CUSTOM_STALE_DAYS)
        df = _make_ohlcv_df(20, base_volume=500_000)
        df.iloc[10:13, df.columns.get_loc("volume")] = 0
        rule = StaleDataRule(config=config)
        report = DataQualityReport()
        rule.apply(df, "AAPL", report)
        assert any(c.reason == "stale_data" for c in report.corrections)

    def test_stale_flag_does_not_remove_data(self):
        df = _make_ohlcv_df(20, base_volume=500_000)
        df.iloc[10:16, df.columns.get_loc("volume")] = 0
        rule = StaleDataRule()
        report = DataQualityReport()
        cleaned = rule.apply(df, "DEAD", report)
        assert len(cleaned) == len(df)


class TestDataValidator:
    def test_clean_data_passes_all_rules(self):
        df = _make_ohlcv_df(60)
        validator = DataValidator()
        report = DataQualityReport()
        validator.validate(df, "AAPL", report)
        assert len(report.corrections) == 0

    def test_negative_prices_are_excluded(self):
        df = _make_ohlcv_df(60)
        df.iloc[5, df.columns.get_loc("close")] = -10.0
        df.iloc[15, df.columns.get_loc("close")] = -5.0
        validator = DataValidator()
        report = DataQualityReport()
        cleaned = validator.validate(df, "AAPL", report)
        neg_rows = cleaned[cleaned["close"] <= 0]
        _expected_corrections = 2
        assert len(neg_rows) == 0
        assert len(report.corrections) == _expected_corrections

    def test_stale_detection_runs(self):
        df = _make_ohlcv_df(60, base_volume=500_000)
        df.iloc[40:47, df.columns.get_loc("volume")] = 0
        validator = DataValidator()
        report = DataQualityReport()
        validator.validate(df, "DEAD", report)
        assert any(c.reason == "stale_data" for c in report.corrections)

    def test_validate_with_fundamentals(self):
        df = _make_ohlcv_df(20, base_price=100.0)
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, -5e9, 1e9], "book_value": [5e9] * 3},
            index=pd.date_range("2024-01-01", periods=3, freq="QS"),
        )
        validator = DataValidator()
        report = DataQualityReport()
        validator.validate(df, "AAPL", report, fundamentals=fundamentals)
        assert any(c.reason == "negative_revenue" for c in report.corrections)

    def test_validate_with_gbp_detection(self):
        df = _make_ohlcv_df(20, base_price=580.0)
        validator = DataValidator()
        report = DataQualityReport()
        validator.validate(df, "BARC.L", report, sector_median_price=5.8)
        assert any(c.reason == "gbp_gbp_mismatch" for c in report.corrections)

    def test_custom_config(self):
        config = ValidationConfig(
            max_pe_ratio=_CUSTOM_CONFIG_PE, stale_consecutive_days=_CUSTOM_STALE_DAYS
        )
        validator = DataValidator(config=config)
        assert validator._config.max_pe_ratio == _CUSTOM_CONFIG_PE  # noqa: SLF001

    def test_report_contains_all_corrections(self):
        df = _make_ohlcv_df(60)
        df.iloc[5, df.columns.get_loc("close")] = -10.0
        df.iloc[40:47, df.columns.get_loc("volume")] = 0
        validator = DataValidator()
        report = DataQualityReport()
        validator.validate(df, "TEST", report)
        reasons = {c.reason for c in report.corrections}
        assert "negative_price" in reasons
        assert "stale_data" in reasons


class TestDataValidatorIntegration:
    def test_validator_in_market_state_builder_pipeline(self):
        df = _make_ohlcv_df(60)
        df.iloc[5, df.columns.get_loc("close")] = -10.0
        builder = MarketStateBuilder(min_bars=10, use_data_validator=True)
        state = builder.build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        for bar in state.ohlcv["AAPL"]:
            assert bar["close"] > 0

    def test_validator_report_accessible_after_build(self):
        df = _make_ohlcv_df(60)
        df.iloc[5, df.columns.get_loc("close")] = -10.0
        builder = MarketStateBuilder(min_bars=10, use_data_validator=True)
        builder.build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        report = builder.last_validation_report()
        assert report is not None
        assert any(c.reason == "negative_price" for c in report.corrections)

    def test_clean_data_no_report_when_no_validator(self):
        df = _make_ohlcv_df(60)
        builder = MarketStateBuilder(min_bars=10)
        builder.build_for_backtest(
            {"AAPL": df},
            df.index[-1],
            ["AAPL"],
        )
        report = builder.last_validation_report()
        assert report is None

    def test_validator_preserves_clean_rows(self):
        df = _make_ohlcv_df(60)
        original_clean_count = len(df[df["close"] > 0]) - 1
        df.iloc[5, df.columns.get_loc("close")] = -10.0
        validator = DataValidator()
        report = DataQualityReport()
        cleaned = validator.validate(df, "AAPL", report)
        assert len(cleaned) == original_clean_count


class TestB1RevenueInterpolationDistinctCorrections:
    def test_multiple_negative_revenues_get_distinct_interpolated_values(self):
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, -2e9, -5e9, 3e9], "book_value": [5e9] * 4},
            index=pd.date_range("2024-01-01", periods=4, freq="QS"),
        )
        rule = RatioSanityRule()
        report = DataQualityReport()
        cleaned = rule.apply_fundamentals("AAPL", report, fundamentals)

        rev_corrections = [c for c in report.corrections if c.reason == "negative_revenue"]
        assert len(rev_corrections) == 2

        corrected_values = {c.original_value: c.corrected_value for c in rev_corrections}
        assert -2e9 in corrected_values
        assert -5e9 in corrected_values
        assert corrected_values[-2e9] != corrected_values[-5e9]

        assert corrected_values[-2e9] == pytest.approx(1e9)
        assert corrected_values[-5e9] == pytest.approx(3e9)

        assert all(cleaned["revenue"].values > 0)


class TestB3GBpGBPRuleDivisionByZero:
    def test_sector_median_zero_returns_df_unchanged(self):
        df = _make_ohlcv_df(20, base_price=580.0)
        rule = GBpGBPRule()
        report = DataQualityReport()
        result = rule.apply(df, "BARC.L", report, sector_median_price=0.0)
        assert len(result) == len(df)
        assert len(report.corrections) == 0

    def test_sector_median_negative_returns_df_unchanged(self):
        df = _make_ohlcv_df(20, base_price=580.0)
        rule = GBpGBPRule()
        report = DataQualityReport()
        result = rule.apply(df, "BARC.L", report, sector_median_price=-5.0)
        assert len(result) == len(df)
        assert len(report.corrections) == 0


class TestM1FlaggedPercentage:
    def test_flagged_percentage_with_corrections(self):
        report = DataQualityReport()
        report.record_symbol("AAPL")
        report.record_symbol("MSFT")
        report.record_symbol("GOOG")
        report.add_correction("AAPL", "close", -1.0, None, "negative_price", "exclude")
        assert report.flagged_percentage == pytest.approx(100.0 / 3.0)

    def test_flagged_percentage_all_flagged(self):
        report = DataQualityReport()
        report.record_symbol("AAPL")
        report.add_correction("AAPL", "close", -1.0, None, "negative_price", "exclude")
        assert report.flagged_percentage == 100.0

    def test_flagged_percentage_no_corrections(self):
        report = DataQualityReport()
        report.record_symbol("AAPL")
        report.record_symbol("MSFT")
        assert report.flagged_percentage == 0.0


class TestM2ApplyFundamentalsReturnValue:
    def test_pe_check_uses_cleaned_fundamentals(self):
        df = _make_ohlcv_df(20, base_price=100.0)
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, -5e9, 1e9], "eps": [0.001, -5.0, 0.001]},
            index=pd.date_range("2024-01-01", periods=3, freq="QS"),
        )
        rule = RatioSanityRule()
        report = DataQualityReport()
        rule.apply(df, "AAPL", report, fundamentals=fundamentals)

        revenue_corrections = [c for c in report.corrections if c.reason == "negative_revenue"]
        assert len(revenue_corrections) == 1


class TestM3FlaggedPercentageUniverseDuplicates:
    def test_duplicate_symbols_in_universe_dont_inflate_denominator(self):
        report = DataQualityReport()
        universe = ["AAPL"] * 20
        report.add_correction("AAPL", "close", -1.0, None, "negative_price", "exclude")
        pct = report.flagged_percentage_for_universe(universe)
        assert pct == 100.0


class TestM4RulesInChain:
    def test_ratio_sanity_rule_in_validator_chain(self):
        df = _make_ohlcv_df(20, base_price=100.0)
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, -5e9, 1e9], "eps": [5.0, 5.0, 5.0]},
            index=pd.date_range("2024-01-01", periods=3, freq="QS"),
        )
        validator = DataValidator()
        report = DataQualityReport()
        validator.validate(df, "AAPL", report, fundamentals=fundamentals)
        assert any(c.reason == "negative_revenue" for c in report.corrections)

    def test_gbp_rule_in_validator_chain(self):
        df = _make_ohlcv_df(20, base_price=580.0)
        validator = DataValidator()
        report = DataQualityReport()
        validator.validate(df, "BARC.L", report, sector_median_price=5.8)
        assert any(c.reason == "gbp_gbp_mismatch" for c in report.corrections)


class TestM5NegativeRevenueTolerance:
    def test_small_negative_revenue_not_flagged_with_tolerance(self):
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, -0.01e9, 1e9], "book_value": [5e9] * 3},
            index=pd.date_range("2024-01-01", periods=3, freq="QS"),
        )
        config = ValidationConfig(negative_revenue_tolerance=0.1)
        rule = RatioSanityRule(config=config)
        report = DataQualityReport()
        cleaned = rule.apply_fundamentals("AAPL", report, fundamentals)
        rev_corrections = [c for c in report.corrections if c.reason == "negative_revenue"]
        assert len(rev_corrections) == 0
        assert cleaned["revenue"].iloc[1] < 0

    def test_large_negative_revenue_still_flagged_with_tolerance(self):
        fundamentals = pd.DataFrame(
            {"revenue": [1e9, -5e9, 1e9], "book_value": [5e9] * 3},
            index=pd.date_range("2024-01-01", periods=3, freq="QS"),
        )
        config = ValidationConfig(negative_revenue_tolerance=0.1)
        rule = RatioSanityRule(config=config)
        report = DataQualityReport()
        rule.apply_fundamentals("AAPL", report, fundamentals)
        rev_corrections = [c for c in report.corrections if c.reason == "negative_revenue"]
        assert len(rev_corrections) == 1
