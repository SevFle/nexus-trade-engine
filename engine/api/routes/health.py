from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from starlette import status

from engine.api.routes.tasks import _broker_is_running
from engine.data.providers import get_registry
from engine.db.session import get_session_factory
from engine.deps import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
logger = structlog.get_logger()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    # **Liveness** probe: returns 200 as long as the process is up and
    # responding to requests. Deliberately performs *no* dependency
    # checks and requires *no* auth, so orchestrators/load balancers can
    # rely on it during rollouts — a failing dependency must never take
    # down liveness (that would cause a restart loop). Gating on
    # dependencies is the job of the readiness probe below (``/readyz``).
    return {"status": "ok"}


@router.get("/api/v1/health")
async def health_v1() -> dict[str, str]:
    # Aliased path so the k6 smoke load test
    # (``GET /api/v1/health``) resolves without a 404. Kept as a
    # distinct route rather than a prefix change so the existing
    # ``/health``, ``/ready`` and rate-limit exempt config stay intact.
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


async def _readiness_checks(request: Request) -> dict[str, str]:
    """Probe the process' real dependencies and report each one's state.

    Every check is wrapped in its own ``try/except`` so that a single
    failing dependency is reported as ``error`` rather than propagating
    as an unhandled 500 — a readiness probe must turn outages into
    structured status fields, not crashes. Callers decide what HTTP
    status to emit from the aggregated result.
    """
    checks: dict[str, str] = {}

    # Database — opened directly (not via ``Depends(get_db)``) so a down
    # DB surfaces as a check result instead of a dependency-injection 500.
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        logger.exception("readiness_check_db_failed")
        checks["db"] = "error"

    # Redis / Valkey
    try:
        valkey_client = getattr(request.app.state, "valkey", None)
        if valkey_client is None:
            checks["valkey"] = "unavailable"
        else:
            await valkey_client.ping()
            checks["valkey"] = "ok"
    except Exception:
        logger.exception("readiness_check_valkey_failed")
        checks["valkey"] = "error"

    # Task broker (taskiq) — reuses the same liveness helper the
    # ``/api/v1/tasks/status`` probe uses so the two probes agree.
    try:
        broker = getattr(request.app.state, "taskiq_broker", None)
        running = await _broker_is_running(broker)
        checks["broker"] = "ok" if running else "stopped"
    except Exception:
        logger.exception("readiness_check_broker_failed")
        checks["broker"] = "error"

    return checks


@router.get("/readyz")
async def readyz(
    request: Request, response: Response
) -> dict[str, object]:
    # **Readiness** probe: gates on the process' real dependencies (DB,
    # Redis/Valkey, broker). Returns 503 when any are unavailable so load
    # balancers stop routing traffic during an outage — the complement of
    # ``/healthz``, which is a pure liveness probe and never fails. Kept
    # separate from the legacy ``/ready`` endpoint (which returns 200 with
    # a ``degraded`` body) so existing consumers keep their contract.
    checks = await _readiness_checks(request)
    all_ok = all(v == "ok" for v in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if all_ok else "degraded", **checks}


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
