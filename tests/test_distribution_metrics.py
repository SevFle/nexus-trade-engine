"""Tests for return-distribution + tail-risk metrics (gh#97 follow-up)."""

from __future__ import annotations

import math

import pytest

from engine.core.distribution_metrics import (
    _inverse_normal_cdf,
    conditional_value_at_risk,
    kurtosis,
    skewness,
    tail_ratio,
    value_at_risk_historical,
    value_at_risk_parametric,
)

# ---------------------------------------------------------------------------
# skewness
# ---------------------------------------------------------------------------


class TestSkewness:
    def test_empty_returns_zero(self):
        assert skewness([]) == 0.0

    def test_too_few_points_returns_zero(self):
        assert skewness([0.01, 0.02]) == 0.0

    def test_zero_variance_returns_zero(self):
        assert skewness([0.01, 0.01, 0.01, 0.01]) == 0.0

    def test_symmetric_distribution_near_zero(self):
        # Symmetric around 0 → skew ≈ 0.
        out = skewness([-2.0, -1.0, 0.0, 1.0, 2.0])
        assert abs(out) < 1e-9

    def test_positive_skew_long_right_tail(self):
        # Long right tail → positive skew.
        out = skewness([-1.0, -1.0, -1.0, 0.0, 0.0, 5.0])
        assert out > 0

    def test_negative_skew_long_left_tail(self):
        out = skewness([-5.0, 0.0, 0.0, 1.0, 1.0, 1.0])
        assert out < 0


# ---------------------------------------------------------------------------
# kurtosis
# ---------------------------------------------------------------------------


class TestKurtosis:
    def test_empty_returns_zero(self):
        assert kurtosis([]) == 0.0

    def test_too_few_points_returns_zero(self):
        assert kurtosis([0.01, 0.02, 0.03]) == 0.0

    def test_zero_variance_returns_zero(self):
        assert kurtosis([0.01, 0.01, 0.01, 0.01]) == 0.0

    def test_uniform_distribution_negative_excess(self):
        # Uniform has excess kurtosis around -1.2.
        out = kurtosis([-2.0, -1.0, 0.0, 1.0, 2.0, -2.0, -1.0, 0.0, 1.0, 2.0])
        assert out < 0

    def test_fat_tails_positive_excess(self):
        # Long-tailed series with heavy outliers → positive excess kurtosis.
        out = kurtosis(
            [
                0.0, 0.0, 0.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 0.0, 0.0,
                10.0, -10.0,
            ]
        )
        assert out > 0


# ---------------------------------------------------------------------------
# VaR — historical
# ---------------------------------------------------------------------------


class TestVaRHistorical:
    def test_empty_returns_zero(self):
        assert value_at_risk_historical([]) == 0.0

    def test_all_positive_returns_zero(self):
        assert value_at_risk_historical([0.01, 0.02, 0.03, 0.04, 0.05]) == 0.0

    def test_known_5pct_quantile(self):
        # Mostly losses; 5th-percentile is a real loss → positive magnitude.
        returns = [-i / 100 for i in range(100)]
        out = value_at_risk_historical(returns, confidence=0.95)
        assert out > 0

    def test_higher_confidence_means_bigger_var(self):
        returns = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05]
        var_95 = value_at_risk_historical(returns, confidence=0.95)
        var_99 = value_at_risk_historical(returns, confidence=0.99)
        assert var_99 >= var_95

    def test_invalid_confidence_zero(self):
        with pytest.raises(ValueError, match="confidence"):
            value_at_risk_historical([0.01, 0.02], confidence=0.0)

    def test_invalid_confidence_one(self):
        with pytest.raises(ValueError, match="confidence"):
            value_at_risk_historical([0.01, 0.02], confidence=1.0)


# ---------------------------------------------------------------------------
# VaR — parametric
# ---------------------------------------------------------------------------


class TestVaRParametric:
    def test_empty_returns_zero(self):
        assert value_at_risk_parametric([]) == 0.0

    def test_too_few_points_returns_zero(self):
        assert value_at_risk_parametric([0.01]) == 0.0

    def test_zero_variance_returns_zero(self):
        assert value_at_risk_parametric([0.01, 0.01, 0.01]) == 0.0

    def test_normal_returns_positive_var(self):
        # Mean-zero modest variance → 95 % VaR > 0.
        returns = [0.01, -0.01, 0.02, -0.02, 0.005, -0.005, 0.015, -0.015]
        out = value_at_risk_parametric(returns, confidence=0.95)
        assert out > 0

    def test_higher_confidence_means_bigger_var(self):
        returns = [0.01, -0.01, 0.02, -0.02, 0.005, -0.005, 0.015, -0.015]
        var_95 = value_at_risk_parametric(returns, confidence=0.95)
        var_99 = value_at_risk_parametric(returns, confidence=0.99)
        assert var_99 > var_95

    def test_invalid_confidence_rejected(self):
        with pytest.raises(ValueError, match="confidence"):
            value_at_risk_parametric([0.01, 0.02, 0.03], confidence=-0.5)


# ---------------------------------------------------------------------------
# Conditional VaR / ES
# ---------------------------------------------------------------------------


class TestCVaR:
    def test_empty_returns_zero(self):
        assert conditional_value_at_risk([]) == 0.0

    def test_all_positive_returns_zero(self):
        assert (
            conditional_value_at_risk([0.01, 0.02, 0.03, 0.04, 0.05]) == 0.0
        )

    def test_cvar_geq_var(self):
        # CVaR (mean of tail) is always ≥ VaR (boundary of tail).
        returns = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05]
        var = value_at_risk_historical(returns, confidence=0.9)
        cvar = conditional_value_at_risk(returns, confidence=0.9)
        assert cvar >= var

    def test_known_value(self):
        # 10 returns. 90 % CVaR = mean of worst 10 % = -0.05 → magnitude 0.05.
        returns = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05]
        out = conditional_value_at_risk(returns, confidence=0.9)
        assert out == pytest.approx(0.05)

    def test_invalid_confidence_rejected(self):
        with pytest.raises(ValueError, match="confidence"):
            conditional_value_at_risk([0.01, 0.02], confidence=2.0)


# ---------------------------------------------------------------------------
# tail_ratio
# ---------------------------------------------------------------------------


class TestTailRatio:
    def test_empty_returns_zero(self):
        assert tail_ratio([]) == 0.0

    def test_symmetric_returns_one(self):
        # Symmetric distribution → tails equal → ratio = 1.
        out = tail_ratio([-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0], percentile=0.85)
        assert out == pytest.approx(1.0)

    def test_positive_skew_ratio_above_one(self):
        # Big right tail.
        out = tail_ratio(
            [-1.0, -1.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 5.0, 5.0],
            percentile=0.9,
        )
        assert out > 1.0

    def test_negative_skew_ratio_below_one(self):
        out = tail_ratio(
            [-5.0, -5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
            percentile=0.9,
        )
        assert out < 1.0

    def test_lower_tail_zero_returns_zero(self):
        # All non-negative — lower-tail magnitude 0 → ratio 0.
        out = tail_ratio([0.0, 0.0, 0.0, 1.0, 2.0], percentile=0.9)
        assert out == 0.0

    def test_invalid_percentile_rejected(self):
        with pytest.raises(ValueError, match="percentile"):
            tail_ratio([0.01, 0.02, 0.03], percentile=0.5)
        with pytest.raises(ValueError, match="percentile"):
            tail_ratio([0.01, 0.02, 0.03], percentile=1.0)


# ---------------------------------------------------------------------------
# inverse normal CDF (helper used by parametric VaR)
# ---------------------------------------------------------------------------


class TestInverseNormalCDF:
    def test_median_is_zero(self):
        assert _inverse_normal_cdf(0.5) == pytest.approx(0.0, abs=1e-9)

    def test_one_sigma_lower(self):
        # Φ⁻¹(0.1587) ≈ -1.0
        out = _inverse_normal_cdf(0.1587)
        assert out == pytest.approx(-1.0, abs=1e-3)

    def test_one_sigma_upper(self):
        out = _inverse_normal_cdf(0.8413)
        assert out == pytest.approx(1.0, abs=1e-3)

    def test_95pct_quantile(self):
        # Φ⁻¹(0.95) ≈ 1.6449
        assert _inverse_normal_cdf(0.95) == pytest.approx(1.6449, abs=1e-3)

    def test_invalid_p_rejected(self):
        with pytest.raises(ValueError, match="p must be"):
            _inverse_normal_cdf(0.0)
        with pytest.raises(ValueError, match="p must be"):
            _inverse_normal_cdf(1.0)

    def test_finite_at_extremes(self):
        # Tail values must remain finite.
        assert math.isfinite(_inverse_normal_cdf(0.001))
        assert math.isfinite(_inverse_normal_cdf(0.999))
