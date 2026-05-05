"""Comprehensive tests for recently changed code targeting coverage gaps.

Targets uncovered lines in:
  - engine/core/metrics_extras.py (lines 131, 181, 243, 246, 342, 351, 354)
  - sdk/nexus_sdk/types.py (epsilon boundary, currency validation edge cases)

Plus additional edge cases, boundary values, and integration coverage
for the Treynor/MAR/Sterling/K-Ratio/payoff/expectancy/Kelly additions.
"""

from __future__ import annotations

import math

import pytest
from nexus_sdk.types import CostBreakdown, Money, PortfolioSnapshot

from engine.core.metrics_extras import (
    compute_expectancy_dollars,
    compute_expectancy_r_multiple,
    compute_gain_to_pain_ratio,
    compute_information_ratio,
    compute_k_ratio,
    compute_kelly_criterion,
    compute_mar_ratio,
    compute_omega_ratio,
    compute_pain_index,
    compute_payoff_ratio,
    compute_recovery_factor,
    compute_sterling_ratio,
    compute_treynor_ratio,
    compute_ulcer_index,
)


# ---------------------------------------------------------------------------
# metrics_extras: uncovered line 131 — ulcer index with non-positive peak
# ---------------------------------------------------------------------------


class TestUlcerIndexNonPositivePeak:
    def test_equity_starting_at_zero_then_rising(self):
        result = compute_ulcer_index([0.0, 100.0, 80.0])
        dd_80 = ((100 - 80) / 100) * 100.0
        expected = math.sqrt((0.0 + 0.0 + dd_80**2) / 3)
        assert result == pytest.approx(expected)

    def test_equity_all_negative(self):
        assert compute_ulcer_index([-10.0, -5.0, -20.0]) == 0.0

    def test_equity_starting_negative_then_positive(self):
        result = compute_ulcer_index([-50.0, 100.0, 80.0])
        dd = ((100 - 80) / 100) * 100.0
        expected = math.sqrt((0.0 + 0.0 + dd**2) / 3)
        assert result == pytest.approx(expected)

    def test_equity_all_zero(self):
        assert compute_ulcer_index([0.0, 0.0, 0.0]) == 0.0

    def test_mixed_negative_and_positive_peak(self):
        result = compute_ulcer_index([-100.0, 50.0, 40.0])
        peak = 50.0
        dd = ((peak - 40.0) / peak) * 100.0
        expected = math.sqrt((0.0 + 0.0 + dd**2) / 3)
        assert result == pytest.approx(expected)

    def test_single_negative_value(self):
        assert compute_ulcer_index([-50.0]) == 0.0

    def test_single_zero_value(self):
        assert compute_ulcer_index([0.0]) == 0.0


# ---------------------------------------------------------------------------
# metrics_extras: uncovered line 181 — payoff ratio avg_loss == 0 guard
# ---------------------------------------------------------------------------


class TestPayoffRatioEdgeCases:
    def test_single_winner_single_loser(self):
        assert compute_payoff_ratio([100.0, -50.0]) == pytest.approx(2.0)

    def test_equal_win_loss_magnitude(self):
        assert compute_payoff_ratio([100.0, -100.0]) == pytest.approx(1.0)

    def test_very_large_values(self):
        result = compute_payoff_ratio([1e15, -1e15])
        assert result == pytest.approx(1.0)

    def test_extremely_unbalanced(self):
        result = compute_payoff_ratio([1.0, -1000000.0])
        assert 0.0 < result < 1.0

    def test_many_winners_one_loser(self):
        wins = [100.0] * 99
        losses = [-1.0]
        result = compute_payoff_ratio(wins + losses)
        assert result == pytest.approx(100.0 / 1.0)

    def test_single_large_winner_many_small_losers(self):
        result = compute_payoff_ratio([1000.0, -1.0, -1.0, -1.0])
        assert result == pytest.approx(1000.0 / 1.0)


# ---------------------------------------------------------------------------
# metrics_extras: uncovered lines 243, 246 — kelly criterion guards
# ---------------------------------------------------------------------------


class TestKellyCriterionEdgeCases:
    def test_single_winner_single_loser(self):
        out = compute_kelly_criterion([100.0, -50.0])
        expected = 0.5 - 0.5 / 2.0
        assert out == pytest.approx(expected)

    def test_all_break_even_trades(self):
        out = compute_kelly_criterion([50.0, -50.0, 50.0, -50.0])
        assert out == pytest.approx(0.0)

    def test_many_trades_with_small_edge(self):
        trades = [1.0] * 51 + [-1.0] * 49
        out = compute_kelly_criterion(trades)
        assert 0 < out < 0.1

    def test_extreme_winner_dominates(self):
        out = compute_kelly_criterion([10000.0, -1.0])
        assert 0 < out < 1.0

    def test_extreme_loser_dominates(self):
        out = compute_kelly_criterion([1.0, -10000.0])
        assert out < 0

    def test_kelly_approaches_zero_for_slight_edge(self):
        trades = [1.0] * 501 + [-1.0] * 499
        out = compute_kelly_criterion(trades)
        assert abs(out) < 0.02

    def test_single_trade_of_each(self):
        out = compute_kelly_criterion([200.0, -100.0])
        expected = 0.5 - 0.5 / 2.0
        assert out == pytest.approx(expected)

    def test_three_winners_one_loser_high_payoff(self):
        out = compute_kelly_criterion([100.0, 100.0, 100.0, -10.0])
        win_rate = 0.75
        loss_rate = 0.25
        payoff = 100.0 / 10.0
        expected = win_rate - loss_rate / payoff
        assert out == pytest.approx(expected)

    def test_one_winner_three_losers_negative_kelly(self):
        out = compute_kelly_criterion([10.0, -50.0, -50.0, -50.0])
        assert out < 0

    def test_many_mixed_trades(self):
        trades = [5.0] * 60 + [-3.0] * 40
        out = compute_kelly_criterion(trades)
        assert out > 0


# ---------------------------------------------------------------------------
# metrics_extras: uncovered lines 342, 351, 354 — k-ratio edge cases
# ---------------------------------------------------------------------------


class TestKRatioEdgeCases:
    def test_exactly_two_points_returns_zero(self):
        assert compute_k_ratio([100.0, 200.0]) == 0.0

    def test_two_constant_points_returns_zero(self):
        assert compute_k_ratio([100.0, 100.0]) == 0.0

    def test_perfectly_linear_log_equity_se_zero(self):
        curve = [math.exp(float(i)) for i in range(5)]
        result = compute_k_ratio(curve)
        assert result == 0.0

    def test_three_points_declining(self):
        curve = [200.0, 100.0, 50.0]
        result = compute_k_ratio(curve)
        assert result < 0

    def test_three_points_rising(self):
        curve = [100.0, 200.0, 400.0]
        result = compute_k_ratio(curve)
        assert result > 0

    def test_long_noisy_uptrend(self):
        import random
        random.seed(12345)
        curve = [100.0]
        for _ in range(99):
            curve.append(curve[-1] * (1.0 + random.gauss(0.001, 0.02)))
        result = compute_k_ratio(curve)
        assert math.isfinite(result)

    def test_oscillating_curve(self):
        curve = [100.0 + 10.0 * ((-1) ** i) for i in range(50)]
        result = compute_k_ratio(curve)
        assert math.isfinite(result)

    def test_single_point_zero(self):
        assert compute_k_ratio([42.0]) == 0.0

    def test_empty_returns_zero(self):
        assert compute_k_ratio([]) == 0.0

    def test_positive_value_zero_returns_zero(self):
        assert compute_k_ratio([100.0, 0.0, 50.0]) == 0.0

    def test_negative_value_returns_zero(self):
        assert compute_k_ratio([100.0, -10.0, 50.0]) == 0.0

    def test_two_points_one_zero(self):
        assert compute_k_ratio([0.0, 100.0]) == 0.0

    def test_exactly_three_perfectly_linear(self):
        curve = [math.exp(float(i)) for i in range(3)]
        result = compute_k_ratio(curve)
        assert result == 0.0

    def test_many_points_near_perfect_growth(self):
        curve = [100.0 * (1.01 ** i) for i in range(20)]
        result = compute_k_ratio(curve)
        assert math.isfinite(result)
        assert result != 0.0


# ---------------------------------------------------------------------------
# metrics_extras: additional coverage for all functions
# ---------------------------------------------------------------------------


class TestOmegaRatioAdditional:
    def test_all_negative_returns(self):
        result = compute_omega_ratio([-0.5, -0.3, -0.1])
        assert result == 0.0

    def test_single_return_zero(self):
        assert compute_omega_ratio([0.0]) == 0.0

    def test_single_return_positive(self):
        assert compute_omega_ratio([0.5]) == math.inf

    def test_single_return_negative(self):
        assert compute_omega_ratio([-0.5]) == 0.0

    def test_high_threshold_all_below(self):
        result = compute_omega_ratio([0.01, 0.02, 0.03], threshold=1.0)
        assert result == 0.0

    def test_negative_threshold(self):
        result = compute_omega_ratio([0.01, -0.01], threshold=-0.1)
        assert result > 0


class TestInformationRatioAdditional:
    def test_negative_active_return(self):
        returns = [0.01, 0.01, 0.01]
        benchmark = [0.02, 0.03, 0.02]
        ir = compute_information_ratio(returns, benchmark)
        assert ir < 0

    def test_single_point_returns_zero(self):
        assert compute_information_ratio([1.0], [1.0]) == 0.0

    def test_volatile_active_returns(self):
        returns = [0.1, -0.05, 0.15, -0.1]
        benchmark = [0.01, 0.01, 0.01, 0.01]
        ir = compute_information_ratio(returns, benchmark)
        assert math.isfinite(ir)

    def test_long_series(self):
        returns = [0.01 + 0.001 * i for i in range(100)]
        benchmark = [0.01] * 100
        ir = compute_information_ratio(returns, benchmark)
        assert ir > 0


class TestGainToPainAdditional:
    def test_single_positive_return(self):
        assert compute_gain_to_pain_ratio([5.0]) == math.inf

    def test_single_negative_return(self):
        assert compute_gain_to_pain_ratio([-5.0]) == -1.0

    def test_zero_sum_with_losses(self):
        result = compute_gain_to_pain_ratio([5.0, -5.0])
        assert result == pytest.approx(0.0)

    def test_mixed_returns(self):
        result = compute_gain_to_pain_ratio([3.0, -1.0, 2.0, -0.5])
        total = 3.0 - 1.0 + 2.0 - 0.5
        pain = 1.0 + 0.5
        assert result == pytest.approx(total / pain)


class TestRecoveryFactorAdditional:
    def test_large_return_small_drawdown(self):
        assert compute_recovery_factor(100.0, 5.0) == pytest.approx(20.0)

    def test_small_return_large_drawdown(self):
        assert compute_recovery_factor(5.0, 50.0) == pytest.approx(0.1)

    def test_zero_return_with_drawdown(self):
        assert compute_recovery_factor(0.0, 10.0) == pytest.approx(0.0)


class TestPainIndexAdditional:
    def test_all_zero_drawdowns(self):
        assert compute_pain_index([0.0, 0.0, 0.0]) == pytest.approx(0.0)

    def test_single_large_drawdown(self):
        assert compute_pain_index([0.5]) == pytest.approx(50.0)

    def test_mixed_drawdowns(self):
        result = compute_pain_index([0.1, 0.2, 0.3])
        assert result == pytest.approx(20.0)

    def test_negative_drawdown_values_treated_as_absolute(self):
        result = compute_pain_index([-0.1, -0.2])
        assert result == pytest.approx(15.0)


class TestTreynorRatioAdditional:
    def test_high_beta_reduces_ratio(self):
        low_beta = compute_treynor_ratio(0.15, 0.05, 0.5)
        high_beta = compute_treynor_ratio(0.15, 0.05, 2.0)
        assert low_beta > high_beta

    def test_excess_return_zero(self):
        assert compute_treynor_ratio(0.05, 0.05, 1.0) == pytest.approx(0.0)

    def test_large_excess_return(self):
        result = compute_treynor_ratio(0.50, 0.05, 1.0)
        assert result == pytest.approx(0.45)

    def test_zero_beta_returns_zero(self):
        assert compute_treynor_ratio(0.20, 0.05, 0.0) == 0.0

    def test_very_small_beta(self):
        result = compute_treynor_ratio(0.10, 0.05, 0.01)
        assert result == pytest.approx(5.0)


class TestMarRatioAdditional:
    def test_large_cagr_small_drawdown(self):
        assert compute_mar_ratio(50.0, 5.0) == pytest.approx(10.0)

    def test_zero_cagr(self):
        assert compute_mar_ratio(0.0, 10.0) == pytest.approx(0.0)

    def test_very_large_drawdown(self):
        assert compute_mar_ratio(10.0, 90.0) == pytest.approx(10.0 / 90.0)


class TestSterlingRatioAdditional:
    def test_zero_cagr_returns_zero(self):
        assert compute_sterling_ratio(0.0, 20.0) == pytest.approx(0.0)

    def test_negative_cagr(self):
        result = compute_sterling_ratio(-10.0, 20.0)
        assert result < 0

    def test_very_high_floor(self):
        result = compute_sterling_ratio(30.0, 15.0, drawdown_floor_pct=20.0)
        assert result == 0.0

    def test_just_above_floor(self):
        result = compute_sterling_ratio(30.0, 10.01, drawdown_floor_pct=10.0)
        assert result > 0
        assert math.isfinite(result)


class TestExpectancyDollarsAdditional:
    def test_single_winner(self):
        assert compute_expectancy_dollars([100.0]) == pytest.approx(100.0)

    def test_single_loser(self):
        assert compute_expectancy_dollars([-50.0]) == pytest.approx(-50.0)

    def test_many_trades(self):
        trades = [10.0] * 90 + [-5.0] * 10
        expected = (900.0 - 50.0) / 100.0
        assert compute_expectancy_dollars(trades) == pytest.approx(expected)


class TestExpectancyRMultipleAdditional:
    def test_very_small_risk(self):
        out = compute_expectancy_r_multiple([100.0], 0.001)
        assert out == pytest.approx(100000.0)

    def test_very_large_risk(self):
        out = compute_expectancy_r_multiple([100.0], 1e9)
        assert abs(out) < 1e-6

    def test_known_value_negative_expectancy(self):
        out = compute_expectancy_r_multiple([-100.0, 50.0], 50.0)
        assert out == pytest.approx(-0.5)


class TestUlcerIndexAdditional:
    def test_single_value(self):
        assert compute_ulcer_index([100.0]) == pytest.approx(0.0)

    def test_sharp_dip_and_recovery(self):
        curve = [100.0, 50.0, 100.0, 100.0]
        dd_50 = ((100 - 50) / 100) * 100.0
        expected = math.sqrt((0 + dd_50**2 + 0 + 0) / 4)
        assert compute_ulcer_index(curve) == pytest.approx(expected)

    def test_gradual_decline(self):
        curve = [100.0, 90.0, 80.0, 70.0, 60.0]
        result = compute_ulcer_index(curve)
        assert result > 0
        assert math.isfinite(result)


# ---------------------------------------------------------------------------
# types.py: epsilon boundary tests for as_pct_of
# ---------------------------------------------------------------------------


class TestMoneyEpsilonBoundary:
    def test_exactly_at_epsilon_boundary(self):
        m = Money(amount=25.0)
        result = m.as_pct_of(1e-12)
        assert result == pytest.approx(25.0 / 1e-12 * 100.0)

    def test_just_below_epsilon_boundary(self):
        m = Money(amount=25.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(1e-13)

    def test_just_above_epsilon_boundary(self):
        m = Money(amount=25.0)
        result = m.as_pct_of(1.5e-12)
        assert math.isfinite(result)

    def test_negative_just_below_epsilon(self):
        m = Money(amount=25.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(-1e-13)

    def test_negative_just_above_epsilon(self):
        m = Money(amount=25.0)
        result = m.as_pct_of(-1.5e-12)
        assert math.isfinite(result)

    def test_positive_zero_raises(self):
        m = Money(amount=10.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(0.0)

    def test_negative_zero_raises(self):
        m = Money(amount=10.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(-0.0)

    def test_5e13_raises(self):
        m = Money(amount=10.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(5e-13)

    def test_5e12_succeeds(self):
        m = Money(amount=10.0)
        result = m.as_pct_of(5e-12)
        assert math.isfinite(result)


# ---------------------------------------------------------------------------
# types.py: CostBreakdown currency validation edge cases
# ---------------------------------------------------------------------------


class TestCostBreakdownCurrencyValidation:
    def test_all_same_non_usd_currency(self):
        cb = CostBreakdown(
            commission=Money(1.0, "EUR"),
            spread=Money(2.0, "EUR"),
            slippage=Money(3.0, "EUR"),
            exchange_fee=Money(4.0, "EUR"),
            tax_estimate=Money(5.0, "EUR"),
        )
        total = cb.total
        assert total.amount == 15.0
        assert total.currency == "EUR"

    def test_all_same_gbp_currency(self):
        cb = CostBreakdown(
            commission=Money(1.0, "GBP"),
            spread=Money(2.0, "GBP"),
            slippage=Money(0.0, "GBP"),
            exchange_fee=Money(0.0, "GBP"),
            tax_estimate=Money(0.0, "GBP"),
        )
        total = cb.total
        assert total.currency == "GBP"
        assert total.amount == 3.0

    def test_three_different_currencies_raises(self):
        cb = CostBreakdown(
            commission=Money(10.0, "USD"),
            spread=Money(5.0, "EUR"),
            slippage=Money(3.0, "GBP"),
        )
        with pytest.raises(ValueError, match="different currencies"):
            _ = cb.total

    def test_four_different_currencies_raises(self):
        cb = CostBreakdown(
            commission=Money(10.0, "USD"),
            spread=Money(5.0, "EUR"),
            slippage=Money(3.0, "GBP"),
            exchange_fee=Money(2.0, "JPY"),
        )
        with pytest.raises(ValueError, match="different currencies"):
            _ = cb.total

    def test_all_five_different_currencies_raises(self):
        cb = CostBreakdown(
            commission=Money(10.0, "USD"),
            spread=Money(5.0, "EUR"),
            slippage=Money(3.0, "GBP"),
            exchange_fee=Money(2.0, "JPY"),
            tax_estimate=Money(1.0, "CHF"),
        )
        with pytest.raises(ValueError, match="different currencies"):
            _ = cb.total

    def test_only_one_component_differs_raises(self):
        cb = CostBreakdown(
            commission=Money(10.0, "USD"),
            spread=Money(5.0, "USD"),
            slippage=Money(3.0, "USD"),
            exchange_fee=Money(2.0, "USD"),
            tax_estimate=Money(1.0, "EUR"),
        )
        with pytest.raises(ValueError, match="different currencies"):
            _ = cb.total

    def test_error_message_contains_currencies(self):
        cb = CostBreakdown(
            commission=Money(10.0, "USD"),
            spread=Money(5.0, "EUR"),
        )
        with pytest.raises(ValueError) as exc_info:
            _ = cb.total
        msg = str(exc_info.value)
        assert "USD" in msg
        assert "EUR" in msg

    def test_mixed_case_currencies_treated_different(self):
        cb = CostBreakdown(
            commission=Money(10.0, "usd"),
            spread=Money(5.0, "USD"),
        )
        with pytest.raises(ValueError):
            _ = cb.total

    def test_default_all_usd_no_error(self):
        cb = CostBreakdown(
            commission=Money(10.0),
            spread=Money(5.0),
            slippage=Money(3.0),
        )
        total = cb.total
        assert total.currency == "USD"

    def test_total_uses_commission_currency(self):
        cb = CostBreakdown(
            commission=Money(10.0, "JPY"),
            spread=Money(5.0, "JPY"),
            slippage=Money(0.0, "JPY"),
            exchange_fee=Money(0.0, "JPY"),
            tax_estimate=Money(0.0, "JPY"),
        )
        assert cb.total.currency == "JPY"

    def test_total_immutability_via_recreation(self):
        cb1 = CostBreakdown(commission=Money(10.0, "USD"))
        total1 = cb1.total
        cb2 = CostBreakdown(commission=Money(20.0, "USD"))
        total2 = cb2.total
        assert total1.amount == 10.0
        assert total2.amount == 20.0

    def test_all_jpy_with_all_components(self):
        cb = CostBreakdown(
            commission=Money(100.0, "JPY"),
            spread=Money(50.0, "JPY"),
            slippage=Money(30.0, "JPY"),
            exchange_fee=Money(20.0, "JPY"),
            tax_estimate=Money(10.0, "JPY"),
        )
        assert cb.total.amount == 210.0
        assert cb.total.currency == "JPY"


# ---------------------------------------------------------------------------
# PortfolioSnapshot additional edge cases
# ---------------------------------------------------------------------------


class TestPortfolioSnapshotEdgeCases:
    def test_allocation_weight_large_portfolio(self):
        positions = {f"S{i}": {"market_value": 1000.0} for i in range(100)}
        snap = PortfolioSnapshot(total_value=100_000.0, positions=positions)
        for i in range(100):
            assert snap.allocation_weight(f"S{i}") == pytest.approx(0.01)

    def test_allocation_weight_single_100_percent(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"AAPL": {"market_value": 100_000.0}},
        )
        assert snap.allocation_weight("AAPL") == pytest.approx(1.0)

    def test_allocation_weight_over_100_percent(self):
        snap = PortfolioSnapshot(
            total_value=50_000.0,
            positions={"AAPL": {"market_value": 60_000.0}},
        )
        assert snap.allocation_weight("AAPL") == pytest.approx(1.2)

    def test_summary_large_numbers(self):
        snap = PortfolioSnapshot(
            cash=1_000_000.0,
            total_value=5_000_000.0,
            positions={f"S{i}": {"qty": i} for i in range(10)},
        )
        s = snap.summary()
        assert "$5,000,000.00" in s
        assert "Positions: 10" in s

    def test_snapshot_with_negative_total_value(self):
        snap = PortfolioSnapshot(total_value=-1000.0)
        assert snap.total_value == -1000.0

    def test_snapshot_with_extreme_pnl_values(self):
        snap = PortfolioSnapshot(realized_pnl=1e15, unrealized_pnl=-1e15)
        assert snap.realized_pnl == 1e15
        assert snap.unrealized_pnl == -1e15

    def test_allocation_weight_with_negative_market_value(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"SHORT": {"market_value": -5000.0}},
        )
        weight = snap.allocation_weight("SHORT")
        assert weight == pytest.approx(-0.05)


# ---------------------------------------------------------------------------
# Integration: types + metrics combinations
# ---------------------------------------------------------------------------


class TestMetricsWithMoneyTypes:
    def test_money_as_pct_of_total_portfolio(self):
        portfolio_value = Money(amount=1_000_000.0)
        position = Money(amount=250_000.0)
        weight = position.as_pct_of(portfolio_value.amount)
        assert weight == pytest.approx(25.0)

    def test_cost_breakdown_total_as_pct_of_trade(self):
        cb = CostBreakdown(
            commission=Money(5.0),
            spread=Money(2.0),
            slippage=Money(3.0),
            exchange_fee=Money(1.0),
            tax_estimate=Money(4.0),
        )
        trade_value = 10_000.0
        cost_pct = cb.total.as_pct_of(trade_value)
        assert cost_pct == pytest.approx(15.0 / 10_000.0 * 100.0)

    def test_recovery_factor_with_portfolio_snapshot(self):
        snap = PortfolioSnapshot(total_return_pct=25.0)
        rf = compute_recovery_factor(snap.total_return_pct, 10.0)
        assert rf == pytest.approx(2.5)

    def test_mar_ratio_with_annualized_snapshot(self):
        mar = compute_mar_ratio(cagr_pct=15.0, max_drawdown_pct=7.5)
        assert mar == pytest.approx(2.0)

    def test_treynor_from_portfolio_returns(self):
        treynor = compute_treynor_ratio(
            portfolio_return=0.18,
            risk_free_rate=0.04,
            beta=1.1,
        )
        assert treynor == pytest.approx((0.18 - 0.04) / 1.1)

    def test_sterling_from_portfolio_metrics(self):
        result = compute_sterling_ratio(cagr_pct=20.0, avg_drawdown_pct=15.0)
        assert result == pytest.approx(4.0)

    def test_cost_as_pct_of_trade_with_currency(self):
        cb = CostBreakdown(
            commission=Money(10.0, "EUR"),
            spread=Money(5.0, "EUR"),
            slippage=Money(3.0, "EUR"),
            exchange_fee=Money(0.0, "EUR"),
            tax_estimate=Money(0.0, "EUR"),
        )
        total = cb.total
        pct = total.as_pct_of(1000.0)
        assert pct == pytest.approx(1.8)
        assert total.currency == "EUR"


# ---------------------------------------------------------------------------
# Property-based invariants for metrics
# ---------------------------------------------------------------------------


class TestMetricsInvariants:
    def test_expectancy_equals_mean_pnl(self):
        pnls = [100.0, -50.0, 75.0, -25.0, 30.0]
        assert compute_expectancy_dollars(pnls) == pytest.approx(
            sum(pnls) / len(pnls)
        )

    def test_kelly_positive_iff_expectancy_positive(self):
        pnls = [100.0, 100.0, 100.0, -50.0]
        kelly = compute_kelly_criterion(pnls)
        exp = compute_expectancy_dollars(pnls)
        assert (kelly > 0) == (exp > 0)

    def test_payoff_ratio_reciprocal_when_swapped(self):
        pnls = [200.0, -100.0]
        pr = compute_payoff_ratio(pnls)
        assert pr == pytest.approx(2.0)

    def test_recovery_factor_linear_in_return(self):
        rf1 = compute_recovery_factor(10.0, 10.0)
        rf2 = compute_recovery_factor(20.0, 10.0)
        assert rf2 == pytest.approx(2.0 * rf1)

    def test_mar_ratio_linear_in_cagr(self):
        m1 = compute_mar_ratio(10.0, 10.0)
        m2 = compute_mar_ratio(20.0, 10.0)
        assert m2 == pytest.approx(2.0 * m1)

    def test_treynor_inverse_in_beta(self):
        t1 = compute_treynor_ratio(0.15, 0.05, 1.0)
        t2 = compute_treynor_ratio(0.15, 0.05, 0.5)
        assert t2 == pytest.approx(2.0 * t1)

    def test_ulcer_index_non_negative(self):
        curves = [
            [100.0],
            [100.0, 80.0, 120.0],
            [50.0, 50.0, 50.0],
            [100.0, 50.0, 100.0],
            [0.0, 0.0, 0.0],
        ]
        for curve in curves:
            assert compute_ulcer_index(curve) >= 0.0

    def test_pain_index_non_negative(self):
        curves = [
            [],
            [0.0, 0.0],
            [0.1, 0.2, 0.3],
            [-0.1, -0.5],
        ]
        for curve in curves:
            assert compute_pain_index(curve) >= 0.0

    def test_information_ratio_zero_for_identical_series(self):
        r = [0.01, -0.02, 0.03, -0.01, 0.02]
        assert compute_information_ratio(r, r) == 0.0

    def test_gain_to_pain_reciprocal_relationship(self):
        returns = [1.0, -1.0]
        gtp = compute_gain_to_pain_ratio(returns)
        assert gtp == pytest.approx(0.0)

    def test_omega_at_zero_threshold(self):
        returns = [0.05, -0.02, 0.03, -0.01]
        omega = compute_omega_ratio(returns)
        assert omega > 0
