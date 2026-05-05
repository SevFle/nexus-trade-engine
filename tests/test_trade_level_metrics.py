"""Tests for trade-level metrics: payoff ratio, expectancy ($), expectancy (R), Kelly criterion.

These functions were added in gh#97 commit 9803ccf but have no existing test coverage.
"""

from __future__ import annotations

import math

import pytest

from engine.core.metrics_extras import (
    compute_expectancy_dollars,
    compute_expectancy_r_multiple,
    compute_kelly_criterion,
    compute_payoff_ratio,
)


class TestPayoffRatio:
    def test_empty_returns_zero(self):
        assert compute_payoff_ratio([]) == 0.0

    def test_no_winners_returns_zero(self):
        assert compute_payoff_ratio([-1.0, -2.0, -3.0]) == 0.0

    def test_no_losers_returns_inf(self):
        assert compute_payoff_ratio([1.0, 2.0, 3.0]) == math.inf

    def test_known_value(self):
        trades = [3.0, -1.0, 6.0, -2.0]
        avg_win = (3.0 + 6.0) / 2
        avg_loss = (1.0 + 2.0) / 2
        assert compute_payoff_ratio(trades) == pytest.approx(avg_win / avg_loss)

    def test_single_winner_single_loser(self):
        assert compute_payoff_ratio([10.0, -5.0]) == pytest.approx(2.0)

    def test_equal_magnitudes(self):
        assert compute_payoff_ratio([5.0, -5.0]) == pytest.approx(1.0)

    def test_losers_outweigh_winners(self):
        result = compute_payoff_ratio([2.0, -8.0])
        assert result == pytest.approx(0.25)

    def test_winners_outweigh_losers(self):
        result = compute_payoff_ratio([10.0, -2.0])
        assert result == pytest.approx(5.0)

    def test_zero_pnl_excluded_from_both(self):
        assert compute_payoff_ratio([0.0, 0.0]) == 0.0

    def test_mixed_with_zeros(self):
        trades = [0.0, 5.0, 0.0, -2.5]
        assert compute_payoff_ratio(trades) == pytest.approx(2.0)

    def test_many_trades(self):
        winners = [10.0] * 6
        losers = [-2.0] * 4
        result = compute_payoff_ratio(winners + losers)
        assert result == pytest.approx(5.0)

    def test_very_small_values(self):
        result = compute_payoff_ratio([0.0001, -0.0001])
        assert result == pytest.approx(1.0)

    def test_very_large_values(self):
        result = compute_payoff_ratio([1e12, -1e12])
        assert result == pytest.approx(1.0)

    def test_single_winner_no_losers(self):
        assert compute_payoff_ratio([42.0]) == math.inf


class TestExpectancyDollars:
    def test_empty_returns_zero(self):
        assert compute_expectancy_dollars([]) == 0.0

    def test_known_value(self):
        trades = [100.0, -50.0, 75.0, -25.0]
        expected = sum(trades) / len(trades)
        assert compute_expectancy_dollars(trades) == pytest.approx(expected)

    def test_all_winners(self):
        trades = [10.0, 20.0, 30.0]
        assert compute_expectancy_dollars(trades) == pytest.approx(20.0)

    def test_all_losers(self):
        trades = [-10.0, -20.0, -30.0]
        assert compute_expectancy_dollars(trades) == pytest.approx(-20.0)

    def test_single_trade(self):
        assert compute_expectancy_dollars([50.0]) == 50.0

    def test_single_loss(self):
        assert compute_expectancy_dollars([-50.0]) == -50.0

    def test_zero_pnl_trades(self):
        assert compute_expectancy_dollars([0.0, 0.0, 0.0]) == 0.0

    def test_mixed_with_zero_pnl(self):
        trades = [10.0, 0.0, -5.0]
        assert compute_expectancy_dollars(trades) == pytest.approx(5.0 / 3)

    def test_breakeven_strategy(self):
        trades = [10.0, -10.0, 5.0, -5.0]
        assert compute_expectancy_dollars(trades) == pytest.approx(0.0)

    def test_large_number_of_trades(self):
        n = 1000
        trades = [1.0] * (n // 2) + [-0.5] * (n // 2)
        expected = sum(trades) / n
        assert compute_expectancy_dollars(trades) == pytest.approx(expected)


class TestExpectancyRMultiple:
    def test_empty_returns_zero(self):
        assert compute_expectancy_r_multiple([], 100.0) == 0.0

    def test_zero_risk_returns_zero(self):
        assert compute_expectancy_r_multiple([10.0, -5.0], 0.0) == 0.0

    def test_negative_risk_returns_zero(self):
        assert compute_expectancy_r_multiple([10.0, -5.0], -50.0) == 0.0

    def test_known_value(self):
        trades = [200.0, -100.0]
        expectancy = compute_expectancy_dollars(trades)
        risk = 100.0
        assert compute_expectancy_r_multiple(trades, risk) == pytest.approx(
            expectancy / risk
        )

    def test_positive_expectancy_positive_r(self):
        result = compute_expectancy_r_multiple([10.0, -5.0], 5.0)
        assert result > 0

    def test_negative_expectancy(self):
        trades = [-10.0, 5.0]
        result = compute_expectancy_r_multiple(trades, 10.0)
        assert result < 0

    def test_one_half_r_expectancy(self):
        trades = [150.0, 50.0]
        avg = 100.0
        risk = 200.0
        assert compute_expectancy_r_multiple(trades, risk) == pytest.approx(
            avg / risk
        )

    def test_very_small_risk(self):
        trades = [0.01]
        result = compute_expectancy_r_multiple(trades, 1e-10)
        assert result > 0

    def test_both_empty_and_zero_risk(self):
        assert compute_expectancy_r_multiple([], 0.0) == 0.0


class TestKellyCriterion:
    def test_empty_returns_zero(self):
        assert compute_kelly_criterion([]) == 0.0

    def test_no_winners_returns_zero(self):
        assert compute_kelly_criterion([-1.0, -2.0]) == 0.0

    def test_no_losers_returns_zero(self):
        assert compute_kelly_criterion([1.0, 2.0]) == 0.0

    def test_known_value_even_game(self):
        trades = [1.0, -1.0]
        win_rate = 0.5
        loss_rate = 0.5
        payoff = 1.0
        expected = win_rate - loss_rate / payoff
        assert compute_kelly_criterion(trades) == pytest.approx(expected)

    def test_known_value_favorable_game(self):
        trades = [3.0, -1.0, 3.0, -1.0, 3.0, -1.0]
        n = len(trades)
        win_rate = 3 / n
        loss_rate = 3 / n
        payoff = 3.0
        expected = win_rate - loss_rate / payoff
        assert compute_kelly_criterion(trades) == pytest.approx(expected)

    def test_negative_kelly_unfavorable_game(self):
        trades = [1.0, -3.0, 1.0, -3.0]
        result = compute_kelly_criterion(trades)
        assert result < 0

    def test_symmetric_outcomes(self):
        trades = [2.0, -2.0]
        result = compute_kelly_criterion(trades)
        assert result == pytest.approx(0.0)

    def test_single_winner_single_loser(self):
        trades = [5.0, -1.0]
        win_rate = 0.5
        loss_rate = 0.5
        payoff = 5.0
        expected = win_rate - loss_rate / payoff
        assert compute_kelly_criterion(trades) == pytest.approx(expected)

    def test_60_percent_win_rate_equal_payoff(self):
        winners = [1.0] * 6
        losers = [-1.0] * 4
        trades = winners + losers
        win_rate = 0.6
        loss_rate = 0.4
        payoff = 1.0
        expected = win_rate - loss_rate / payoff
        assert compute_kelly_criterion(trades) == pytest.approx(expected)

    def test_many_small_winners_few_large_losers(self):
        winners = [1.0] * 9
        losers = [-9.0]
        trades = winners + losers
        result = compute_kelly_criterion(trades)
        assert result < 0

    def test_many_small_losers_few_large_winners(self):
        winners = [10.0]
        losers = [-1.0] * 9
        trades = winners + losers
        result = compute_kelly_criterion(trades)
        assert result > 0

    def test_very_high_payoff_ratio(self):
        trades = [1000.0, -1.0]
        result = compute_kelly_criterion(trades)
        assert result > 0
        assert result < 1.0

    def test_result_not_clamped_positive(self):
        trades = [1.0, -1.0, 1.0, -1.0, 1.0]
        result = compute_kelly_criterion(trades)
        assert result > 0

    def test_result_not_clamped_negative(self):
        trades = [-1.0, 1.0, -1.0, 1.0, -1.0]
        result = compute_kelly_criterion(trades)
        assert result < 0

    def test_zero_pnl_does_not_affect(self):
        trades = [0.0, 0.0]
        assert compute_kelly_criterion(trades) == 0.0

    def test_zero_pnl_mixed_with_winners_losers(self):
        trades = [2.0, -1.0, 0.0]
        n = len(trades)
        win_rate = 1 / n
        loss_rate = 1 / n
        payoff = 2.0
        expected = win_rate - loss_rate / payoff
        assert compute_kelly_criterion(trades) == pytest.approx(expected)

    def test_all_equal_winners_and_losers(self):
        trades = [5.0, 5.0, 5.0, -1.0, -1.0, -1.0]
        win_rate = 0.5
        loss_rate = 0.5
        payoff = 5.0
        expected = win_rate - loss_rate / payoff
        assert compute_kelly_criterion(trades) == pytest.approx(expected)
