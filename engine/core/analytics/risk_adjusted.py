"""Risk-adjusted-ratio section of the performance report (gh#97).

Pydantic model for the 10 risk-adjusted KPIs (taxonomy items 15-24).

Every ratio that can be mathematically infinite or undefined (omega,
sortino, profit-factor, payoff, information/treynor ratios that need a
benchmark) is typed ``float | None``: ``None`` is emitted whenever the
denominator is zero so the resulting JSON is always finite. Computation
lives in :mod:`engine.core.analytics.analyzer`.
"""

from __future__ import annotations

from pydantic import BaseModel


class RiskAdjustedMetrics(BaseModel):
    """Risk-adjusted ratio KPIs — taxonomy items 15-24."""

    sharpe_ratio: float = 0.0
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    omega_ratio: float | None = None
    information_ratio: float | None = None
    treynor_ratio: float | None = None
    mar_ratio: float | None = None
    sterling_ratio: float | None = None
    k_ratio: float | None = None
    gain_to_pain_ratio: float | None = None


__all__ = ["RiskAdjustedMetrics"]
