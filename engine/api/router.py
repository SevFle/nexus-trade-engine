from __future__ import annotations

from fastapi import APIRouter, Depends

from engine.api.routes.auth import router as auth_router
from engine.api.routes.backtest import router as backtest_router
from engine.api.routes.health import router as health_router
from engine.api.routes.legal import router as legal_router
from engine.api.routes.marketplace import router as marketplace_router
from engine.api.routes.portfolio import router as portfolio_router
from engine.api.routes.scoring import router as scoring_router
from engine.api.routes.strategies import router as strategies_router
from engine.legal.dependencies import require_legal_acceptance

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
api_router.include_router(
    backtest_router,
    prefix="/api/v1/backtest",
    tags=["backtest"],
    dependencies=[Depends(require_legal_acceptance)],
)
api_router.include_router(legal_router, tags=["legal"])
api_router.include_router(portfolio_router, prefix="/api/v1/portfolio", tags=["portfolio"])
api_router.include_router(strategies_router, prefix="/api/v1/strategies", tags=["strategies"])
api_router.include_router(marketplace_router, prefix="/api/v1/marketplace", tags=["marketplace"])
api_router.include_router(
    scoring_router,
    prefix="/api/v1/scoring",
    tags=["scoring"],
    dependencies=[Depends(require_legal_acceptance)],
)
