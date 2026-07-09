"""Task queue (taskiq) health and management routes.

The taskiq broker shared with the worker process is opened/closed in the
FastAPI app lifespan (see :func:`engine.app.lifespan`). These routes
expose lightweight probes over that subsystem.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger()

router = APIRouter()


async def _broker_is_running(broker: object | None) -> bool:
    """Return whether the taskiq ``broker`` is live and ready.

    The status endpoint is an *infrastructure* probe — orchestrators and
    load balancers hit it during deploys and rollouts — so the broker field
    must reflect the broker's **real** state rather than a hardcoded
    constant, otherwise a probe happily green-lights a dead queue.

    The broker is started/shut down by the FastAPI lifespan and stashed on
    ``app.state.taskiq_broker`` (``None`` when the lifespan was never
    entered or ``startup()`` failed). When it's ``None`` we report
    ``stopped``.

    For the live broker we prefer taskiq's own ``is_started`` flag (set by
    ``startup()`` / cleared by ``shutdown()`` in newer releases). Older
    taskiq (e.g. 0.12) has no such flag, so when it's absent we fall back
    to an actual ``PING`` against the broker's Redis/Valkey connection
    pool — the only reliable, version-independent way to confirm the broker
    can actually reach its broker process. A ping failure is treated as
    "not running" (and logged) rather than propagated, because a liveness
    probe must never turn a broker outage into an API 500.
    """
    if broker is None:
        return False

    # Preferred signal: taskiq's own liveness flag (available on newer
    # taskiq). ``getattr`` keeps this forward/backward compatible.
    is_started = getattr(broker, "is_started", None)
    if is_started is not None:
        return bool(is_started)

    # Fallback for taskiq versions without ``is_started``: probe the
    # Redis/Valkey pool the broker holds. Only attempted when the broker
    # actually exposes a connection pool.
    pool = getattr(broker, "connection_pool", None)
    if pool is None:
        return False
    try:
        from redis.asyncio import Redis

        # Non-owning client: the broker's connection pool owns the
        # underlying connections (and shares them with the rest of the
        # API process), so we deliberately do NOT ``aclose``/``close``
        # this client — tearing it down would drain a pool other code
        # still depends on. The PING is bounded by a short timeout so a
        # hung/unreachable broker can never stall the liveness probe.
        redis = Redis(connection_pool=pool)
        return bool(await asyncio.wait_for(redis.ping(), timeout=2.0))
    except Exception as exc:  # pragma: no cover - depends on live infra
        logger.warning("tasks.status.broker_ping_failed", error=str(exc))
        return False


@router.get("/status")
async def task_status(request: Request) -> JSONResponse:
    """Report taskiq broker readiness.

    Deliberately **unauthenticated**: this is an infrastructure
    liveness/readiness probe that load balancers, orchestrators and CI hit
    during deploys, so it must stay reachable without credentials (no
    ``Depends(get_current_user)``).

    The ``broker`` field is derived from the broker's real state
    (``running`` / ``stopped``) via :func:`_broker_is_running`, rather than
    a hardcoded string, so a probe actually catches a broker outage
    instead of green-lining a dead queue. The overall endpoint ``status``
    mirrors the broker's condition: ``"ok"`` (HTTP 200) when the broker is
    running, and ``"degraded"`` (HTTP 503) when it is stopped, so
    orchestrators reading the status code alone are steered away from an
    instance that can't enqueue tasks.
    """
    broker = getattr(request.app.state, "taskiq_broker", None)
    running = await _broker_is_running(broker)
    if running:
        return JSONResponse({"status": "ok", "broker": "running"}, status_code=200)
    return JSONResponse({"status": "degraded", "broker": "stopped"}, status_code=503)


__all__ = ["router", "task_status"]
