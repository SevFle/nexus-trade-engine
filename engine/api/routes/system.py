"""System status endpoint for headless / automation use (gh#94).

GET /api/v1/system/status returns a single JSON object describing the
running engine: version, uptime, DB reachability, active counts.
Intended for CI/CD probes and operator scripts that don't want to
scrape /metrics.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from engine.api.auth.dependency import get_current_user
from engine.db.models import ApiKey, BacktestResult, Portfolio, User, WebhookConfig
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(prefix="/system", tags=["system"])


_PROCESS_START = time.monotonic()


def _engine_version() -> str:
    try:
        from importlib.metadata import version

        return version("nexus-trade-engine")
    except Exception:
        return "0.0.0+unknown"


class ComponentStatus(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class SystemStatusResponse(BaseModel):
    engine_version: str
    uptime_seconds: float
    server_time: datetime
    components: list[ComponentStatus]
    counts: dict[str, int]


@router.get("/status", response_model=SystemStatusResponse)
async def system_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SystemStatusResponse:
    components: list[ComponentStatus] = []

    db_ok, db_detail = await _check_database(db)
    components.append(ComponentStatus(name="database", healthy=db_ok, detail=db_detail))

    counts: dict[str, int] = {}
    if db_ok:
        counts = await _gather_counts(db)

    return SystemStatusResponse(
        engine_version=_engine_version(),
        uptime_seconds=round(time.monotonic() - _PROCESS_START, 3),
        server_time=datetime.now(tz=UTC),
        components=components,
        counts=counts,
    )


async def _check_database(db: AsyncSession) -> tuple[bool, str | None]:
    try:
        await db.execute(select(func.now()))
        return True, None
    except Exception as exc:  # pragma: no cover - probe fail path
        return False, str(exc)[:200]


async def _gather_counts(db: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key, model in (
        ("users", User),
        ("portfolios", Portfolio),
        ("backtests", BacktestResult),
        ("webhooks_active", WebhookConfig),
        ("api_keys_active", ApiKey),
    ):
        try:
            result = await db.execute(select(func.count()).select_from(model))
            counts[key] = int(result.scalar_one())
        except Exception:  # pragma: no cover - count is best-effort
            counts[key] = -1
    return counts


__all__: list[Any] = ["router"]
