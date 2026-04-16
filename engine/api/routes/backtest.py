from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class BacktestRequest(BaseModel):
    strategy_name: str
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float = 100_000.0
    config: dict | None = None


class BacktestResponse(BaseModel):
    status: str
    task_id: str | None = None


class RollingMetricsSnapshot(BaseModel):
    window_days: int
    sharpe_ratio: float
    sortino_ratio: float | None
    volatility_annual_pct: float
    max_drawdown_pct: float


class MetricsSummary(BaseModel):
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float | None
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    max_drawdown_recovery_days: int | None
    calmar_ratio: float | None
    volatility_annual_pct: float
    total_trades: int
    win_rate: float
    profit_factor: float | None
    avg_trade_pnl: float
    avg_winner: float
    avg_loser: float
    best_trade: float
    worst_trade: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    total_costs: float
    total_taxes: float
    cost_drag_pct: float
    turnover_ratio: float
    exposure_pct: float
    rolling_metrics: list[RollingMetricsSnapshot] = []


class BacktestResultResponse(BaseModel):
    status: str
    strategy_name: str
    symbol: str
    initial_capital: float
    final_value: float
    metrics: MetricsSummary
    equity_curve: list[dict[str, Any]]
    drawdown_curve: list[float]


@router.post("/run")
async def run_backtest(_request: BacktestRequest) -> BacktestResponse:
    return BacktestResponse(status="accepted", task_id=None)


@router.post("/result")
async def get_backtest_result(request: BacktestRequest) -> BacktestResultResponse:
    return BacktestResultResponse(
        status="completed",
        strategy_name=request.strategy_name,
        symbol=request.symbol,
        initial_capital=request.initial_capital,
        final_value=request.initial_capital,
        metrics=MetricsSummary(
            total_return_pct=0.0,
            annualized_return_pct=0.0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            max_drawdown_pct=0.0,
            max_drawdown_duration_days=0,
            max_drawdown_recovery_days=0,
            calmar_ratio=0.0,
            volatility_annual_pct=0.0,
            total_trades=0,
            win_rate=0.0,
            profit_factor=0.0,
            avg_trade_pnl=0.0,
            avg_winner=0.0,
            avg_loser=0.0,
            best_trade=0.0,
            worst_trade=0.0,
            max_consecutive_wins=0,
            max_consecutive_losses=0,
            total_costs=0.0,
            total_taxes=0.0,
            cost_drag_pct=0.0,
            turnover_ratio=0.0,
            exposure_pct=0.0,
        ),
        equity_curve=[],
        drawdown_curve=[],
    )
