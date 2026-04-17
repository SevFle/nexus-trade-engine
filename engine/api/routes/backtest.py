from __future__ import annotations

import time
import traceback
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.data.feeds import get_data_provider

logger = structlog.get_logger()

router = APIRouter()

_backtest_results: dict[str, tuple[float, dict[str, Any]]] = {}

_RESULTS_TTL_SECONDS = 3600


def _evict_expired_results() -> None:
    now = time.monotonic()
    expired = [k for k, (ts, _) in _backtest_results.items() if now - ts > _RESULTS_TTL_SECONDS]
    for k in expired:
        del _backtest_results[k]
    if expired:
        logger.debug("backtest.results_evicted", count=len(expired))


class BacktestRequest(BaseModel):
    strategy_name: str
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float = 100_000.0
    config: dict | None = None


class BacktestResponse(BaseModel):
    status: str
    backtest_id: str | None = None


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


def _empty_metrics() -> MetricsSummary:
    return MetricsSummary(
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
    )


class BacktestResultResponse(BaseModel):
    status: str
    strategy_name: str
    symbol: str
    initial_capital: float
    final_value: float
    metrics: MetricsSummary
    equity_curve: list[dict[str, Any]]
    drawdown_curve: list[float]
    error: str | None = None


async def _run_backtest_background(
    backtest_id: str,
    request: BacktestRequest,
) -> None:
    _backtest_results[backtest_id] = (
        time.monotonic(),
        {
            "status": "running",
            "strategy_name": request.strategy_name,
            "symbol": request.symbol,
        },
    )

    try:
        provider = get_data_provider("yahoo")
        config = BacktestConfig(
            strategy_name=request.strategy_name,
            symbol=request.symbol,
            start_date=request.start_date,
            end_date=request.end_date,
            initial_capital=request.initial_capital,
        )

        from engine.plugins.registry import PluginRegistry

        registry = PluginRegistry()
        strategy = registry.load_strategy(request.strategy_name)
        if strategy is None:
            raise ValueError(f"Strategy not found: {request.strategy_name}")

        runner = BacktestRunner(config=config, strategy=strategy, provider=provider)
        result = await runner.run()

        _backtest_results[backtest_id] = (
            time.monotonic(),
            {
                "status": "completed",
                "strategy_name": request.strategy_name,
                "symbol": request.symbol,
                "initial_capital": request.initial_capital,
                "final_value": result.final_capital,
                "metrics": result.metrics,
                "equity_curve": result.equity_curve,
                "trades": result.trades,
            },
        )

    except Exception as e:
        logger.exception(
            "backtest.background_failed",
            backtest_id=backtest_id,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        _backtest_results[backtest_id] = (
            time.monotonic(),
            {
                "status": "failed",
                "strategy_name": request.strategy_name,
                "symbol": request.symbol,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )


@router.post("/run")
async def run_backtest(
    request: BacktestRequest,
    background_tasks: BackgroundTasks,
) -> BacktestResponse:
    backtest_id = str(uuid.uuid4())
    background_tasks.add_task(
        _run_backtest_background,
        backtest_id,
        request,
    )
    return BacktestResponse(status="accepted", backtest_id=backtest_id)


@router.get("/results/{backtest_id}")
async def get_backtest_result(backtest_id: str) -> JSONResponse:
    _evict_expired_results()

    entry = _backtest_results.get(backtest_id)
    if entry is None:
        return JSONResponse(
            status_code=404,
            content=BacktestResultResponse(
                status="not_found",
                strategy_name="",
                symbol="",
                initial_capital=0.0,
                final_value=0.0,
                metrics=_empty_metrics(),
                equity_curve=[],
                drawdown_curve=[],
                error=f"Backtest {backtest_id} not found",
            ).model_dump(),
        )

    stored = entry[1]
    status = stored.get("status", "unknown")

    if status == "running":
        return JSONResponse(
            status_code=202,
            content=BacktestResultResponse(
                status="running",
                strategy_name=stored.get("strategy_name", ""),
                symbol=stored.get("symbol", ""),
                initial_capital=0.0,
                final_value=0.0,
                metrics=_empty_metrics(),
                equity_curve=[],
                drawdown_curve=[],
            ).model_dump(),
        )

    if status == "failed":
        return JSONResponse(
            status_code=200,
            content=BacktestResultResponse(
                status="failed",
                strategy_name=stored.get("strategy_name", ""),
                symbol=stored.get("symbol", ""),
                initial_capital=0.0,
                final_value=0.0,
                metrics=_empty_metrics(),
                equity_curve=[],
                drawdown_curve=[],
                error=stored.get("error", "Unknown error"),
            ).model_dump(),
        )

    metrics_data = stored.get("metrics", {})
    rolling = metrics_data.get("rolling_metrics", [])

    return JSONResponse(
        status_code=200,
        content=BacktestResultResponse(
            status="completed",
            strategy_name=stored.get("strategy_name", ""),
            symbol=stored.get("symbol", ""),
            initial_capital=stored.get("initial_capital", 0.0),
            final_value=stored.get("final_value", 0.0),
            metrics=MetricsSummary(
                total_return_pct=metrics_data.get("total_return_pct", 0.0),
                annualized_return_pct=metrics_data.get("annualized_return_pct", 0.0),
                sharpe_ratio=metrics_data.get("sharpe_ratio", 0.0),
                sortino_ratio=metrics_data.get("sortino_ratio"),
                max_drawdown_pct=metrics_data.get("max_drawdown_pct", 0.0),
                max_drawdown_duration_days=metrics_data.get("max_drawdown_duration_days", 0),
                max_drawdown_recovery_days=metrics_data.get("max_drawdown_recovery_days"),
                calmar_ratio=metrics_data.get("calmar_ratio"),
                volatility_annual_pct=metrics_data.get("volatility_annual_pct", 0.0),
                total_trades=metrics_data.get("total_trades", 0),
                win_rate=metrics_data.get("win_rate", 0.0),
                profit_factor=metrics_data.get("profit_factor"),
                avg_trade_pnl=metrics_data.get("avg_trade_pnl", 0.0),
                avg_winner=metrics_data.get("avg_winner", 0.0),
                avg_loser=metrics_data.get("avg_loser", 0.0),
                best_trade=metrics_data.get("best_trade", 0.0),
                worst_trade=metrics_data.get("worst_trade", 0.0),
                max_consecutive_wins=metrics_data.get("max_consecutive_wins", 0),
                max_consecutive_losses=metrics_data.get("max_consecutive_losses", 0),
                total_costs=metrics_data.get("total_costs", 0.0),
                total_taxes=metrics_data.get("total_taxes", 0.0),
                cost_drag_pct=metrics_data.get("cost_drag_pct", 0.0),
                turnover_ratio=metrics_data.get("turnover_ratio", 0.0),
                exposure_pct=metrics_data.get("exposure_pct", 0.0),
                rolling_metrics=[RollingMetricsSnapshot(**rm) for rm in rolling],
            ),
            equity_curve=stored.get("equity_curve", []),
            drawdown_curve=metrics_data.get("drawdown_curve", []),
        ).model_dump(),
    )
