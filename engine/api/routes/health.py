from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from engine.data.providers import get_registry
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
logger = structlog.get_logger()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/providers")
async def provider_health() -> dict[str, object]:
    registry = get_registry()
    results = await registry.health()
    summary = {
        r.name: {
            "status": r.status.value,
            "latency_ms": r.latency_ms,
            "detail": r.detail,
        }
        for r in results
    }
    overall = "ok" if all(r.status.value == "up" for r in results) else "degraded"
    if results and all(r.status.value == "down" for r in results):
        overall = "down"
    return {"status": overall, "providers": summary}


@router.get("/ready")
async def ready(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    checks: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        logger.exception("readiness_check_db_failed")
        checks["db"] = "error"

    try:
        valkey_client = request.app.state.valkey
        await valkey_client.ping()
        checks["valkey"] = "ok"
    except Exception:
        logger.exception("readiness_check_valkey_failed")
        checks["valkey"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if all_ok else "degraded", **checks}
