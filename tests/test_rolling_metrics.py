"""Tests for rolling-window time-series metrics (gh#97 follow-up)."""

from __future__ import annotations

import math

import pytest

from engine.core.rolling_metrics import (
    DEFAULT_ANNUALISATION,
    rolling_mean,
    rolling_return,
    rolling_sharpe,
    rolling_sortino,
    rolling_volatility,
)


# ---------------------------------------------------------------------------
# rolling_mean
# ---------------------------------------------------------------------------


class TestRollingMean:
    def test_empty_returns_empty(self):
        assert rolling_mean([], 3) == []

    def test_window_too_large_all_none(self):
        assert rolling_mean([0.01, 0.02], 5) == [None, None]

    def test_first_window_minus_one_indices_none(self):
        out = rolling_mean([0.01, 0.02, 0.03, 0.04], 3)
        assert out[0] is None
        assert out[1] is None
        assert out[2] is not None

    def test_known_values(self):
        # Window=3 mean.
        out = rolling_mean([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        assert out == [
            None,
            None,
            pytest.approx(2.0),
            pytest.approx(3.0),
            pytest.approx(4.0),
        ]

    def test_window_one_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_mean([0.01, 0.02], 1)

    def test_zero_window_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_mean([0.01], 0)

    def test_output_length_matches_input(self):
        for n in (5, 10, 50):
            out = rolling_mean([0.01] * n, 3)
            assert len(out) == n


# ---------------------------------------------------------------------------
# rolling_volatility
# ---------------------------------------------------------------------------


class TestRollingVolatility:
    def test_empty_returns_empty(self):
        assert rolling_volatility([], 3) == []

    def test_first_indices_none(self):
        out = rolling_volatility([0.01, 0.02, 0.03], 3)
        assert out[0] is None
        assert out[1] is None
        assert out[2] is not None

    def test_constant_returns_zero_volatility(self):
        out = rolling_volatility([0.01] * 10, 5)
        for v in out[4:]:
            assert v == pytest.approx(0.0, abs=1e-12)

    def test_increasing_variance_increases_vol(self):
        # Calm window then volatile window.
        returns = [0.01, 0.01, 0.01, 0.01, 0.01, 0.05, -0.05, 0.05, -0.05, 0.05]
        out = rolling_volatility(returns, 5, annualisation_factor=252)
        # Latest window has high variance; earlier full-window has zero.
        assert out[4] == pytest.approx(0.0, abs=1e-12)
        assert out[-1] is not None
        assert out[-1] > out[4]

    def test_default_annualisation(self):
        # Default is 252 — multiplier is sqrt(252).
        returns = [0.01, -0.01, 0.01, -0.01, 0.01]
        out_default = rolling_volatility(returns, 5)
        out_custom = rolling_volatility(returns, 5, annualisation_factor=252)
        assert out_default[-1] == out_custom[-1]
        assert DEFAULT_ANNUALISATION == 252

    def test_zero_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            rolling_volatility([0.01, 0.02, 0.03], 3, annualisation_factor=0)

    def test_negative_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            rolling_volatility([0.01, 0.02, 0.03], 3, annualisation_factor=-1)


# ---------------------------------------------------------------------------
# rolling_sharpe
# ---------------------------------------------------------------------------


class TestRollingSharpe:
    def test_empty_returns_empty(self):
        assert rolling_sharpe([], 3) == []

    def test_first_indices_none(self):
        out = rolling_sharpe([0.01, 0.02, 0.03], 3)
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_constant_returns_zero_sharpe(self):
        # Zero std → zero Sharpe.
        out = rolling_sharpe([0.01] * 5, 5)
        assert out[-1] == 0.0

    def test_positive_drift_positive_sharpe(self):
        out = rolling_sharpe([0.01, 0.02, 0.015, 0.018, 0.012], 5)
        assert out[-1] > 0

    def test_negative_drift_negative_sharpe(self):
        out = rolling_sharpe([-0.01, -0.02, -0.015, -0.018, -0.012], 5)
        assert out[-1] < 0

    def test_constant_returns_with_high_rf_still_zero(self):
        # Constant returns → std==0 → 0.0 short-circuit (regardless of rf).
        returns = [0.0001] * 5
        out = rolling_sharpe(returns, 5, risk_free_rate=1.0)
        assert out[-1] == 0.0

    def test_rf_with_variance_negative_sharpe(self):
        # Variance > 0, rf > mean → negative excess Sharpe.
        returns = [0.0, 0.0001, 0.0, 0.0001, 0.0]
        out = rolling_sharpe(returns, 5, risk_free_rate=1.0)
        assert out[-1] < 0


# ---------------------------------------------------------------------------
# rolling_sortino
# ---------------------------------------------------------------------------


class TestRollingSortino:
    def test_empty_returns_empty(self):
        assert rolling_sortino([], 3) == []

    def test_first_indices_none(self):
        out = rolling_sortino([0.01, 0.02, 0.03], 3)
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_no_negatives_zero_downside_returns_zero(self):
        # All positive returns with rf=0 → no downside dev → 0.0 short-circuit.
        out = rolling_sortino([0.01, 0.02, 0.03, 0.04, 0.05], 5)
        assert out[-1] == 0.0

    def test_mixed_returns_finite_sortino(self):
        out = rolling_sortino(
            [0.02, -0.01, 0.03, -0.02, 0.04, 0.01, -0.01], 5
        )
        assert out[-1] is not None
        assert math.isfinite(out[-1])

    def test_negative_drift_negative_sortino(self):
        out = rolling_sortino([-0.01, -0.02, -0.015, -0.018, -0.012], 5)
        assert out[-1] < 0


# ---------------------------------------------------------------------------
# rolling_return
# ---------------------------------------------------------------------------


class TestRollingReturn:
    def test_empty_returns_empty(self):
        assert rolling_return([], 3) == []

    def test_first_indices_none(self):
        out = rolling_return([0.01, 0.02, 0.03], 3)
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_compounding(self):
        # +1 % three times → (1.01)^3 - 1 ≈ 0.030301.
        out = rolling_return([0.01, 0.01, 0.01], 3)
        assert out[-1] == pytest.approx(0.030301, rel=1e-9)

    def test_loss_then_recovery_compounds(self):
        # -10% then +10% → (0.9 × 1.1) - 1 = -0.01.
        out = rolling_return([-0.10, 0.10], 2)
        assert out[-1] == pytest.approx(-0.01, rel=1e-9)

    def test_window_one_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_return([0.01, 0.02], 1)

    def test_output_length_matches_input(self):
        out = rolling_return([0.01, 0.02, 0.03, 0.04, 0.05], 3)
        assert len(out) == 5
