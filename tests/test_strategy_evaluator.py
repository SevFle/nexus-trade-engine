"""Tests for engine.core.strategy_evaluator — cross-strategy composite score."""

from __future__ import annotations

import math

import pytest

from engine.core.metrics import MetricsReport, RollingWindowMetrics
from engine.core.strategy_evaluator import (
    EvaluationDimension,
    EvaluationResult,
    EvaluationWeights,
    StrategyEvaluator,
    StrategyEvaluatorError,
)


def _report(
    *,
    sharpe: float = 1.0,
    max_dd_pct: float = 10.0,
    cost_drag_pct: float = 1.0,
    annualized_return_pct: float = 10.0,
    rolling: list[RollingWindowMetrics] | None = None,
    sortino: float | None = None,
    profit_factor: float | None = 1.5,
) -> MetricsReport:
    """Build a minimal MetricsReport for unit testing."""
    return MetricsReport(
        total_return_pct=annualized_return_pct,
        annualized_return_pct=annualized_return_pct,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_duration_days=10,
        max_drawdown_recovery_days=5,
        calmar_ratio=None,
        volatility_annual_pct=15.0,
        total_trades=20,
        win_rate=55.0,
        profit_factor=profit_factor,
        avg_trade_pnl=10.0,
        avg_winner=20.0,
        avg_loser=-10.0,
        best_trade=100.0,
        worst_trade=-50.0,
        max_consecutive_wins=3,
        max_consecutive_losses=2,
        total_costs=100.0,
        total_taxes=20.0,
        cost_drag_pct=cost_drag_pct,
        turnover_ratio=1.0,
        exposure_pct=80.0,
        equity_curve=[],
        drawdown_curve=[],
        rolling_metrics=rolling or [],
    )


class TestWeightsValidation:
    def test_default_weights_sum_to_one(self):
        w = EvaluationWeights()
        assert math.isclose(
            w.risk_adjusted_return
            + w.drawdown_control
            + w.consistency
            + w.cost_efficiency
            + w.raw_return,
            1.0,
        )

    def test_weights_must_sum_to_one(self):
        with pytest.raises(StrategyEvaluatorError):
            EvaluationWeights(
                risk_adjusted_return=0.5,
                drawdown_control=0.5,
                consistency=0.5,
                cost_efficiency=0.0,
                raw_return=0.0,
            )

    def test_negative_weight_rejected(self):
        with pytest.raises(StrategyEvaluatorError):
            EvaluationWeights(
                risk_adjusted_return=-0.1,
                drawdown_control=0.4,
                consistency=0.2,
                cost_efficiency=0.2,
                raw_return=0.3,
            )

    def test_nan_weight_rejected(self):
        with pytest.raises(StrategyEvaluatorError):
            EvaluationWeights(
                risk_adjusted_return=float("nan"),
                drawdown_control=0.25,
                consistency=0.25,
                cost_efficiency=0.25,
                raw_return=0.25,
            )


class TestRiskAdjustedReturnDimension:
    def test_high_sharpe_scores_high(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(sharpe=3.0))
        assert (
            out.dimensions[EvaluationDimension.RISK_ADJUSTED_RETURN]
            >= 90.0
        )

    def test_zero_sharpe_scores_around_50(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(sharpe=0.0))
        assert (
            45.0
            <= out.dimensions[EvaluationDimension.RISK_ADJUSTED_RETURN]
            <= 55.0
        )

    def test_negative_sharpe_scores_below_50(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(sharpe=-1.5))
        assert out.dimensions[EvaluationDimension.RISK_ADJUSTED_RETURN] < 30.0


class TestDrawdownControlDimension:
    def test_zero_drawdown_scores_perfect(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(max_dd_pct=0.0))
        assert out.dimensions[EvaluationDimension.DRAWDOWN_CONTROL] == 100.0

    def test_severe_drawdown_scores_low(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(max_dd_pct=80.0))
        assert out.dimensions[EvaluationDimension.DRAWDOWN_CONTROL] <= 10.0

    def test_moderate_drawdown_scores_middle(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(max_dd_pct=20.0))
        score = out.dimensions[EvaluationDimension.DRAWDOWN_CONTROL]
        assert 50.0 <= score <= 80.0


class TestCostEfficiencyDimension:
    def test_zero_cost_drag_scores_perfect(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(cost_drag_pct=0.0))
        assert out.dimensions[EvaluationDimension.COST_EFFICIENCY] == 100.0

    def test_high_cost_drag_scores_low(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(cost_drag_pct=20.0))
        assert out.dimensions[EvaluationDimension.COST_EFFICIENCY] <= 5.0


class TestConsistencyDimension:
    def test_no_rolling_metrics_yields_neutral_score(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(rolling=[]))
        assert out.dimensions[EvaluationDimension.CONSISTENCY] == 50.0

    def test_steady_rolling_sharpe_scores_high(self):
        ev = StrategyEvaluator()
        rolling = [
            RollingWindowMetrics(
                window_days=30,
                sharpe_ratio=1.5,
                sortino_ratio=None,
                volatility_annual_pct=12.0,
                max_drawdown_pct=5.0,
            )
            for _ in range(10)
        ]
        out = ev.evaluate(_report(rolling=rolling))
        assert out.dimensions[EvaluationDimension.CONSISTENCY] >= 80.0

    def test_volatile_rolling_sharpe_scores_low(self):
        ev = StrategyEvaluator()
        rolling = [
            RollingWindowMetrics(
                window_days=30,
                sharpe_ratio=v,
                sortino_ratio=None,
                volatility_annual_pct=12.0,
                max_drawdown_pct=5.0,
            )
            for v in [3.0, -2.0, 4.0, -1.5, 2.5, -2.5, 3.5, -3.0]
        ]
        out = ev.evaluate(_report(rolling=rolling))
        assert out.dimensions[EvaluationDimension.CONSISTENCY] <= 40.0


class TestRawReturnDimension:
    def test_high_return_scores_high(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(annualized_return_pct=50.0))
        assert out.dimensions[EvaluationDimension.RAW_RETURN] >= 90.0

    def test_zero_return_scores_around_50(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(annualized_return_pct=0.0))
        assert 40.0 <= out.dimensions[EvaluationDimension.RAW_RETURN] <= 60.0

    def test_loss_scores_low(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(annualized_return_pct=-25.0))
        assert out.dimensions[EvaluationDimension.RAW_RETURN] < 30.0


class TestComposite:
    def test_composite_is_weighted_sum_of_dimensions(self):
        ev = StrategyEvaluator(
            EvaluationWeights(
                risk_adjusted_return=0.0,
                drawdown_control=0.0,
                consistency=0.0,
                cost_efficiency=0.0,
                raw_return=1.0,
            )
        )
        out = ev.evaluate(_report(annualized_return_pct=0.0))
        assert math.isclose(
            out.composite_score,
            out.dimensions[EvaluationDimension.RAW_RETURN],
            abs_tol=1e-9,
        )

    def test_composite_bounded_zero_to_hundred(self):
        ev = StrategyEvaluator()
        for sharpe in [-5.0, 0.0, 5.0]:
            out = ev.evaluate(_report(sharpe=sharpe))
            assert 0.0 <= out.composite_score <= 100.0

    def test_result_is_serializable(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report())
        d = out.to_dict()
        assert "composite_score" in d
        assert "dimensions" in d
        assert isinstance(d["dimensions"], dict)


class TestComparison:
    def test_compare_orders_by_composite_descending(self):
        ev = StrategyEvaluator()
        a = ev.evaluate(_report(sharpe=2.5, annualized_return_pct=30.0))
        b = ev.evaluate(_report(sharpe=0.5, annualized_return_pct=5.0))
        ranked = StrategyEvaluator.rank({"alpha": a, "beta": b})
        assert ranked[0][0] == "alpha"
        assert ranked[1][0] == "beta"


class TestEvaluatorErrors:
    def test_negative_max_dd_rejected(self):
        ev = StrategyEvaluator()
        with pytest.raises(StrategyEvaluatorError):
            ev.evaluate(_report(max_dd_pct=-5.0))

    def test_nan_sharpe_rejected(self):
        ev = StrategyEvaluator()
        with pytest.raises(StrategyEvaluatorError):
            ev.evaluate(_report(sharpe=float("nan")))
