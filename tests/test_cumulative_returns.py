"""Tests for cumulative returns + return-stream comparison (gh#97 follow-up)."""

from __future__ import annotations

import math

import pytest

from engine.core.cumulative_returns import (
    active_returns,
    beating_benchmark_pct,
    cumulative_returns,
    equity_curve_from_returns,
    log_returns,
    returns_from_equity,
    tracking_error,
)

# ---------------------------------------------------------------------------
# cumulative_returns
# ---------------------------------------------------------------------------


class TestCumulativeReturns:
    def test_empty_returns_empty(self):
        assert cumulative_returns([]) == []

    def test_first_bar_equals_first_return(self):
        out = cumulative_returns([0.05, 0.10, -0.02])
        assert out[0] == pytest.approx(0.05)

    def test_compounding_known_value(self):
        # Three +1 % bars → (1.01)^3 - 1 ≈ 0.030301.
        out = cumulative_returns([0.01, 0.01, 0.01])
        assert out[-1] == pytest.approx(0.030301, rel=1e-9)

    def test_loss_then_recovery(self):
        # -10% then +10% → -0.01.
        out = cumulative_returns([-0.10, 0.10])
        assert out[-1] == pytest.approx(-0.01, rel=1e-9)

    def test_output_length_matches_input(self):
        out = cumulative_returns([0.01, 0.02, 0.03, 0.04, 0.05])
        assert len(out) == 5


# ---------------------------------------------------------------------------
# equity_curve_from_returns
# ---------------------------------------------------------------------------


class TestEquityCurveFromReturns:
    def test_empty_returns_initial_only(self):
        assert equity_curve_from_returns([]) == [1.0]

    def test_default_initial_one(self):
        out = equity_curve_from_returns([0.10])
        assert out == [1.0, pytest.approx(1.10)]

    def test_custom_initial(self):
        out = equity_curve_from_returns([0.10, -0.05], initial_value=100.0)
        # 100 → 110 → 104.5
        assert out[0] == 100.0
        assert out[1] == pytest.approx(110.0)
        assert out[2] == pytest.approx(104.5)

    def test_zero_initial_rejected(self):
        with pytest.raises(ValueError, match="initial_value"):
            equity_curve_from_returns([0.10], initial_value=0.0)

    def test_negative_initial_rejected(self):
        with pytest.raises(ValueError, match="initial_value"):
            equity_curve_from_returns([0.10], initial_value=-100.0)

    def test_length_is_returns_plus_one(self):
        out = equity_curve_from_returns([0.01] * 5)
        assert len(out) == 6


# ---------------------------------------------------------------------------
# log_returns
# ---------------------------------------------------------------------------


class TestLogReturns:
    def test_empty_returns_empty(self):
        assert log_returns([]) == []

    def test_zero_input_zero_output(self):
        out = log_returns([0.0, 0.0, 0.0])
        for v in out:
            assert v == pytest.approx(0.0)

    def test_known_value(self):
        # ln(1.10) ≈ 0.09531.
        out = log_returns([0.10])
        assert out[0] == pytest.approx(math.log(1.10))

    def test_minus_one_rejected(self):
        with pytest.raises(ValueError, match="return must be"):
            log_returns([-1.0])

    def test_below_minus_one_rejected(self):
        with pytest.raises(ValueError, match="return must be"):
            log_returns([0.05, -1.5])

    def test_log_returns_sum_to_log_of_total(self):
        # Identity: sum(log_returns) == ln(1 + total_compounded_return).
        simple = [0.01, -0.02, 0.03, -0.01]
        logs = log_returns(simple)
        compounded = 1.0
        for r in simple:
            compounded *= 1.0 + r
        assert sum(logs) == pytest.approx(math.log(compounded), rel=1e-12)


# ---------------------------------------------------------------------------
# returns_from_equity
# ---------------------------------------------------------------------------


class TestReturnsFromEquity:
    def test_empty_returns_empty(self):
        assert returns_from_equity([]) == []

    def test_single_point_returns_empty(self):
        assert returns_from_equity([100.0]) == []

    def test_known_values(self):
        # 100 → 110 → 104.5 → returns 0.10, -0.05.
        out = returns_from_equity([100.0, 110.0, 104.5])
        assert out == [pytest.approx(0.10), pytest.approx(-0.05)]

    def test_length_is_equity_minus_one(self):
        out = returns_from_equity([100.0, 105.0, 110.0, 115.0])
        assert len(out) == 3

    def test_zero_previous_yields_zero(self):
        # Stub-safe: zero previous bar yields 0.0 instead of divide error.
        out = returns_from_equity([0.0, 100.0, 110.0])
        assert out[0] == 0.0
        assert out[1] == pytest.approx(0.10)

    def test_negative_previous_yields_zero(self):
        # Defensive — should never happen in real data but must not crash.
        out = returns_from_equity([-100.0, 100.0])
        assert out[0] == 0.0

    def test_round_trip_with_equity_curve(self):
        # Inverse of equity_curve_from_returns.
        original = [0.01, -0.02, 0.03, -0.01]
        equity = equity_curve_from_returns(original, initial_value=100.0)
        recovered = returns_from_equity(equity)
        for o, r in zip(original, recovered):
            assert r == pytest.approx(o, rel=1e-12)


# ---------------------------------------------------------------------------
# active_returns
# ---------------------------------------------------------------------------


class TestActiveReturns:
    def test_empty_returns_empty(self):
        assert active_returns([], []) == []

    def test_known_values(self):
        out = active_returns([0.10, 0.05], [0.08, 0.06])
        assert out == [pytest.approx(0.02), pytest.approx(-0.01)]

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            active_returns([0.01, 0.02], [0.01, 0.02, 0.03])

    def test_zero_active_when_identical(self):
        out = active_returns([0.01, 0.02, 0.03], [0.01, 0.02, 0.03])
        assert all(v == pytest.approx(0.0) for v in out)


# ---------------------------------------------------------------------------
# tracking_error
# ---------------------------------------------------------------------------


class TestTrackingError:
    def test_empty_returns_zero(self):
        assert tracking_error([], []) == 0.0

    def test_single_point_returns_zero(self):
        assert tracking_error([0.01], [0.01]) == 0.0

    def test_identical_streams_zero_te(self):
        out = tracking_error([0.01, 0.02, 0.03], [0.01, 0.02, 0.03])
        assert out == pytest.approx(0.0, abs=1e-12)

    def test_constant_active_zero_te(self):
        # Constant excess return → no variance → TE 0.
        out = tracking_error(
            [0.05, 0.05, 0.05, 0.05], [0.02, 0.02, 0.02, 0.02]
        )
        assert out == pytest.approx(0.0, abs=1e-12)

    def test_non_constant_active_positive_te(self):
        out = tracking_error(
            [0.10, -0.05, 0.10, -0.05], [0.05, 0.05, 0.05, 0.05]
        )
        assert out > 0

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            tracking_error([0.01], [0.01, 0.02])

    def test_zero_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            tracking_error([0.01, 0.02], [0.01, 0.02], annualisation_factor=0)

    def test_negative_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            tracking_error([0.01, 0.02], [0.01, 0.02], annualisation_factor=-1)


# ---------------------------------------------------------------------------
# beating_benchmark_pct
# ---------------------------------------------------------------------------


class TestBeatingBenchmarkPct:
    def test_empty_returns_zero(self):
        assert beating_benchmark_pct([], []) == 0.0

    def test_all_beat(self):
        out = beating_benchmark_pct([0.05, 0.06, 0.07], [0.01, 0.02, 0.03])
        assert out == pytest.approx(1.0)

    def test_none_beat(self):
        out = beating_benchmark_pct([0.01, 0.02, 0.03], [0.05, 0.06, 0.07])
        assert out == pytest.approx(0.0)

    def test_mixed(self):
        # 2 of 4 beat.
        out = beating_benchmark_pct(
            [0.10, 0.01, 0.10, 0.01], [0.05, 0.05, 0.05, 0.05]
        )
        assert out == pytest.approx(0.5)

    def test_ties_not_counted_as_beating(self):
        out = beating_benchmark_pct([0.05, 0.05], [0.05, 0.05])
        assert out == pytest.approx(0.0)

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            beating_benchmark_pct([0.01], [0.01, 0.02])
