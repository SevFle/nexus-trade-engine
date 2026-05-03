"""Tests for trade-level metrics extras (gh#97 follow-up)."""

from __future__ import annotations

import math

import pytest

from engine.core.metrics_extras import (
    compute_expectancy_dollars,
    compute_expectancy_r_multiple,
    compute_kelly_criterion,
    compute_payoff_ratio,
)


# ---------------------------------------------------------------------------
# Payoff ratio
# ---------------------------------------------------------------------------


class TestPayoffRatio:
    def test_empty_returns_zero(self):
        assert compute_payoff_ratio([]) == 0.0

    def test_known_value(self):
        # avg_win = (200 + 100) / 2 = 150
        # avg_loss = abs((-50 + -100) / 2) = 75
        # payoff = 150 / 75 = 2.0
        assert compute_payoff_ratio([200, 100, -50, -100]) == pytest.approx(2.0)

    def test_no_winners_returns_zero(self):
        assert compute_payoff_ratio([-10, -20]) == 0.0

    def test_no_losers_returns_inf(self):
        assert compute_payoff_ratio([100, 50]) == math.inf

    def test_zero_pnls_treated_as_neither(self):
        # All zeros → no winners, no losers → 0.0 (no winners path).
        assert compute_payoff_ratio([0.0, 0.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# Expectancy in dollars
# ---------------------------------------------------------------------------


class TestExpectancyDollars:
    def test_empty_returns_zero(self):
        assert compute_expectancy_dollars([]) == 0.0

    def test_balanced_returns_zero(self):
        assert compute_expectancy_dollars([100, -100]) == 0.0

    def test_known_value(self):
        # mean of [200, 100, -50, -100] = 37.5
        assert compute_expectancy_dollars([200, 100, -50, -100]) == 37.5

    def test_negative_when_losing_system(self):
        assert compute_expectancy_dollars([10, -50]) == -20.0


# ---------------------------------------------------------------------------
# Expectancy in R-multiples
# ---------------------------------------------------------------------------


class TestExpectancyRMultiple:
    def test_empty_returns_zero(self):
        assert compute_expectancy_r_multiple([], 100.0) == 0.0

    def test_zero_risk_returns_zero(self):
        assert compute_expectancy_r_multiple([100, -50], 0.0) == 0.0

    def test_negative_risk_returns_zero(self):
        # Defensive: nonsensical risk → 0.0.
        assert compute_expectancy_r_multiple([100, -50], -1.0) == 0.0

    def test_known_value(self):
        # mean PnL 37.5, R = 50 → 0.75 R per trade.
        out = compute_expectancy_r_multiple([200, 100, -50, -100], 50.0)
        assert out == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Kelly criterion
# ---------------------------------------------------------------------------


class TestKelly:
    def test_empty_returns_zero(self):
        assert compute_kelly_criterion([]) == 0.0

    def test_no_winners_returns_zero(self):
        assert compute_kelly_criterion([-10, -20]) == 0.0

    def test_no_losers_returns_zero(self):
        # Never-losing system → Kelly undefined; we return 0 rather
        # than inf so the caller doesn't risk infinite capital.
        assert compute_kelly_criterion([10, 20]) == 0.0

    def test_balanced_50_50_zero_edge_returns_zero(self):
        # 50 % win-rate with 1.0 payoff ratio → Kelly = 0.5 - 0.5/1 = 0.
        out = compute_kelly_criterion([100, -100])
        assert out == pytest.approx(0.0)

    def test_high_win_rate_high_payoff_positive_kelly(self):
        # 3 winners (avg 100) + 1 loser (-50). win_rate=0.75, loss_rate=0.25,
        # payoff = 100/50 = 2. Kelly = 0.75 - 0.25/2 = 0.625.
        out = compute_kelly_criterion([100, 100, 100, -50])
        assert out == pytest.approx(0.625)

    def test_negative_kelly_when_edge_negative(self):
        # 1 winner of 10, 3 losers of -50 each.
        # win_rate=0.25, loss_rate=0.75, payoff=10/50=0.2.
        # Kelly = 0.25 - 0.75/0.2 = -3.5 → caller skips the trade.
        out = compute_kelly_criterion([10, -50, -50, -50])
        assert out == pytest.approx(-3.5)
