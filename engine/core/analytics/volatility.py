"""Volatility & distribution section of the performance report (gh#97).

Pydantic model for the 10 volatility/distribution KPIs (taxonomy items
67-76). VaR / CVaR are stored as positive magnitudes (a 5 % VaR =
``5.0``) for parity with
:mod:`engine.core.distribution_metrics`; capture ratios and tail ratio
need a benchmark and are therefore ``float | None``.
"""

from __future__ import annotations

from pydantic import BaseModel


class VolatilityMetrics(BaseModel):
    """Volatility & distribution KPIs — taxonomy items 67-76."""

    annualized_volatility_pct: float = 0.0
    downside_deviation_pct: float = 0.0
    upside_capture_ratio: float | None = None
    downside_capture_ratio: float | None = None
    skewness: float = 0.0
    kurtosis: float = 0.0
    var_95_pct: float = 0.0
    var_99_pct: float = 0.0
    cvar_pct: float = 0.0
    tail_ratio: float | None = None


__all__ = ["VolatilityMetrics"]
