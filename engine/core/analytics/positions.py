"""Position & exposure section of the performance report (gh#97).

Pydantic model for the 8 position/exposure KPIs (taxonomy items 59-66).
``time_in_market_pct`` (exposure) is precise when the equity curve
carries per-bar ``cash``; the remaining long/short / concentration /
simultaneous-position metrics are reconstructed best-effort from the
trade log and degrade to zero / ``None`` when there is not enough
information to compute them.
"""

from __future__ import annotations

from pydantic import BaseModel


class PositionMetrics(BaseModel):
    """Position & exposure KPIs — taxonomy items 59-66."""

    time_in_market_pct: float = 0.0
    avg_simultaneous_positions: float = 0.0
    max_simultaneous_positions: int = 0
    long_short_ratio: float | None = None
    portfolio_concentration_pct: float = 0.0
    turnover_ratio: float = 0.0
    portfolio_beta: float | None = None
    correlation_to_benchmark: float | None = None


__all__ = ["PositionMetrics"]
