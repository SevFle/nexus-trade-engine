"""Extended edge-case tests for engine.core.strategy_evaluator."""

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
    _build_warnings,
    _cost_efficiency_score,
    _drawdown_score,
    _grade_for,
    _piecewise_linear,
    _risk_adjusted_score,
    _stability_score,
    _win_rate_quality_score,
)


def _report(
    *,
    sharpe: float = 1.0,
    max_dd_pct: float = 10.0,
    cost_drag_pct: float = 1.0,
    volatility: float = 15.0,
    win_rate: float = 55.0,
    avg_winner: float = 20.0,
    avg_loser: float = -10.0,
    total_trades: int = 20,
    rolling: list[RollingWindowMetrics] | None = None,
) -> MetricsReport:
    return MetricsReport(
        total_return_pct=10.0,
        annualized_return_pct=10.0,
        sharpe_ratio=sharpe,
        sortino_ratio=None,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_duration_days=10,
        max_drawdown_recovery_days=5,
        calmar_ratio=None,
        volatility_annual_pct=volatility,
        total_trades=total_trades,
        win_rate=win_rate,
        profit_factor=1.5,
        avg_trade_pnl=10.0,
        avg_winner=avg_winner,
        avg_loser=avg_loser,
        best_trade=100.0,
        worst_trade=-50.0,
        max_consecutive_wins=5,
        max_consecutive_losses=3,
        total_costs=100.0,
        total_taxes=20.0,
        cost_drag_pct=cost_drag_pct,
        turnover_ratio=2.0,
        exposure_pct=80.0,
        rolling_metrics=rolling or [],
    )


class TestStrEnumBehavior:
    def test_risk_adjusted_return_string_value(self):
        assert EvaluationDimension.RISK_ADJUSTED_RETURN == "risk_adjusted_return"

    def test_all_dimensions_are_strings(self):
        for dim in EvaluationDimension:
            assert isinstance(dim, str)
            assert isinstance(dim.value, str)


class TestPiecewiseLinearEdgeCases:
    def test_below_range_clamps_to_first(self):
        assert _piecewise_linear(-1.0, [(0.0, 10.0), (1.0, 20.0)]) == 10.0

    def test_above_range_clamps_to_last(self):
        assert _piecewise_linear(5.0, [(0.0, 10.0), (1.0, 20.0)]) == 20.0

    def test_exact_at_breakpoint(self):
        assert _piecewise_linear(0.0, [(0.0, 10.0), (1.0, 20.0)]) == 10.0

    def test_midpoint_interpolation(self):
        result = _piecewise_linear(0.5, [(0.0, 0.0), (1.0, 100.0)])
        assert abs(result - 50.0) < 1e-9

    def test_multiple_breakpoints(self):
        bps = [(0.0, 0.0), (1.0, 50.0), (2.0, 100.0)]
        assert abs(_piecewise_linear(0.5, bps) - 25.0) < 1e-9
        assert abs(_piecewise_linear(1.5, bps) - 75.0) < 1e-9


class TestRiskAdjustedScore:
    def test_negative_sharpe(self):
        assert _risk_adjusted_score(-1.0) == 0.0

    def test_zero_sharpe(self):
        assert _risk_adjusted_score(0.0) == 0.0

    def test_high_sharpe_clamps(self):
        assert _risk_adjusted_score(5.0) == 100.0


class TestDrawdownScore:
    def test_zero_drawdown(self):
        assert _drawdown_score(0.0) == 100.0

    def test_extreme_drawdown(self):
        assert _drawdown_score(50.0) == 0.0

    def test_negative_drawdown_raises(self):
        with pytest.raises(StrategyEvaluatorError):
            _drawdown_score(-1.0)


class TestCostEfficiencyScore:
    def test_zero_cost(self):
        assert _cost_efficiency_score(0.0) == 100.0

    def test_negative_cost_raises(self):
        with pytest.raises(StrategyEvaluatorError):
            _cost_efficiency_score(-1.0)

    def test_high_cost_decays(self):
        result = _cost_efficiency_score(20.0)
        assert result < 5.0


class TestWinRateQualityScore:
    def test_win_rate_as_percentage(self):
        result = _win_rate_quality_score(55.0, 20.0, -10.0)
        assert 0.0 <= result <= 100.0

    def test_win_rate_as_fraction(self):
        result = _win_rate_quality_score(0.55, 20.0, -10.0)
        assert 0.0 <= result <= 100.0

    def test_zero_avg_loser_zero_win_rate(self):
        assert _win_rate_quality_score(0.0, 20.0, 0.0) == 50.0

    def test_zero_avg_loser_positive_win_rate(self):
        assert _win_rate_quality_score(0.5, 20.0, 0.0) == 100.0

    def test_invalid_win_rate_raises(self):
        with pytest.raises(StrategyEvaluatorError, match="win_rate"):
            _win_rate_quality_score(150.0, 20.0, -10.0)


class TestStabilityScore:
    def test_zero_volatility(self):
        assert _stability_score(0.0) == 100.0

    def test_high_volatility(self):
        assert _stability_score(50.0) == 0.0

    def test_negative_volatility_raises(self):
        with pytest.raises(StrategyEvaluatorError):
            _stability_score(-1.0)


class TestGradeFor:
    @pytest.mark.parametrize(
        "score,grade",
        [
            (95.0, "A+"),
            (90.0, "A+"),
            (89.9, "A"),
            (80.0, "A"),
            (79.9, "B+"),
            (70.0, "B+"),
            (69.9, "B"),
            (60.0, "B"),
            (59.9, "C+"),
            (50.0, "C+"),
            (49.9, "C"),
            (40.0, "C"),
            (39.9, "D"),
            (25.0, "D"),
            (24.9, "F"),
            (0.0, "F"),
        ],
    )
    def test_grade_boundaries(self, score, grade):
        assert _grade_for(score) == grade


class TestEvaluationWeights:
    def test_default_weights_sum_to_one(self):
        w = EvaluationWeights()
        assert abs(sum(w.as_mapping().values()) - 1.0) < 1e-9

    def test_negative_weight_raises(self):
        with pytest.raises(StrategyEvaluatorError, match=">= 0"):
            EvaluationWeights(risk_adjusted_return=-0.1)

    def test_weights_not_summing_to_one_raises(self):
        with pytest.raises(StrategyEvaluatorError, match="sum to 1.0"):
            EvaluationWeights(risk_adjusted_return=0.5)

    def test_nan_weight_raises(self):
        with pytest.raises(StrategyEvaluatorError, match="finite"):
            EvaluationWeights(risk_adjusted_return=float("nan"))

    def test_inf_weight_raises(self):
        with pytest.raises(StrategyEvaluatorError, match="finite"):
            EvaluationWeights(risk_adjusted_return=float("inf"))


class TestEvaluatorConsistency:
    def test_fewer_than_two_rolling_metrics_returns_50(self):
        rolling = [
            RollingWindowMetrics(
                window_days=30,
                sharpe_ratio=1.0,
                sortino_ratio=None,
                volatility_annual_pct=15.0,
                max_drawdown_pct=5.0,
            )
        ]
        report = _report(rolling=rolling)
        evaluator = StrategyEvaluator()
        result = evaluator.evaluate(report)
        assert result.dimensions[EvaluationDimension.CONSISTENCY] == 50.0

    def test_identical_sharpes_returns_100(self):
        rolling = [
            RollingWindowMetrics(
                window_days=30,
                sharpe_ratio=1.5,
                sortino_ratio=None,
                volatility_annual_pct=15.0,
                max_drawdown_pct=5.0,
            )
            for _ in range(5)
        ]
        report = _report(rolling=rolling)
        evaluator = StrategyEvaluator()
        result = evaluator.evaluate(report)
        assert result.dimensions[EvaluationDimension.CONSISTENCY] == 100.0


class TestEvaluatorNaNRejection:
    def test_nan_sharpe_raises(self):
        report = _report(sharpe=float("nan"))
        with pytest.raises(StrategyEvaluatorError, match="finite"):
            StrategyEvaluator().evaluate(report)

    def test_inf_drawdown_raises(self):
        report = _report(max_dd_pct=float("inf"))
        with pytest.raises(StrategyEvaluatorError, match="finite"):
            StrategyEvaluator().evaluate(report)


class TestBuildWarnings:
    def test_negative_sharpe_warning(self):
        report = _report(sharpe=-0.5)
        warnings = _build_warnings(report)
        assert any("Negative Sharpe" in w for w in warnings)

    def test_excessive_drawdown_warning(self):
        report = _report(max_dd_pct=25.0)
        warnings = _build_warnings(report)
        assert any("Excessive drawdown" in w for w in warnings)

    def test_high_cost_drag_warning(self):
        report = _report(cost_drag_pct=6.0)
        warnings = _build_warnings(report)
        assert any("High cost drag" in w for w in warnings)

    def test_high_volatility_warning(self):
        report = _report(volatility=35.0)
        warnings = _build_warnings(report)
        assert any("High volatility" in w for w in warnings)

    def test_no_warnings_for_good_report(self):
        report = _report(sharpe=2.0, max_dd_pct=5.0, cost_drag_pct=1.0, volatility=10.0)
        warnings = _build_warnings(report)
        assert len(warnings) == 0


class TestEvaluationResultToDict:
    def test_to_dict_keys(self):
        report = _report()
        result = StrategyEvaluator().evaluate(report)
        d = result.to_dict()
        assert "composite_score" in d
        assert "grade" in d
        assert "dimensions" in d
        assert "warnings" in d
        assert "weights" in d
        assert "percentile" in d

    def test_dimensions_serialized_as_strings(self):
        report = _report()
        result = StrategyEvaluator().evaluate(report)
        d = result.to_dict()
        for key in d["dimensions"]:
            assert isinstance(key, str)


class TestRank:
    def test_rank_empty_returns_empty(self):
        result = StrategyEvaluator.rank({})
        assert result == []

    def test_rank_sorted_by_composite(self):
        report_good = _report(sharpe=3.0)
        report_bad = _report(sharpe=-1.0)
        results = {
            "good": StrategyEvaluator().evaluate(report_good),
            "bad": StrategyEvaluator().evaluate(report_bad),
        }
        ranked = StrategyEvaluator.rank(results)
        assert ranked[0][0] == "good"
        assert ranked[1][0] == "bad"

    def test_rank_tiebreak_by_name(self):
        report = _report()
        result = StrategyEvaluator().evaluate(report)
        results = {"alpha": result, "beta": result}
        ranked = StrategyEvaluator.rank(results)
        assert ranked[0][0] == "alpha"
        assert ranked[1][0] == "beta"
