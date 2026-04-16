from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text

from engine.config import settings
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
logger = structlog.get_logger()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> dict[str, str]:  # noqa: B008
    checks: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        logger.exception("readiness_check_db_failed")
        checks["db"] = "error"

    try:
        from valkey.asyncio import Valkey  # noqa: PLC0415

        client = Valkey.from_url(settings.valkey_url)
        await client.ping()
        await client.aclose()
        checks["valkey"] = "ok"
    except Exception:
        logger.exception("readiness_check_valkey_failed")
        checks["valkey"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if all_ok else "degraded", **checks}
