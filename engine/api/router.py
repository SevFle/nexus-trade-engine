from __future__ import annotations

from fastapi import APIRouter

from engine.api.routes.backtest import router as backtest_router
from engine.api.routes.health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(backtest_router, prefix="/api/v1/backtest", tags=["backtest"])
