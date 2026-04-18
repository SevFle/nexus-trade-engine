from __future__ import annotations

from fastapi import APIRouter, Depends

from engine.api.routes.backtest import router as backtest_router
from engine.api.routes.health import router as health_router
from engine.api.routes.legal import router as legal_router
from engine.legal.dependencies import require_legal_acceptance

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(
    backtest_router,
    prefix="/api/v1/backtest",
    tags=["backtest"],
    dependencies=[Depends(require_legal_acceptance)],
)
api_router.include_router(legal_router, prefix="/api/v1/legal", tags=["legal"])
