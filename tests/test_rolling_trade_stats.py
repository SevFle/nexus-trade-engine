"""Tests for rolling trade-stat + Calmar time series (gh#97 follow-up)."""

from __future__ import annotations

import pytest

from engine.core.rolling_trade_stats import (
    rolling_calmar,
    rolling_hit_ratio,
    rolling_profit_factor,
    rolling_win_loss_ratio,
)

# ---------------------------------------------------------------------------
# rolling_hit_ratio
# ---------------------------------------------------------------------------


class TestRollingHitRatio:
    def test_empty_returns_empty(self):
        assert rolling_hit_ratio([], 3) == []

    def test_window_too_large_all_none(self):
        assert rolling_hit_ratio([1.0, -1.0], 5) == [None, None]

    def test_first_indices_none(self):
        out = rolling_hit_ratio([1.0, -1.0, 1.0, 1.0], 3)
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_known_values(self):
        # window=3 over [1, -1, 1, 1, -1]
        # idx 2: [1,-1,1] → 2/3.
        # idx 3: [-1,1,1] → 2/3.
        # idx 4: [1,1,-1] → 2/3.
        out = rolling_hit_ratio([1.0, -1.0, 1.0, 1.0, -1.0], 3)
        assert out[2] == pytest.approx(2 / 3)
        assert out[3] == pytest.approx(2 / 3)
        assert out[4] == pytest.approx(2 / 3)

    def test_window_one_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_hit_ratio([1.0, -1.0], 1)


# ---------------------------------------------------------------------------
# rolling_profit_factor
# ---------------------------------------------------------------------------


class TestRollingProfitFactor:
    def test_empty_returns_empty(self):
        assert rolling_profit_factor([], 3) == []

    def test_first_indices_none(self):
        out = rolling_profit_factor([1.0, -1.0, 1.0], 3)
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_no_losses_in_window_returns_none(self):
        # Window [1, 2, 3] — no losses → None (infinite PF).
        out = rolling_profit_factor([1.0, 2.0, 3.0], 3)
        assert out[2] is None

    def test_known_value(self):
        # Window [10, -5, 20]: gross profit 30, gross loss 5 → 6.0.
        out = rolling_profit_factor([10.0, -5.0, 20.0], 3)
        assert out[2] == pytest.approx(6.0)

    def test_no_wins_returns_zero(self):
        out = rolling_profit_factor([-1.0, -2.0, -3.0], 3)
        assert out[2] == 0.0


# ---------------------------------------------------------------------------
# rolling_win_loss_ratio
# ---------------------------------------------------------------------------


class TestRollingWinLossRatio:
    def test_empty_returns_empty(self):
        assert rolling_win_loss_ratio([], 3) == []

    def test_first_indices_none(self):
        out = rolling_win_loss_ratio([1.0, -1.0, 1.0], 3)
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_known_value(self):
        # Window [20, -10, 20]: avg_win 20, avg_loss -10 → 2.0.
        out = rolling_win_loss_ratio([20.0, -10.0, 20.0], 3)
        assert out[2] == pytest.approx(2.0)

    def test_all_wins_returns_zero(self):
        # No losses → 0.0 (consistent with full-period helper).
        out = rolling_win_loss_ratio([1.0, 2.0, 3.0], 3)
        assert out[2] == 0.0


# ---------------------------------------------------------------------------
# rolling_calmar
# ---------------------------------------------------------------------------


class TestRollingCalmar:
    def test_empty_returns_empty(self):
        assert rolling_calmar([], 3) == []

    def test_window_too_large_all_none(self):
        assert rolling_calmar([100.0, 110.0], 5) == [None, None]

    def test_first_indices_none(self):
        out = rolling_calmar([100.0, 110.0, 105.0, 115.0], 3)
        assert out[:2] == [None, None]

    def test_no_drawdown_positive_return_none(self):
        # Monotonic increase, no drawdown → Calmar undefined → None.
        out = rolling_calmar([100.0, 105.0, 110.0, 115.0], 3, periods_per_year=252)
        # Last window: monotonic up → no drawdown, ann_ret > 0 → None.
        assert out[-1] is None

    def test_no_drawdown_flat_return_zero(self):
        # Flat curve → ann_ret 0, no drawdown → 0.0.
        out = rolling_calmar([100.0, 100.0, 100.0, 100.0], 3, periods_per_year=252)
        assert out[-1] == 0.0

    def test_drawdown_present_returns_finite(self):
        # 100 → 90 → 100 → 110 with window 4: ann_ret > 0, max_dd = 0.10.
        out = rolling_calmar(
            [100.0, 90.0, 100.0, 110.0], 4, periods_per_year=252
        )
        assert out[-1] is not None
        assert out[-1] > 0

    def test_negative_periods_per_year_rejected(self):
        with pytest.raises(ValueError, match="periods_per_year"):
            rolling_calmar([100.0, 110.0, 105.0], 2, periods_per_year=-1)

    def test_zero_periods_per_year_rejected(self):
        with pytest.raises(ValueError, match="periods_per_year"):
            rolling_calmar([100.0, 110.0, 105.0], 2, periods_per_year=0)

    def test_window_one_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_calmar([100.0, 110.0], 1)

    def test_output_length_matches_input(self):
        out = rolling_calmar([100.0] * 10, 3)
        assert len(out) == 10
