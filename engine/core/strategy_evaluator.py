"""Cross-strategy composite scoring (gh#8).

Takes a :class:`MetricsReport` and produces a single composite score in
[0, 100] plus a per-dimension breakdown, a letter grade, and a list of
warnings. Used by:

- The marketplace to rank strategies in a configurable but principled
  way (a 20%-return / 40%-drawdown strategy should not outrank a
  12%-return / 5%-drawdown one).
- A/B testing surfaces to summarise a comparison in one number.
- The backtest summary endpoint to ship the score alongside the raw
  metrics so the UI doesn't have to re-derive it.

Six dimensions, each normalised to [0, 100]:

- ``RISK_ADJUSTED_RETURN`` — Sharpe ratio, piecewise mapping per spec.
- ``DRAWDOWN_CONTROL`` — Max drawdown, piecewise mapping per spec.
- ``CONSISTENCY`` — coefficient of variation of rolling-window sharpe
  ratios. 50.0 if fewer than two windows so short backtests are not
  penalised.
- ``COST_EFFICIENCY`` — exponential decay on ``cost_drag_pct``.
- ``WIN_RATE_QUALITY`` — ``win_rate * (avg_winner / abs(avg_loser))``.
- ``STABILITY`` — annual volatility, piecewise mapping per spec.

Default weights mirror the spec (sum to 1.0): risk-adjusted 0.30,
drawdown 0.20, consistency 0.15, cost 0.15, win-rate-quality 0.10,
stability 0.10. Weights are user-overridable via
:class:`EvaluationWeights`.

Numeric guards: NaN / inf inputs are rejected at evaluation time so a
junk metrics report cannot silently land a NaN composite score in the
marketplace ranking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engine.core.metrics import MetricsReport


class StrategyEvaluatorError(ValueError):
    """Bad evaluator configuration or invalid metrics input."""


class EvaluationDimension(StrEnum):
    RISK_ADJUSTED_RETURN = "risk_adjusted_return"
    DRAWDOWN_CONTROL = "drawdown_control"
    CONSISTENCY = "consistency"
    COST_EFFICIENCY = "cost_efficiency"
    WIN_RATE_QUALITY = "win_rate_quality"
    STABILITY = "stability"


_GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (90.0, "A+"),
    (80.0, "A"),
    (70.0, "B+"),
    (60.0, "B"),
    (50.0, "C+"),
    (40.0, "C"),
    (25.0, "D"),
    (0.0, "F"),
)


def _require_finite(value: float, label: str) -> None:
    if not math.isfinite(value):
        raise StrategyEvaluatorError(f"{label} must be finite, got {value!r}")


def _piecewise_linear(value: float, breakpoints: list[tuple[float, float]]) -> float:
    """Map ``value`` onto a score using piecewise-linear interpolation
    between sorted ``breakpoints`` of ``(input, output)``. Values
    outside the bracketed range clamp to the nearest endpoint."""
    if value <= breakpoints[0][0]:
        return breakpoints[0][1]
    if value >= breakpoints[-1][0]:
        return breakpoints[-1][1]
    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if x0 <= value <= x1:
            t = (value - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return breakpoints[-1][1]


def _risk_adjusted_score(sharpe: float) -> float:
    """Spec: <0 -> 0, 0->0, 0.5->30, 1.0->60, 2.0->90, 3.0+->100."""
    if sharpe < 0:
        return 0.0
    return _piecewise_linear(
        sharpe,
        [(0.0, 0.0), (0.5, 30.0), (1.0, 60.0), (2.0, 90.0), (3.0, 100.0)],
    )


def _drawdown_score(max_dd_pct: float) -> float:
    """Spec: 0->100, 5->90, 10->70, 20->40, 30->20, >30 falls toward 0."""
    if max_dd_pct < 0:
        raise StrategyEvaluatorError(f"max_drawdown_pct must be >= 0, got {max_dd_pct}")
    return _piecewise_linear(
        max_dd_pct,
        [
            (0.0, 100.0),
            (5.0, 90.0),
            (10.0, 70.0),
            (20.0, 40.0),
            (30.0, 20.0),
            (50.0, 0.0),
        ],
    )


def _cost_efficiency_score(cost_drag_pct: float) -> float:
    """Exponential decay on cost_drag_pct, half-life 4%. 0% -> 100,
    4% -> 50, 20% -> ~3."""
    if cost_drag_pct < 0:
        raise StrategyEvaluatorError(f"cost_drag_pct must be >= 0, got {cost_drag_pct}")
    return 100.0 * math.pow(0.5, cost_drag_pct / 4.0)


def _win_rate_quality_score(win_rate: float, avg_winner: float, avg_loser: float) -> float:
    """``quality = win_rate * (avg_winner / abs(avg_loser))``,
    normalised to [0, 100]. ``win_rate`` is treated as a fraction in
    [0, 1] if <= 1.0, else as a percentage in [0, 100]."""
    wr = win_rate / 100.0 if win_rate > 1.0 else win_rate
    if wr < 0 or wr > 1:
        raise StrategyEvaluatorError(f"win_rate must be in [0, 1] or [0, 100], got {win_rate}")
    if avg_loser == 0:
        return 50.0 if wr == 0 else 100.0
    quality = wr * (avg_winner / abs(avg_loser))
    return _piecewise_linear(
        max(0.0, quality),
        [(0.0, 0.0), (0.5, 25.0), (1.0, 50.0), (2.0, 80.0), (3.0, 100.0)],
    )


def _stability_score(volatility_annual_pct: float) -> float:
    """Spec: <10% -> 80-100, 10-20% -> 50-80, 20-30% -> 20-50, >30 -> 0-20."""
    if volatility_annual_pct < 0:
        raise StrategyEvaluatorError(
            f"volatility_annual_pct must be >= 0, got {volatility_annual_pct}"
        )
    return _piecewise_linear(
        volatility_annual_pct,
        [
            (0.0, 100.0),
            (10.0, 80.0),
            (20.0, 50.0),
            (30.0, 20.0),
            (50.0, 0.0),
        ],
    )


@dataclass(frozen=True)
class EvaluationWeights:
    risk_adjusted_return: float = 0.30
    drawdown_control: float = 0.20
    consistency: float = 0.15
    cost_efficiency: float = 0.15
    win_rate_quality: float = 0.10
    stability: float = 0.10

    def __post_init__(self) -> None:
        for label, value in self.as_mapping().items():
            _require_finite(value, f"weight {label}")
            if value < 0:
                raise StrategyEvaluatorError(f"weight {label} must be >= 0, got {value}")
        total = sum(self.as_mapping().values())
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise StrategyEvaluatorError(f"EvaluationWeights must sum to 1.0, got {total}")

    def as_mapping(self) -> dict[str, float]:
        return {
            "risk_adjusted_return": self.risk_adjusted_return,
            "drawdown_control": self.drawdown_control,
            "consistency": self.consistency,
            "cost_efficiency": self.cost_efficiency,
            "win_rate_quality": self.win_rate_quality,
            "stability": self.stability,
        }


def _grade_for(score: float) -> str:
    for threshold, grade in _GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


@dataclass(frozen=True)
class EvaluationResult:
    composite_score: float
    grade: str
    dimensions: Mapping[EvaluationDimension, float]
    warnings: list[str] = field(default_factory=list)
    weights: EvaluationWeights = field(default_factory=EvaluationWeights)
    percentile: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "composite_score": self.composite_score,
            "grade": self.grade,
            "dimensions": {d.value: v for d, v in self.dimensions.items()},
            "warnings": list(self.warnings),
            "weights": self.weights.as_mapping(),
            "percentile": self.percentile,
        }


_WARN_NEGATIVE_SHARPE = "Negative Sharpe ratio"
_WARN_EXCESSIVE_DRAWDOWN = "Excessive drawdown"
_WARN_HIGH_COST_DRAG = "High cost drag"
_WARN_HIGH_VOLATILITY = "High volatility"
_WARN_POOR_WIN_QUALITY = "Poor win/loss profile"


def _build_warnings(report: MetricsReport) -> list[str]:
    warnings: list[str] = []
    if report.sharpe_ratio < 0:
        warnings.append(_WARN_NEGATIVE_SHARPE)
    if report.max_drawdown_pct >= 20.0:
        warnings.append(_WARN_EXCESSIVE_DRAWDOWN)
    if report.cost_drag_pct >= 5.0:
        warnings.append(_WARN_HIGH_COST_DRAG)
    if report.volatility_annual_pct >= 30.0:
        warnings.append(_WARN_HIGH_VOLATILITY)
    if report.avg_loser != 0 and report.total_trades > 0:
        wr = report.win_rate / 100.0 if report.win_rate > 1.0 else report.win_rate
        quality = wr * (report.avg_winner / abs(report.avg_loser))
        if quality < 0.5:
            warnings.append(_WARN_POOR_WIN_QUALITY)
    return warnings


class StrategyEvaluator:
    """Stateless evaluator. Construct once per weight configuration."""

    def __init__(self, weights: EvaluationWeights | None = None) -> None:
        self.weights = weights or EvaluationWeights()

    def evaluate(self, report: MetricsReport) -> EvaluationResult:
        _require_finite(report.sharpe_ratio, "sharpe_ratio")
        _require_finite(report.max_drawdown_pct, "max_drawdown_pct")
        _require_finite(report.cost_drag_pct, "cost_drag_pct")
        _require_finite(report.volatility_annual_pct, "volatility_annual_pct")
        _require_finite(report.win_rate, "win_rate")
        _require_finite(report.avg_winner, "avg_winner")
        _require_finite(report.avg_loser, "avg_loser")

        dims: dict[EvaluationDimension, float] = {
            EvaluationDimension.RISK_ADJUSTED_RETURN: _risk_adjusted_score(report.sharpe_ratio),
            EvaluationDimension.DRAWDOWN_CONTROL: _drawdown_score(report.max_drawdown_pct),
            EvaluationDimension.CONSISTENCY: self._consistency(report),
            EvaluationDimension.COST_EFFICIENCY: _cost_efficiency_score(report.cost_drag_pct),
            EvaluationDimension.WIN_RATE_QUALITY: _win_rate_quality_score(
                report.win_rate, report.avg_winner, report.avg_loser
            ),
            EvaluationDimension.STABILITY: _stability_score(report.volatility_annual_pct),
        }

        weight_map = {
            EvaluationDimension.RISK_ADJUSTED_RETURN: self.weights.risk_adjusted_return,
            EvaluationDimension.DRAWDOWN_CONTROL: self.weights.drawdown_control,
            EvaluationDimension.CONSISTENCY: self.weights.consistency,
            EvaluationDimension.COST_EFFICIENCY: self.weights.cost_efficiency,
            EvaluationDimension.WIN_RATE_QUALITY: self.weights.win_rate_quality,
            EvaluationDimension.STABILITY: self.weights.stability,
        }
        composite = sum(dims[d] * weight_map[d] for d in dims)
        composite = max(0.0, min(100.0, composite))

        return EvaluationResult(
            composite_score=composite,
            grade=_grade_for(composite),
            dimensions=dims,
            warnings=_build_warnings(report),
            weights=self.weights,
        )

    def _consistency(self, report: MetricsReport) -> float:
        sharpes = [r.sharpe_ratio for r in report.rolling_metrics if math.isfinite(r.sharpe_ratio)]
        if len(sharpes) < 2:
            return 50.0
        mean = sum(sharpes) / len(sharpes)
        variance = sum((s - mean) ** 2 for s in sharpes) / len(sharpes)
        std = math.sqrt(variance)
        if std == 0.0:
            return 100.0
        cov = std / max(abs(mean), 0.5)
        return max(0.0, 100.0 - (cov / 4.0) * 100.0)

    @staticmethod
    def rank(
        results: Mapping[str, EvaluationResult],
    ) -> list[tuple[str, EvaluationResult]]:
        """Return ``results`` sorted by composite score, highest first.
        Ties resolved by name to keep the ordering deterministic."""
        return sorted(
            results.items(),
            key=lambda kv: (-kv[1].composite_score, kv[0]),
        )


__all__ = [
    "EvaluationDimension",
    "EvaluationResult",
    "EvaluationWeights",
    "StrategyEvaluator",
    "StrategyEvaluatorError",
]
