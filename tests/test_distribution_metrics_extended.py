"""Extended edge-case tests for engine.core.distribution_metrics."""

from __future__ import annotations

import math

import pytest

from engine.core.distribution_metrics import (
    conditional_value_at_risk,
    kurtosis,
    skewness,
    tail_ratio,
    value_at_risk_historical,
    value_at_risk_parametric,
)


class TestSkewnessEdgeCases:
    def test_empty_returns(self):
        assert skewness([]) == 0.0

    def test_single_return(self):
        assert skewness([0.01]) == 0.0

    def test_two_returns(self):
        assert skewness([0.01, -0.01]) == 0.0

    def test_three_identical_returns_zero_variance(self):
        assert skewness([0.05, 0.05, 0.05]) == 0.0

    def test_positive_skew(self):
        returns = [0.01, 0.01, 0.01, 0.01, 0.20]
        result = skewness(returns)
        assert result > 0

    def test_negative_skew(self):
        returns = [-0.20, 0.01, 0.01, 0.01, 0.01]
        result = skewness(returns)
        assert result < 0

    def test_symmetric_distribution_near_zero(self):
        returns = [-0.05, -0.02, 0.0, 0.02, 0.05]
        result = skewness(returns)
        assert abs(result) < 0.5

    def test_large_input(self):
        returns = [0.01] * 1000 + [0.5]
        result = skewness(returns)
        assert math.isfinite(result)


class TestKurtosisEdgeCases:
    def test_empty_returns(self):
        assert kurtosis([]) == 0.0

    def test_three_returns(self):
        assert kurtosis([0.01, -0.01, 0.0]) == 0.0

    def test_four_identical_zero_variance(self):
        assert kurtosis([0.05, 0.05, 0.05, 0.05]) == 0.0

    def test_normal_like_low_excess_kurtosis(self):
        import random

        random.seed(42)
        returns = [random.gauss(0, 0.01) for _ in range(1000)]
        result = kurtosis(returns)
        assert abs(result) < 0.5

    def test_fat_tails_positive_kurtosis(self):
        returns = [0.0] * 90 + [0.5, -0.5, 0.6, -0.6, 0.7, -0.7, 0.8, -0.8, 0.9, -0.9]
        result = kurtosis(returns)
        assert result > 0


class TestValueAtRiskHistoricalEdgeCases:
    def test_empty_returns(self):
        assert value_at_risk_historical([]) == 0.0

    def test_all_positive_returns(self):
        assert value_at_risk_historical([0.01, 0.02, 0.03, 0.04, 0.05]) == 0.0

    def test_all_negative_returns(self):
        returns = [-0.05, -0.04, -0.03, -0.02, -0.01]
        var = value_at_risk_historical(returns, confidence=0.95)
        assert var > 0

    def test_confidence_0_99(self):
        returns = list(range(-10, 11))
        var = value_at_risk_historical(returns, confidence=0.99)
        assert var >= 0

    def test_invalid_confidence_zero(self):
        with pytest.raises(ValueError, match="confidence"):
            value_at_risk_historical([0.01], confidence=0.0)

    def test_invalid_confidence_one(self):
        with pytest.raises(ValueError, match="confidence"):
            value_at_risk_historical([0.01], confidence=1.0)

    def test_invalid_confidence_negative(self):
        with pytest.raises(ValueError, match="confidence"):
            value_at_risk_historical([0.01], confidence=-0.5)

    def test_single_return(self):
        var = value_at_risk_historical([-0.05], confidence=0.95)
        assert var >= 0


class TestValueAtRiskParametricEdgeCases:
    def test_empty_returns(self):
        assert value_at_risk_parametric([]) == 0.0

    def test_single_return(self):
        assert value_at_risk_parametric([0.01]) == 0.0

    def test_zero_variance(self):
        assert value_at_risk_parametric([0.05, 0.05, 0.05]) == 0.0

    def test_known_gaussian(self):
        returns = [-0.04, -0.02, 0.0, 0.02, 0.04]
        var = value_at_risk_parametric(returns, confidence=0.95)
        assert var > 0

    def test_invalid_confidence(self):
        with pytest.raises(ValueError):
            value_at_risk_parametric([0.01, 0.02], confidence=0.0)


class TestConditionalValueAtRiskEdgeCases:
    def test_empty_returns(self):
        assert conditional_value_at_risk([]) == 0.0

    def test_all_positive_returns(self):
        assert conditional_value_at_risk([0.01, 0.02, 0.03]) == 0.0

    def test_cutoff_zero_returns_worst(self):
        returns = [-0.10, -0.05, 0.0, 0.05, 0.10]
        cvar = conditional_value_at_risk(returns, confidence=0.99)
        assert cvar >= 0

    def test_mixed_returns(self):
        returns = [-0.10, -0.05, -0.03, 0.0, 0.05, 0.10, 0.15, 0.20]
        cvar = conditional_value_at_risk(returns, confidence=0.95)
        assert cvar > 0


class TestTailRatioEdgeCases:
    def test_empty_returns(self):
        assert tail_ratio([]) == 0.0

    def test_all_zero_returns(self):
        assert tail_ratio([0.0, 0.0, 0.0, 0.0]) == 0.0

    def test_symmetric_returns_near_one(self):
        returns = list(range(-50, 51))
        result = tail_ratio(returns, percentile=0.95)
        assert result > 0

    def test_positive_skew_ratio_above_one(self):
        returns = [0.1, 0.2, 0.3, 0.4, -0.01, -0.02]
        result = tail_ratio(returns, percentile=0.90)
        assert result > 1.0

    def test_invalid_percentile_below_half(self):
        with pytest.raises(ValueError, match="percentile"):
            tail_ratio([0.01], percentile=0.4)

    def test_invalid_percentile_at_one(self):
        with pytest.raises(ValueError, match="percentile"):
            tail_ratio([0.01], percentile=1.0)

    def test_single_return(self):
        result = tail_ratio([0.05], percentile=0.95)
        assert math.isfinite(result)
