"""Task queue (taskiq) health and management routes.

The taskiq broker shared with the worker process is opened/closed in the
FastAPI app lifespan (see :func:`engine.app.lifespan`). These routes
expose lightweight probes over that subsystem.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request

logger = structlog.get_logger()

router = APIRouter()

# Sentinel distinct from every real attribute value. Used so we can tell
# "the broker has no ``is_started`` attribute at all" (older taskiq, e.g.
# 0.12) apart from "the attribute exists but is ``None`` / falsy". Using
# ``None`` as the ``getattr`` default conflates the two and would make a
# genuinely-``None`` flag fall through to the PING probe instead of being
# trusted as a (falsy) value.
_MISSING: object = object()


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
    taskiq (e.g. 0.12) has no such flag, so when it's *absent* we fall back
    to an actual ``PING`` against the broker's ``connection_pool`` — the
    only reliable, version-independent way to confirm the broker can reach
    its broker process.

    The PING is issued through a throwaway ``redis.asyncio.Redis`` client
    bound to the broker's pool via ``async with Redis(connection_pool=pool)``.
    Closing that client on context exit does **not** disconnect the broker's
    shared pool: redis-py (>=5.0.1) sets ``auto_close_connection_pool =
    False`` whenever a ``connection_pool`` is passed explicitly to
    ``Redis()``, so ``aclose()`` only releases the connection the probe
    borrowed and never calls ``pool.disconnect()``. The probe therefore
    cannot sever or perturb the pool the app's task dispatch depends on. A
    ping failure is treated as "not running" (and logged) rather than
    propagated, because a liveness probe must never turn a broker outage
    into an API 500.
    """
    if broker is None:
        return False

    # Preferred signal: taskiq's own liveness flag (available on newer
    # taskiq). ``getattr`` with a MISSING sentinel keeps this forward/backward
    # compatible AND distinguishes "flag absent" (fall back to PING) from
    # "flag present but None/falsy" (trust it as a value via ``bool()``).
    is_started = getattr(broker, "is_started", _MISSING)
    if is_started is not _MISSING:
        return bool(is_started)

    # Fallback for taskiq versions without ``is_started``: probe the
    # Redis/Valkey the broker points at by binding a throwaway client to the
    # broker's ``connection_pool``. Imported lazily so tests can patch
    # ``redis.asyncio.Redis`` at call time.
    pool = getattr(broker, "connection_pool", None)
    if pool is None:
        return False
    from redis.asyncio import Redis

    try:
        # ``Redis(connection_pool=pool)`` reuses the broker's shared pool.
        # Because the pool is passed explicitly redis-py leaves
        # ``auto_close_connection_pool`` ``False``, so the ``async with``
        # exit (which calls ``aclose()``) only releases the connection this
        # probe borrowed — it never disconnects/severs the broker's pool.
        async with Redis(connection_pool=pool) as client:
            return bool(await client.ping())
    except Exception as exc:
        logger.warning("tasks.status.broker_ping_failed", error=str(exc))
        return False


@router.get("/status")
async def task_status(request: Request) -> dict[str, object]:
    """Report taskiq broker readiness.

    Deliberately **unauthenticated**: this is an infrastructure
    liveness/readiness probe that load balancers, orchestrators and CI hit
    during deploys, so it must stay reachable without credentials (no
    ``Depends(get_current_user)``).

    The ``broker`` field is derived from the broker's real state
    (``running`` / ``stopped``) via :func:`_broker_is_running`, rather than
    a hardcoded string, so a probe actually catches a broker outage
    instead of green-lining a dead queue. The overall endpoint ``status``
    stays ``"ok"`` and the HTTP code is always **200** because the API
    itself is up regardless of the broker's condition — the per-subsystem
    ``broker`` / ``broker_online`` fields carry the detail so callers can
    branch on the broker's health without a failing probe tripping
    orchestrator restarts or alerting.
    """
    broker = getattr(request.app.state, "taskiq_broker", None)
    running = await _broker_is_running(broker)
    return {
        "status": "ok",
        "broker": "running" if running else "stopped",
        # Machine-readable boolean mirror of ``broker``. A probe must never
        # turn a broker outage into a non-200 (which would trigger
        # orchestrator restarts), so the HTTP status stays 200 and callers
        # inspect ``broker_online`` to know whether the queue is reachable.
        "broker_online": running,
    }


__all__ = ["router", "task_status"]
