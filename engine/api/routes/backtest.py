"""
Backtest API — run historical simulations with full cost modeling.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


class BacktestRequest(BaseModel):
    strategy_id: str
    symbols: list[str] = Field(..., min_length=1)
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    initial_cash: float = Field(default=100_000.0, ge=1000)
    strategy_params: dict = Field(default_factory=dict)
    cost_config: dict = Field(
        default_factory=lambda: {
            "commission_per_trade": 0.0,
            "spread_bps": 5.0,
            "slippage_bps": 10.0,
            "tax_method": "fifo",
            "short_term_tax_rate": 0.37,
            "long_term_tax_rate": 0.20,
        }
    )
    interval: str = Field(default="1d", pattern="^(1m|5m|15m|1h|1d)$")


class BacktestSummary(BaseModel):
    backtest_id: int
    strategy_id: str
    status: str
    initial_cash: float
    final_value: float
    total_return_pct: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    max_drawdown_pct: float
    total_trades: int
    win_rate: float
    total_costs: float
    total_taxes: float
    cost_drag_pct: float  # How much costs reduced returns
    duration_seconds: float


@router.post("/run", response_model=BacktestSummary)
async def run_backtest(req: BacktestRequest, request: Request):
    """
    Run a backtest synchronously (for small date ranges).
    For large backtests, use /run-async.
    """
    registry = request.app.state.plugin_registry
    entry = registry.get(req.strategy_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Strategy '{req.strategy_id}' not found")

    # TODO: Implement full backtest loop
    # 1. Load historical data for symbols in date range
    # 2. Initialize strategy with config
    # 3. Create backtest Portfolio + CostModel + BacktestBackend
    # 4. Loop through each bar:
    #    a. Build MarketState from historical data
    #    b. Call strategy.evaluate(portfolio.snapshot(), market_state, cost_model)
    #    c. Process signals through OrderManager
    #    d. Record equity curve point
    # 5. Calculate performance metrics
    # 6. Persist results to DB

    return BacktestSummary(
        backtest_id=0,
        strategy_id=req.strategy_id,
        status="not_implemented",
        initial_cash=req.initial_cash,
        final_value=req.initial_cash,
        total_return_pct=0.0,
        sharpe_ratio=None,
        sortino_ratio=None,
        max_drawdown_pct=0.0,
        total_trades=0,
        win_rate=0.0,
        total_costs=0.0,
        total_taxes=0.0,
        cost_drag_pct=0.0,
        duration_seconds=0.0,
    )


@router.post("/run-async")
async def run_backtest_async(req: BacktestRequest):
    """Submit a backtest as an async Celery task. Returns task ID for polling."""
    # TODO: Submit to Celery worker
    return {
        "task_id": "placeholder-task-id",
        "status": "queued",
        "poll_url": "/api/v1/backtest/status/placeholder-task-id",
    }


@router.get("/status/{task_id}")
async def backtest_status(task_id: str):
    """Poll backtest progress."""
    # TODO: Query Celery task status
    return {"task_id": task_id, "status": "not_implemented", "progress": 0}


@router.get("/results")
async def list_backtests():
    """List all completed backtests."""
    # TODO: Query DB
    return {"backtests": []}


@router.get("/results/{backtest_id}")
async def get_backtest_result(backtest_id: int):
    """Get full backtest results including equity curve and trade log."""
    # TODO: Query DB
    raise HTTPException(status_code=404, detail="Not found")
