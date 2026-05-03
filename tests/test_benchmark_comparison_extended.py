"""Extended edge-case tests for engine.core.benchmark_comparison."""

from __future__ import annotations

import math

import pytest

from engine.core.benchmark_comparison import (
    beta,
    capture_ratio,
    correlation,
    down_capture_ratio,
    jensen_alpha,
    up_capture_ratio,
)


class TestBetaEdgeCases:
    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="length mismatch"):
            beta([0.01], [0.01, 0.02])

    def test_empty_returns(self):
        assert beta([], []) == 0.0

    def test_single_return(self):
        assert beta([0.01], [0.01]) == 0.0

    def test_constant_benchmark_zero_variance(self):
        result = beta([0.01, -0.01, 0.02, -0.02], [0.05, 0.05, 0.05, 0.05])
        assert result == 0.0

    def test_perfectly_correlated_beta_one(self):
        returns = [0.01, -0.01, 0.02, -0.02]
        result = beta(returns, returns)
        assert abs(result - 1.0) < 1e-9

    def test_perfect_negative_correlation(self):
        port = [0.01, -0.01, 0.02, -0.02]
        bench = [-0.01, 0.01, -0.02, 0.02]
        result = beta(port, bench)
        assert result < 0


class TestJensenAlphaEdgeCases:
    def test_annualisation_factor_zero_raises(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            jensen_alpha([0.01, 0.02], [0.01, 0.02], annualisation_factor=0)

    def test_annualisation_factor_negative_raises(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            jensen_alpha([0.01, 0.02], [0.01, 0.02], annualisation_factor=-1)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            jensen_alpha([0.01], [0.01, 0.02])

    def test_empty_returns(self):
        assert jensen_alpha([], []) == 0.0

    def test_single_return(self):
        assert jensen_alpha([0.01], [0.01]) == 0.0

    def test_zero_risk_free_rate(self):
        alpha = jensen_alpha([0.01, 0.02], [0.005, 0.01], risk_free_rate=0.0)
        assert math.isfinite(alpha)

    def test_non_zero_risk_free_rate(self):
        alpha = jensen_alpha(
            [0.01, 0.02], [0.005, 0.01], risk_free_rate=0.05, annualisation_factor=252
        )
        assert math.isfinite(alpha)


class TestUpCaptureEdgeCases:
    def test_no_up_bars(self):
        result = up_capture_ratio([-0.01, -0.02], [-0.01, -0.02])
        assert result == 0.0

    def test_all_up_bars(self):
        port = [0.01, 0.02, 0.03]
        bench = [0.01, 0.02, 0.03]
        result = up_capture_ratio(port, bench)
        assert abs(result - 1.0) < 1e-9

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            up_capture_ratio([0.01], [0.01, 0.02])


class TestDownCaptureEdgeCases:
    def test_no_down_bars(self):
        result = down_capture_ratio([0.01, 0.02], [0.01, 0.02])
        assert result == 0.0

    def test_all_down_bars(self):
        port = [-0.01, -0.02, -0.03]
        bench = [-0.01, -0.02, -0.03]
        result = down_capture_ratio(port, bench)
        assert abs(result - 1.0) < 1e-9

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            down_capture_ratio([0.01], [0.01, 0.02])


class TestCaptureRatioEdgeCases:
    def test_down_capture_zero_returns_zero(self):
        result = capture_ratio([0.01, 0.02], [0.01, 0.02])
        assert result == 0.0

    def test_balanced_up_and_down(self):
        port = [0.02, -0.01, 0.03, -0.02]
        bench = [0.01, -0.01, 0.02, -0.02]
        result = capture_ratio(port, bench)
        assert math.isfinite(result)
        assert result > 0


class TestCorrelationEdgeCases:
    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            correlation([0.01], [0.01, 0.02])

    def test_empty_returns(self):
        assert correlation([], []) == 0.0

    def test_single_return(self):
        assert correlation([0.01], [0.01]) == 0.0

    def test_zero_variance_portfolio(self):
        result = correlation([0.05, 0.05, 0.05], [0.01, -0.01, 0.02])
        assert result == 0.0

    def test_zero_variance_benchmark(self):
        result = correlation([0.01, -0.01, 0.02], [0.05, 0.05, 0.05])
        assert result == 0.0

    def test_perfect_correlation(self):
        returns = [0.01, -0.01, 0.02, -0.02]
        result = correlation(returns, returns)
        assert abs(result - 1.0) < 1e-9

    def test_perfect_negative_correlation(self):
        port = [0.01, -0.01, 0.02, -0.02]
        bench = [-0.01, 0.01, -0.02, 0.02]
        result = correlation(port, bench)
        assert abs(result + 1.0) < 1e-9
