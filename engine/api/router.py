from __future__ import annotations

from fastapi import APIRouter, Depends

from engine.api.routes.api_keys import router as api_keys_router
from engine.api.routes.auth import router as auth_router
from engine.api.routes.backtest import router as backtest_router
from engine.api.routes.client_errors import router as client_errors_router
from engine.api.routes.health import router as health_router
from engine.api.routes.legal import router as legal_router
from engine.api.routes.market_data import router as market_data_router
from engine.api.routes.marketplace import router as marketplace_router
from engine.api.routes.metrics import router as metrics_router
from engine.api.routes.mfa import router as mfa_router
from engine.api.routes.portfolio import router as portfolio_router
from engine.api.routes.privacy import router as privacy_router
from engine.api.routes.reference import router as reference_router
from engine.api.routes.scoring import router as scoring_router
from engine.api.routes.strategies import router as strategies_router
from engine.api.routes.system import router as system_router
from engine.api.routes.tax import router as tax_router
from engine.api.routes.webhooks import router as webhooks_router
from engine.api.routes.websocket import router as websocket_router
from engine.legal.dependencies import require_legal_acceptance

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(metrics_router, tags=["observability"])
api_router.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
api_router.include_router(mfa_router, prefix="/api/v1/auth/mfa", tags=["auth"])
api_router.include_router(api_keys_router, prefix="/api/v1", tags=["auth"])
api_router.include_router(system_router, prefix="/api/v1", tags=["system"])
api_router.include_router(privacy_router, prefix="/api/v1", tags=["privacy"])
api_router.include_router(websocket_router, prefix="/api/v1", tags=["websocket"])
api_router.include_router(
    backtest_router,
    prefix="/api/v1/backtest",
    tags=["backtest"],
    dependencies=[Depends(require_legal_acceptance)],
)
api_router.include_router(client_errors_router, prefix="/api/v1/client", tags=["client"])
api_router.include_router(legal_router, tags=["legal"])
api_router.include_router(portfolio_router, prefix="/api/v1/portfolio", tags=["portfolio"])
api_router.include_router(strategies_router, prefix="/api/v1/strategies", tags=["strategies"])
api_router.include_router(webhooks_router, prefix="/api/v1/webhooks", tags=["webhooks"])
api_router.include_router(marketplace_router, prefix="/api/v1/marketplace", tags=["marketplace"])
api_router.include_router(reference_router, prefix="/api/v1/reference", tags=["reference"])
api_router.include_router(tax_router, prefix="/api/v1/tax", tags=["tax"])
api_router.include_router(
    scoring_router,
    prefix="/api/v1/scoring",
    tags=["scoring"],
    dependencies=[Depends(require_legal_acceptance)],
)
api_router.include_router(
    market_data_router,
    prefix="/api/v1/market-data",
    tags=["market-data"],
    dependencies=[Depends(require_legal_acceptance)],
)
