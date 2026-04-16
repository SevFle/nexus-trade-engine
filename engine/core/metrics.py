from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl


def compute_sharpe_ratio(returns: pl.Series, risk_free_rate: float = 0.0) -> float:
    """Stub for SEV-276 — compute annualized Sharpe ratio from return series."""
    raise NotImplementedError


def compute_max_drawdown(equity_curve: pl.Series) -> float:
    """Stub for SEV-276 — compute maximum drawdown from equity curve."""
    raise NotImplementedError


def compute_cagr(start_value: float, end_value: float, years: float) -> float:
    """Stub for SEV-276 — compound annual growth rate."""
    raise NotImplementedError
