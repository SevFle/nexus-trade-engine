"""Tests for the additional risk-adjusted metrics (gh#97 follow-up)."""

from __future__ import annotations

import math

import pytest

from engine.core.metrics_extras import (
    compute_gain_to_pain_ratio,
    compute_information_ratio,
    compute_omega_ratio,
    compute_pain_index,
    compute_recovery_factor,
    compute_ulcer_index,
)

# ---------------------------------------------------------------------------
# Omega ratio
# ---------------------------------------------------------------------------


class TestOmega:
    def test_empty_returns_zero(self):
        assert compute_omega_ratio([]) == 0.0

    def test_balanced_distribution_returns_one(self):
        # +1 and -1 around zero threshold → equal up/down mass.
        assert compute_omega_ratio([1.0, -1.0]) == pytest.approx(1.0)

    def test_above_threshold_returns_above_one(self):
        # Mostly gains relative to threshold of zero.
        out = compute_omega_ratio([0.5, 0.5, -0.1])
        assert out == pytest.approx(10.0)

    def test_threshold_shifts_partition(self):
        # Threshold raises the bar — same returns may flip from gain
        # to loss bucket.
        returns = [0.05, -0.02, 0.01]
        assert compute_omega_ratio(returns, threshold=0.04) > 0
        assert compute_omega_ratio(returns, threshold=0.5) < 1

    def test_no_downside_returns_inf(self):
        assert compute_omega_ratio([0.1, 0.2, 0.3]) == math.inf

    def test_all_at_threshold_returns_zero(self):
        assert compute_omega_ratio([0.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# Information ratio
# ---------------------------------------------------------------------------


class TestInformation:
    def test_mismatched_lengths_returns_zero(self):
        assert compute_information_ratio([1.0, 2.0], [1.0]) == 0.0

    def test_too_few_points_returns_zero(self):
        assert compute_information_ratio([1.0], [1.0]) == 0.0

    def test_zero_active_returns_yields_zero(self):
        # No tracking error.
        assert compute_information_ratio(
            [0.01, 0.01, 0.01], [0.01, 0.01, 0.01]
        ) == 0.0

    def test_consistent_outperformance_yields_positive(self):
        # Slightly noisy outperformance — produces a positive IR with
        # meaningful tracking-error denominator.
        returns = [0.020, 0.022, 0.018, 0.021]
        benchmark = [0.010, 0.010, 0.010, 0.010]
        ir = compute_information_ratio(returns, benchmark)
        assert ir > 0


# ---------------------------------------------------------------------------
# Gain-to-pain
# ---------------------------------------------------------------------------


class TestGainToPain:
    def test_empty_returns_zero(self):
        assert compute_gain_to_pain_ratio([]) == 0.0

    def test_known_value(self):
        # +5 and -2 → sum=3, pain=2 → ratio=1.5
        assert compute_gain_to_pain_ratio([5.0, -2.0]) == 1.5

    def test_no_losses_returns_inf(self):
        assert compute_gain_to_pain_ratio([1.0, 2.0]) == math.inf

    def test_all_losses_returns_negative(self):
        # -1 and -2 → sum=-3, pain=3 → ratio=-1
        assert compute_gain_to_pain_ratio([-1.0, -2.0]) == -1.0


# ---------------------------------------------------------------------------
# Ulcer / pain index
# ---------------------------------------------------------------------------


class TestUlcerIndex:
    def test_empty_returns_zero(self):
        assert compute_ulcer_index([]) == 0.0

    def test_flat_curve_returns_zero(self):
        # No drawdowns at all.
        assert compute_ulcer_index([100.0, 100.0, 100.0]) == 0.0

    def test_monotonic_decline_yields_positive(self):
        # 100 → 90 → 80 → 70: drawdowns 0 / 10 / 20 / 30 percent.
        # RMS = sqrt((0+100+400+900)/4) = sqrt(350) ≈ 18.71
        out = compute_ulcer_index([100.0, 90.0, 80.0, 70.0])
        assert out == pytest.approx(math.sqrt(350.0))

    def test_recovery_curve_keeps_history_in_rms(self):
        # 100 → 80 → 100 — temporary 20 % drawdown.
        # RMS = sqrt((0+400+0)/3) ≈ 11.55
        out = compute_ulcer_index([100.0, 80.0, 100.0])
        assert out == pytest.approx(math.sqrt(400.0 / 3))


class TestPainIndex:
    def test_empty_returns_zero(self):
        assert compute_pain_index([]) == 0.0

    def test_known_value(self):
        # Drawdowns 0.0, 0.05, 0.10 → mean 0.05 → 5.0 percent.
        assert compute_pain_index([0.0, 0.05, 0.10]) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Recovery factor
# ---------------------------------------------------------------------------


class TestRecoveryFactor:
    def test_zero_drawdown_returns_zero(self):
        # Metric undefined when there is no drawdown.
        assert compute_recovery_factor(50.0, 0.0) == 0.0

    def test_negative_drawdown_returns_zero(self):
        # Defensive: nonsensical input clamps to 0.
        assert compute_recovery_factor(50.0, -5.0) == 0.0

    def test_known_value(self):
        # 30 % return / 10 % drawdown → 3.0.
        assert compute_recovery_factor(30.0, 10.0) == 3.0

    def test_loss_year_returns_negative(self):
        assert compute_recovery_factor(-5.0, 10.0) == -0.5
