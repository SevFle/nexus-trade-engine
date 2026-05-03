"""Tests for time-based return analytics (gh#97 follow-up)."""

from __future__ import annotations

from datetime import date

import pytest

from engine.core.time_metrics import (
    aggregate_returns_by_month,
    aggregate_returns_by_week,
    compute_best_period,
    compute_negative_period_pct,
    compute_positive_period_pct,
    compute_worst_period,
)


# ---------------------------------------------------------------------------
# Best / worst
# ---------------------------------------------------------------------------


class TestBestWorst:
    def test_empty_inputs_return_zero(self):
        assert compute_best_period([]) == 0.0
        assert compute_worst_period([]) == 0.0

    def test_known_values(self):
        returns = [0.01, 0.02, -0.01, 0.03, -0.04]
        assert compute_best_period(returns) == 0.03
        assert compute_worst_period(returns) == -0.04

    def test_all_positive(self):
        returns = [0.01, 0.02, 0.03]
        assert compute_best_period(returns) == 0.03
        assert compute_worst_period(returns) == 0.01

    def test_all_negative(self):
        returns = [-0.01, -0.02, -0.03]
        assert compute_best_period(returns) == -0.01
        assert compute_worst_period(returns) == -0.03


# ---------------------------------------------------------------------------
# Positive / negative period %
# ---------------------------------------------------------------------------


class TestPositivePeriodPct:
    def test_empty_returns_zero(self):
        assert compute_positive_period_pct([]) == 0.0

    def test_known_value(self):
        # 3 of 5 positive → 60 %.
        returns = [0.01, 0.02, -0.01, 0.03, -0.04]
        assert compute_positive_period_pct(returns) == pytest.approx(0.6)

    def test_zero_returns_not_counted(self):
        # Zero returns are NOT positive — convention from major analytics platforms.
        returns = [0.0, 0.01, 0.0]
        assert compute_positive_period_pct(returns) == pytest.approx(1 / 3)

    def test_all_negative(self):
        assert compute_positive_period_pct([-0.01, -0.02]) == 0.0


class TestNegativePeriodPct:
    def test_empty_returns_zero(self):
        assert compute_negative_period_pct([]) == 0.0

    def test_known_value(self):
        # 2 of 5 negative → 40 %.
        returns = [0.01, 0.02, -0.01, 0.03, -0.04]
        assert compute_negative_period_pct(returns) == pytest.approx(0.4)

    def test_zero_returns_not_counted(self):
        returns = [0.0, -0.01, 0.0]
        assert compute_negative_period_pct(returns) == pytest.approx(1 / 3)

    def test_pos_plus_neg_does_not_have_to_sum_to_one(self):
        # When zeros are present, pos + neg < 1.
        returns = [0.0, 0.01, -0.01]
        pos = compute_positive_period_pct(returns)
        neg = compute_negative_period_pct(returns)
        assert pos + neg == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# Monthly aggregation
# ---------------------------------------------------------------------------


class TestMonthlyAggregation:
    def test_empty_returns_empty(self):
        assert aggregate_returns_by_month([]) == []

    def test_single_month_compounds(self):
        # +1 % three days in a row → (1.01)^3 - 1 ≈ 3.0301 %.
        dated = [
            (date(2024, 6, 3), 0.01),
            (date(2024, 6, 4), 0.01),
            (date(2024, 6, 5), 0.01),
        ]
        out = aggregate_returns_by_month(dated)
        assert len(out) == 1
        key, ret = out[0]
        assert key == "2024-06"
        assert ret == pytest.approx(0.030301, rel=1e-6)

    def test_two_months_chronological_order(self):
        dated = [
            (date(2024, 7, 1), 0.02),  # July
            (date(2024, 6, 1), 0.01),  # June
        ]
        out = aggregate_returns_by_month(dated)
        # Sorted internally — June first, July second.
        assert [k for k, _ in out] == ["2024-06", "2024-07"]
        assert out[0][1] == pytest.approx(0.01)
        assert out[1][1] == pytest.approx(0.02)

    def test_loss_then_recovery_compounds(self):
        # -10 % then +10 % → (0.9 * 1.1) - 1 = -0.01 (1 % loss).
        dated = [
            (date(2024, 1, 5), -0.10),
            (date(2024, 1, 25), 0.10),
        ]
        out = aggregate_returns_by_month(dated)
        assert len(out) == 1
        assert out[0][1] == pytest.approx(-0.01, rel=1e-9)

    def test_year_boundary_handled(self):
        dated = [
            (date(2023, 12, 31), 0.01),
            (date(2024, 1, 1), 0.02),
        ]
        out = aggregate_returns_by_month(dated)
        assert [k for k, _ in out] == ["2023-12", "2024-01"]


# ---------------------------------------------------------------------------
# Weekly aggregation
# ---------------------------------------------------------------------------


class TestWeeklyAggregation:
    def test_empty_returns_empty(self):
        assert aggregate_returns_by_week([]) == []

    def test_single_week_compounds(self):
        # 5 trading days within ISO week 23 of 2024.
        # Week 23 starts Mon 2024-06-03.
        dated = [
            (date(2024, 6, 3), 0.01),
            (date(2024, 6, 4), 0.01),
            (date(2024, 6, 5), 0.01),
            (date(2024, 6, 6), 0.01),
            (date(2024, 6, 7), 0.01),
        ]
        out = aggregate_returns_by_week(dated)
        assert len(out) == 1
        key, ret = out[0]
        assert key == "2024-W23"
        assert ret == pytest.approx(0.05101005, rel=1e-6)

    def test_two_weeks_chronological_order(self):
        dated = [
            (date(2024, 6, 10), 0.02),  # week 24
            (date(2024, 6, 3), 0.01),  # week 23
        ]
        out = aggregate_returns_by_week(dated)
        assert [k for k, _ in out] == ["2024-W23", "2024-W24"]

    def test_iso_year_boundary(self):
        # Jan 1 2024 falls on Monday, ISO week 1 of 2024.
        # Dec 31 2023 (Sunday) is ISO week 52 of 2023.
        dated = [
            (date(2023, 12, 31), 0.01),
            (date(2024, 1, 1), 0.02),
        ]
        out = aggregate_returns_by_week(dated)
        keys = [k for k, _ in out]
        assert "2023-W52" in keys
        assert "2024-W01" in keys
