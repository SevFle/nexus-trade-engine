"""Cross-strategy composite scoring (gh#8).

Takes a :class:`MetricsReport` (the output of
:class:`engine.core.metrics.PerformanceMetrics`) and produces a single
composite score in [0, 100] plus a per-dimension breakdown. Used by:

- The marketplace to rank strategies in a configurable but principled
  way (a 20%-return / 40%-drawdown strategy should not outrank a
  12%-return / 5%-drawdown strategy).
- A/B testing surfaces to summarise a comparison in one number.
- The backtest summary endpoint to ship the score alongside the raw
  metrics so the UI doesn't have to re-derive it.

Dimensions and their inputs:

- ``RISK_ADJUSTED_RETURN`` — sharpe ratio, mapped via a logistic so a
  sharpe of 0 -> 50 and a sharpe of 3 -> ~95.
- ``DRAWDOWN_CONTROL`` — ``max_drawdown_pct`` inverted: 0% drawdown ->
  100, 25% -> 50, 100% -> ~0.
- ``CONSISTENCY`` — coefficient of variation of rolling-window sharpe
  ratios. A strategy with steady risk-adjusted returns scores high; a
  whippy one scores low. Returns 50.0 when fewer than two rolling
  windows are available so this dimension does not penalise short
  backtests.
- ``COST_EFFICIENCY`` — ``cost_drag_pct`` inverted: 0% drag -> 100,
  5% -> 50, 30% -> ~0.
- ``RAW_RETURN`` — annualised return mapped via a logistic so 0% ->
  50 and 30% -> ~95.

The composite is a weighted sum; weights default to a risk-aware mix
(30% risk-adjusted, 25% drawdown, 15% consistency, 15% cost, 15% raw
return) and are user-overridable via :class:`EvaluationWeights`.

Numeric guards: NaN / inf inputs are rejected at evaluation time so a
junk metrics report cannot silently land a NaN composite score in the
marketplace ranking.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from engine.core.metrics import MetricsReport


class StrategyEvaluatorError(ValueError):
    """Bad evaluator configuration or invalid metrics input."""


class EvaluationDimension(str, Enum):
    RISK_ADJUSTED_RETURN = "risk_adjusted_return"
    DRAWDOWN_CONTROL = "drawdown_control"
    CONSISTENCY = "consistency"
    COST_EFFICIENCY = "cost_efficiency"
    RAW_RETURN = "raw_return"


def _require_finite(value: float, label: str) -> None:
    if not math.isfinite(value):
        raise StrategyEvaluatorError(f"{label} must be finite, got {value!r}")


def _logistic(value: float, *, midpoint: float, scale: float) -> float:
    """Map ``value`` onto [0, 100] via a logistic so ``midpoint`` -> 50.
    ``scale`` is the unit of "one logit" — larger scale flattens the
    curve, smaller scale makes it sharper."""
    if scale <= 0:
        raise StrategyEvaluatorError(
            f"logistic scale must be positive, got {scale}"
        )
    z = (value - midpoint) / scale
    if z > 50:
        return 100.0
    if z < -50:
        return 0.0
    return 100.0 / (1.0 + math.exp(-z))


def _inverse_pct_score(pct: float, *, half_life: float) -> float:
    """For ``pct`` (a percentage where 0 is best and 100 is worst),
    return 100 at 0% and decay exponentially. ``half_life`` is the
    input value at which the output drops to 50."""
    if pct < 0:
        raise StrategyEvaluatorError(
            f"inverse-pct input must be non-negative, got {pct}"
        )
    if half_life <= 0:
        raise StrategyEvaluatorError(
            f"half_life must be positive, got {half_life}"
        )
    return 100.0 * math.pow(0.5, pct / half_life)


@dataclass(frozen=True)
class EvaluationWeights:
    risk_adjusted_return: float = 0.30
    drawdown_control: float = 0.25
    consistency: float = 0.15
    cost_efficiency: float = 0.15
    raw_return: float = 0.15

    def __post_init__(self) -> None:
        for label, value in self.as_mapping().items():
            _require_finite(value, f"weight {label}")
            if value < 0:
                raise StrategyEvaluatorError(
                    f"weight {label} must be >= 0, got {value}"
                )
        total = sum(self.as_mapping().values())
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise StrategyEvaluatorError(
                f"EvaluationWeights must sum to 1.0, got {total}"
            )

    def as_mapping(self) -> dict[str, float]:
        return {
            "risk_adjusted_return": self.risk_adjusted_return,
            "drawdown_control": self.drawdown_control,
            "consistency": self.consistency,
            "cost_efficiency": self.cost_efficiency,
            "raw_return": self.raw_return,
        }


@dataclass(frozen=True)
class EvaluationResult:
    composite_score: float
    dimensions: Mapping[EvaluationDimension, float]
    weights: EvaluationWeights = field(default_factory=EvaluationWeights)

    def to_dict(self) -> dict[str, object]:
        return {
            "composite_score": self.composite_score,
            "dimensions": {d.value: v for d, v in self.dimensions.items()},
            "weights": self.weights.as_mapping(),
        }


class StrategyEvaluator:
    """Stateless evaluator. Construct once per weight configuration."""

    # Tuning constants kept adjacent so future calibration happens in
    # one place rather than scattered through the dimension functions.
    _SHARPE_MIDPOINT = 0.0
    _SHARPE_SCALE = 1.0  # +1 sharpe shifts the score by ~22 points.

    _RETURN_MIDPOINT = 0.0
    _RETURN_SCALE = 10.0  # +10% return shifts the score by ~22 points.

    _DRAWDOWN_HALF_LIFE = 22.0  # 22% max-DD -> 50; 80% -> ~8.
    _COST_HALF_LIFE = 4.0  # 4% cost drag -> 50; 20% -> ~3.

    def __init__(self, weights: EvaluationWeights | None = None) -> None:
        self.weights = weights or EvaluationWeights()

    def evaluate(self, report: MetricsReport) -> EvaluationResult:
        _require_finite(report.sharpe_ratio, "sharpe_ratio")
        _require_finite(report.max_drawdown_pct, "max_drawdown_pct")
        _require_finite(report.cost_drag_pct, "cost_drag_pct")
        _require_finite(
            report.annualized_return_pct, "annualized_return_pct"
        )
        if report.max_drawdown_pct < 0:
            raise StrategyEvaluatorError(
                f"max_drawdown_pct must be >= 0, got {report.max_drawdown_pct}"
            )
        if report.cost_drag_pct < 0:
            raise StrategyEvaluatorError(
                f"cost_drag_pct must be >= 0, got {report.cost_drag_pct}"
            )

        dims: dict[EvaluationDimension, float] = {
            EvaluationDimension.RISK_ADJUSTED_RETURN: _logistic(
                report.sharpe_ratio,
                midpoint=self._SHARPE_MIDPOINT,
                scale=self._SHARPE_SCALE,
            ),
            EvaluationDimension.DRAWDOWN_CONTROL: _inverse_pct_score(
                report.max_drawdown_pct, half_life=self._DRAWDOWN_HALF_LIFE
            ),
            EvaluationDimension.CONSISTENCY: self._consistency(report),
            EvaluationDimension.COST_EFFICIENCY: _inverse_pct_score(
                report.cost_drag_pct, half_life=self._COST_HALF_LIFE
            ),
            EvaluationDimension.RAW_RETURN: _logistic(
                report.annualized_return_pct,
                midpoint=self._RETURN_MIDPOINT,
                scale=self._RETURN_SCALE,
            ),
        }

        weight_map = {
            EvaluationDimension.RISK_ADJUSTED_RETURN: self.weights.risk_adjusted_return,
            EvaluationDimension.DRAWDOWN_CONTROL: self.weights.drawdown_control,
            EvaluationDimension.CONSISTENCY: self.weights.consistency,
            EvaluationDimension.COST_EFFICIENCY: self.weights.cost_efficiency,
            EvaluationDimension.RAW_RETURN: self.weights.raw_return,
        }
        composite = sum(dims[d] * weight_map[d] for d in dims)

        return EvaluationResult(
            composite_score=max(0.0, min(100.0, composite)),
            dimensions=dims,
            weights=self.weights,
        )

    def _consistency(self, report: MetricsReport) -> float:
        sharpes = [
            r.sharpe_ratio
            for r in report.rolling_metrics
            if math.isfinite(r.sharpe_ratio)
        ]
        if len(sharpes) < 2:
            return 50.0  # neutral — not enough data to judge.
        mean = sum(sharpes) / len(sharpes)
        variance = sum((s - mean) ** 2 for s in sharpes) / len(sharpes)
        std = math.sqrt(variance)
        if std == 0.0:
            return 100.0
        # CoV around mean=1.0 (a sharpe of 1 is a reasonable yardstick
        # for "good"). Lower CoV -> higher score.
        cov = std / max(abs(mean), 0.5)
        # Map CoV in [0, 4] onto [100, 0] linearly with a floor at 0.
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
