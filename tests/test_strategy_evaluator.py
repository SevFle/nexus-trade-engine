"""Tests for engine.core.strategy_evaluator — composite score (gh#8)."""

from __future__ import annotations

import math

import pytest

from engine.core.metrics import MetricsReport, RollingWindowMetrics
from engine.core.strategy_evaluator import (
    EvaluationDimension,
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
    volatility_annual_pct: float = 15.0,
    win_rate: float = 55.0,
    avg_winner: float = 20.0,
    avg_loser: float = -10.0,
    total_trades: int = 20,
    rolling: list[RollingWindowMetrics] | None = None,
    sortino: float | None = None,
    profit_factor: float | None = 1.5,
) -> MetricsReport:
    return MetricsReport(
        total_return_pct=annualized_return_pct,
        annualized_return_pct=annualized_return_pct,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_duration_days=10,
        max_drawdown_recovery_days=5,
        calmar_ratio=None,
        volatility_annual_pct=volatility_annual_pct,
        total_trades=total_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_trade_pnl=10.0,
        avg_winner=avg_winner,
        avg_loser=avg_loser,
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
        assert math.isclose(sum(w.as_mapping().values()), 1.0)

    def test_default_weights_match_spec(self):
        w = EvaluationWeights()
        assert w.risk_adjusted_return == 0.30
        assert w.drawdown_control == 0.20
        assert w.consistency == 0.15
        assert w.cost_efficiency == 0.15
        assert w.win_rate_quality == 0.10
        assert w.stability == 0.10

    def test_weights_must_sum_to_one(self):
        with pytest.raises(StrategyEvaluatorError):
            EvaluationWeights(
                risk_adjusted_return=0.5,
                drawdown_control=0.5,
                consistency=0.5,
                cost_efficiency=0.0,
                win_rate_quality=0.0,
                stability=0.0,
            )

    def test_negative_weight_rejected(self):
        with pytest.raises(StrategyEvaluatorError):
            EvaluationWeights(
                risk_adjusted_return=-0.1,
                drawdown_control=0.4,
                consistency=0.2,
                cost_efficiency=0.2,
                win_rate_quality=0.15,
                stability=0.15,
            )

    def test_nan_weight_rejected(self):
        with pytest.raises(StrategyEvaluatorError):
            EvaluationWeights(
                risk_adjusted_return=float("nan"),
                drawdown_control=0.20,
                consistency=0.20,
                cost_efficiency=0.20,
                win_rate_quality=0.20,
                stability=0.20,
            )


class TestRiskAdjustedReturnDimension:
    def test_high_sharpe_scores_high(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(sharpe=3.0))
        assert out.dimensions[EvaluationDimension.RISK_ADJUSTED_RETURN] >= 90.0

    def test_sharpe_one_scores_around_60(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(sharpe=1.0))
        assert (
            55.0
            <= out.dimensions[EvaluationDimension.RISK_ADJUSTED_RETURN]
            <= 65.0
        )

    def test_negative_sharpe_scores_zero(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(sharpe=-1.5))
        assert out.dimensions[EvaluationDimension.RISK_ADJUSTED_RETURN] == 0.0


class TestDrawdownControlDimension:
    def test_zero_drawdown_scores_perfect(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(max_dd_pct=0.0))
        assert out.dimensions[EvaluationDimension.DRAWDOWN_CONTROL] == 100.0

    def test_severe_drawdown_scores_low(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(max_dd_pct=50.0))
        assert out.dimensions[EvaluationDimension.DRAWDOWN_CONTROL] <= 5.0

    def test_drawdown_20pct_scores_around_40(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(max_dd_pct=20.0))
        score = out.dimensions[EvaluationDimension.DRAWDOWN_CONTROL]
        assert 35.0 <= score <= 45.0


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


class TestWinRateQualityDimension:
    def test_high_quality_scores_high(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(win_rate=60.0, avg_winner=30.0, avg_loser=-10.0)
        )
        assert out.dimensions[EvaluationDimension.WIN_RATE_QUALITY] >= 60.0

    def test_break_even_quality_scores_around_25(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(win_rate=50.0, avg_winner=10.0, avg_loser=-10.0)
        )
        score = out.dimensions[EvaluationDimension.WIN_RATE_QUALITY]
        assert 20.0 <= score <= 35.0

    def test_low_quality_scores_low(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(win_rate=30.0, avg_winner=5.0, avg_loser=-10.0)
        )
        assert out.dimensions[EvaluationDimension.WIN_RATE_QUALITY] <= 15.0

    def test_zero_avg_loser_does_not_divide_by_zero(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(win_rate=80.0, avg_winner=20.0, avg_loser=0.0)
        )
        assert out.dimensions[EvaluationDimension.WIN_RATE_QUALITY] == 100.0


class TestStabilityDimension:
    def test_low_volatility_scores_high(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(volatility_annual_pct=5.0))
        assert out.dimensions[EvaluationDimension.STABILITY] >= 80.0

    def test_high_volatility_scores_low(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(volatility_annual_pct=45.0))
        assert out.dimensions[EvaluationDimension.STABILITY] <= 20.0

    def test_moderate_volatility_scores_middle(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(volatility_annual_pct=20.0))
        score = out.dimensions[EvaluationDimension.STABILITY]
        assert 40.0 <= score <= 60.0


class TestComposite:
    def test_composite_is_weighted_sum_of_dimensions(self):
        ev = StrategyEvaluator(
            EvaluationWeights(
                risk_adjusted_return=1.0,
                drawdown_control=0.0,
                consistency=0.0,
                cost_efficiency=0.0,
                win_rate_quality=0.0,
                stability=0.0,
            )
        )
        out = ev.evaluate(_report(sharpe=2.0))
        assert math.isclose(
            out.composite_score,
            out.dimensions[EvaluationDimension.RISK_ADJUSTED_RETURN],
            abs_tol=1e-9,
        )

    def test_composite_bounded_zero_to_hundred(self):
        ev = StrategyEvaluator()
        for sharpe in [-5.0, 0.0, 5.0]:
            out = ev.evaluate(_report(sharpe=sharpe))
            assert 0.0 <= out.composite_score <= 100.0

    def test_perfect_strategy_scores_above_85(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(
                sharpe=3.0,
                max_dd_pct=2.0,
                cost_drag_pct=0.5,
                volatility_annual_pct=8.0,
                win_rate=65.0,
                avg_winner=30.0,
                avg_loser=-10.0,
            )
        )
        assert out.composite_score >= 85.0

    def test_terrible_strategy_scores_below_25(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(
                sharpe=-1.0,
                max_dd_pct=50.0,
                cost_drag_pct=15.0,
                volatility_annual_pct=45.0,
                win_rate=30.0,
                avg_winner=5.0,
                avg_loser=-15.0,
            )
        )
        assert out.composite_score < 25.0


class TestGrade:
    def test_a_plus_for_score_above_90(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(
                sharpe=3.0,
                max_dd_pct=1.0,
                cost_drag_pct=0.1,
                volatility_annual_pct=5.0,
                win_rate=70.0,
                avg_winner=40.0,
                avg_loser=-10.0,
            )
        )
        assert out.grade == "A+"

    def test_f_for_score_below_25(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(
                sharpe=-2.0,
                max_dd_pct=60.0,
                cost_drag_pct=20.0,
                volatility_annual_pct=50.0,
                win_rate=20.0,
                avg_winner=2.0,
                avg_loser=-20.0,
            )
        )
        assert out.grade == "F"

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (95.0, "A+"),
            (85.0, "A"),
            (75.0, "B+"),
            (65.0, "B"),
            (55.0, "C+"),
            (45.0, "C"),
            (30.0, "D"),
            (10.0, "F"),
        ],
    )
    def test_grade_thresholds_inclusive_lower_bound(self, score, expected):
        from engine.core.strategy_evaluator import _grade_for

        assert _grade_for(score) == expected


class TestWarnings:
    def test_negative_sharpe_warning(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(sharpe=-0.5))
        assert any("Sharpe" in w for w in out.warnings)

    def test_excessive_drawdown_warning(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(max_dd_pct=35.0))
        assert any("drawdown" in w.lower() for w in out.warnings)

    def test_high_cost_drag_warning(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(cost_drag_pct=8.0))
        assert any("cost" in w.lower() for w in out.warnings)

    def test_high_volatility_warning(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report(volatility_annual_pct=40.0))
        assert any("volatility" in w.lower() for w in out.warnings)

    def test_clean_strategy_no_warnings(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(
            _report(
                sharpe=2.0,
                max_dd_pct=8.0,
                cost_drag_pct=1.0,
                volatility_annual_pct=12.0,
                win_rate=60.0,
                avg_winner=20.0,
                avg_loser=-10.0,
            )
        )
        assert out.warnings == []


class TestSerialization:
    def test_result_to_dict_has_required_keys(self):
        ev = StrategyEvaluator()
        out = ev.evaluate(_report())
        d = out.to_dict()
        assert "composite_score" in d
        assert "grade" in d
        assert "dimensions" in d
        assert "warnings" in d
        assert "weights" in d
        assert "percentile" in d
        assert isinstance(d["dimensions"], dict)
        assert isinstance(d["warnings"], list)


class TestRanking:
    def test_compare_orders_by_composite_descending(self):
        ev = StrategyEvaluator()
        a = ev.evaluate(
            _report(
                sharpe=2.5,
                max_dd_pct=3.0,
                volatility_annual_pct=8.0,
                win_rate=65.0,
            )
        )
        b = ev.evaluate(
            _report(
                sharpe=0.2,
                max_dd_pct=25.0,
                volatility_annual_pct=30.0,
                win_rate=40.0,
            )
        )
        ranked = StrategyEvaluator.rank({"alpha": a, "beta": b})
        assert ranked[0][0] == "alpha"
        assert ranked[1][0] == "beta"

    def test_tie_resolved_by_name(self):
        ev = StrategyEvaluator()
        r = ev.evaluate(_report())
        ranked = StrategyEvaluator.rank({"zebra": r, "alpha": r})
        assert ranked[0][0] == "alpha"
        assert ranked[1][0] == "zebra"


class TestEvaluatorErrors:
    def test_negative_max_dd_rejected(self):
        ev = StrategyEvaluator()
        with pytest.raises(StrategyEvaluatorError):
            ev.evaluate(_report(max_dd_pct=-5.0))

    def test_nan_sharpe_rejected(self):
        ev = StrategyEvaluator()
        with pytest.raises(StrategyEvaluatorError):
            ev.evaluate(_report(sharpe=float("nan")))

    def test_inf_volatility_rejected(self):
        ev = StrategyEvaluator()
        with pytest.raises(StrategyEvaluatorError):
            ev.evaluate(_report(volatility_annual_pct=float("inf")))

    def test_negative_volatility_rejected(self):
        ev = StrategyEvaluator()
        with pytest.raises(StrategyEvaluatorError):
            ev.evaluate(_report(volatility_annual_pct=-1.0))
