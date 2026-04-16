from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from functools import partial

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from engine.core.backtest_runner import BacktestConfig, run_backtest
from engine.db.models import BacktestResult
from engine.db.session import get_session

router = APIRouter()


class BacktestRequest(BaseModel):
    strategy_name: str
    symbols: list[str]
    start_date: str
    end_date: str
    initial_cash: float = 100_000.0
    strategy_params: dict | None = None
    cost_config: dict | None = None
    interval: str = "1d"
    random_seed: int | None = 42


class BacktestResponse(BaseModel):
    status: str
    task_id: str | None = None
    backtest_id: str | None = None
    message: str | None = None


def _run_backtest_background(config: BacktestConfig, backtest_id: str):
    asyncio.run(run_backtest(config, backtest_id=backtest_id))


@router.post("/run")
async def run_backtest_endpoint(
    request: BacktestRequest, background_tasks: BackgroundTasks
) -> BacktestResponse:
    try:
        datetime.fromisoformat(request.start_date)
        datetime.fromisoformat(request.end_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD") from e

    if not request.symbols:
        raise HTTPException(status_code=400, detail="At least one symbol is required")

    config = BacktestConfig(
        strategy_name=request.strategy_name,
        symbols=request.symbols,
        start_date=request.start_date,
        end_date=request.end_date,
        initial_cash=request.initial_cash,
        strategy_params=request.strategy_params or {},
        cost_config=request.cost_config or {},
        interval=request.interval,
        random_seed=request.random_seed,
    )

    task_id = str(uuid.uuid4())

    background_tasks.add_task(partial(_run_backtest_background, config, task_id))

    return BacktestResponse(
        status="accepted",
        task_id=task_id,
        backtest_id=task_id,
        message="Backtest started. Use the returned backtest_id to query results.",
    )


@router.get("/results/{backtest_id}")
async def get_backtest_result(backtest_id: str) -> dict:
    async with get_session() as session:
        stmt = select(BacktestResult).where(BacktestResult.id == uuid.UUID(backtest_id))
        result = await session.execute(stmt)
        backtest = result.scalar_one_or_none()

    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest result not found")

    return {
        "id": str(backtest.id),
        "strategy_name": backtest.strategy_name,
        "start_date": backtest.start_date.isoformat(),
        "end_date": backtest.end_date.isoformat(),
        "metrics": backtest.metrics,
    }
