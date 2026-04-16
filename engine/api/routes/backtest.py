from __future__ import annotations

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


@router.post("/run")
async def run_backtest(request: BacktestRequest) -> BacktestResponse:  # noqa: ARG001
    return BacktestResponse(status="accepted", task_id=None)
