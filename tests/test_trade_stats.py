"""Tests for trade-level performance statistics (gh#97 follow-up)."""

from __future__ import annotations

import pytest

from engine.core.trade_stats import (
    average_loss,
    average_win,
    current_streak,
    hit_ratio,
    largest_loss,
    largest_win,
    max_consecutive_losses,
    max_consecutive_wins,
    profit_factor,
    win_loss_ratio,
)

# ---------------------------------------------------------------------------
# hit_ratio
# ---------------------------------------------------------------------------


class TestHitRatio:
    def test_empty_returns_zero(self):
        assert hit_ratio([]) == 0.0

    def test_all_wins(self):
        assert hit_ratio([10.0, 20.0, 30.0]) == pytest.approx(1.0)

    def test_all_losses(self):
        assert hit_ratio([-10.0, -20.0]) == 0.0

    def test_mixed(self):
        # 3 of 5 positive → 0.6.
        assert hit_ratio([10.0, -5.0, 20.0, -3.0, 15.0]) == pytest.approx(0.6)

    def test_breakeven_not_counted(self):
        # Convention: breakeven not a win.
        assert hit_ratio([0.0, 10.0, 0.0]) == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# average_win / average_loss
# ---------------------------------------------------------------------------


class TestAverageWin:
    def test_empty_returns_zero(self):
        assert average_win([]) == 0.0

    def test_no_wins_returns_zero(self):
        assert average_win([-10.0, -20.0]) == 0.0

    def test_known_value(self):
        # Wins: 10, 20, 30 → mean 20.
        assert average_win([10.0, -5.0, 20.0, 30.0]) == pytest.approx(20.0)

    def test_breakeven_not_a_win(self):
        # Wins: 10 only. Mean 10.
        assert average_win([0.0, 10.0, 0.0]) == pytest.approx(10.0)


class TestAverageLoss:
    def test_empty_returns_zero(self):
        assert average_loss([]) == 0.0

    def test_no_losses_returns_zero(self):
        assert average_loss([10.0, 20.0]) == 0.0

    def test_known_value_negative(self):
        # Losses: -10, -20 → mean -15.
        assert average_loss([10.0, -10.0, -20.0]) == pytest.approx(-15.0)

    def test_breakeven_not_a_loss(self):
        assert average_loss([0.0, -10.0, 0.0]) == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# win_loss_ratio
# ---------------------------------------------------------------------------


class TestWinLossRatio:
    def test_empty_returns_zero(self):
        assert win_loss_ratio([]) == 0.0

    def test_no_losses_returns_zero(self):
        # Convention: 0.0, not infinity.
        assert win_loss_ratio([10.0, 20.0]) == 0.0

    def test_no_wins_returns_zero(self):
        assert win_loss_ratio([-10.0, -20.0]) == 0.0

    def test_known_value(self):
        # avg_win = 20, avg_loss = -10 → ratio 2.0.
        assert win_loss_ratio([20.0, -10.0]) == pytest.approx(2.0)

    def test_symmetric_payoff_ratio_one(self):
        assert win_loss_ratio([10.0, -10.0]) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# profit_factor
# ---------------------------------------------------------------------------


class TestProfitFactor:
    def test_empty_returns_zero(self):
        assert profit_factor([]) == 0.0

    def test_no_losses_returns_none(self):
        # Mathematically infinite — None preserves "no data" vs "infinity".
        assert profit_factor([10.0, 20.0]) is None

    def test_no_wins_returns_zero(self):
        assert profit_factor([-10.0, -20.0]) == 0.0

    def test_breakeven_only_returns_zero(self):
        assert profit_factor([0.0, 0.0]) == 0.0

    def test_known_value(self):
        # gross profit 30, gross loss 15 → 2.0.
        assert profit_factor([10.0, 20.0, -5.0, -10.0]) == pytest.approx(2.0)

    def test_breakeven_does_not_affect(self):
        assert profit_factor([10.0, 0.0, -5.0]) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# largest_win / largest_loss
# ---------------------------------------------------------------------------


class TestLargestWin:
    def test_empty_returns_zero(self):
        assert largest_win([]) == 0.0

    def test_no_wins_returns_zero(self):
        assert largest_win([-10.0, -20.0]) == 0.0

    def test_known_value(self):
        assert largest_win([5.0, 100.0, -50.0, 25.0]) == 100.0


class TestLargestLoss:
    def test_empty_returns_zero(self):
        assert largest_loss([]) == 0.0

    def test_no_losses_returns_zero(self):
        assert largest_loss([5.0, 10.0]) == 0.0

    def test_known_value_negative(self):
        # Largest loss = most negative = -50.
        assert largest_loss([5.0, 100.0, -50.0, -25.0]) == -50.0


# ---------------------------------------------------------------------------
# max_consecutive_wins / max_consecutive_losses
# ---------------------------------------------------------------------------


class TestMaxConsecutiveWins:
    def test_empty_returns_zero(self):
        assert max_consecutive_wins([]) == 0

    def test_no_wins_returns_zero(self):
        assert max_consecutive_wins([-1.0, -2.0]) == 0

    def test_simple_streak(self):
        assert max_consecutive_wins([1.0, 2.0, 3.0, -1.0, 1.0]) == 3

    def test_streak_at_end(self):
        assert max_consecutive_wins([-1.0, 1.0, 2.0, 3.0, 4.0]) == 4

    def test_breakeven_breaks_streak(self):
        # 1, 1, 0, 1 → longest run is 2.
        assert max_consecutive_wins([1.0, 1.0, 0.0, 1.0]) == 2


class TestMaxConsecutiveLosses:
    def test_empty_returns_zero(self):
        assert max_consecutive_losses([]) == 0

    def test_no_losses_returns_zero(self):
        assert max_consecutive_losses([1.0, 2.0]) == 0

    def test_simple_streak(self):
        assert max_consecutive_losses([1.0, -1.0, -2.0, -3.0, 1.0]) == 3

    def test_breakeven_breaks_streak(self):
        assert max_consecutive_losses([-1.0, -1.0, 0.0, -1.0]) == 2


# ---------------------------------------------------------------------------
# current_streak
# ---------------------------------------------------------------------------


class TestCurrentStreak:
    def test_empty_returns_zero(self):
        assert current_streak([]) == 0

    def test_breakeven_last_returns_zero(self):
        assert current_streak([1.0, 1.0, 0.0]) == 0

    def test_winning_streak_positive(self):
        assert current_streak([-1.0, 1.0, 1.0, 1.0]) == 3

    def test_losing_streak_negative(self):
        assert current_streak([1.0, -1.0, -1.0]) == -2

    def test_single_win(self):
        assert current_streak([1.0]) == 1

    def test_single_loss(self):
        assert current_streak([-1.0]) == -1

    def test_full_winning_run(self):
        assert current_streak([1.0, 2.0, 3.0]) == 3

    def test_breakeven_in_middle_breaks_streak(self):
        # Ends in winning, but breakeven before breaks it. Streak counts
        # only from the breakeven onward → 1.
        assert current_streak([1.0, 1.0, 0.0, 1.0]) == 1
