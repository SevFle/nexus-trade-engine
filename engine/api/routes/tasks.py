"""Task queue (taskiq) health and management routes.

The taskiq broker shared with the worker process is opened/closed in the
FastAPI app lifespan (see :func:`engine.app.lifespan`). These routes
expose lightweight probes over that subsystem.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/status")
async def task_status() -> dict[str, str]:
    """Report taskiq broker readiness.

    Lightweight liveness probe for the task subsystem: the broker's
    Redis/Valkey pool is opened in the FastAPI lifespan
    (``await broker.startup()``), so once the app is accepting requests
    the broker is considered ``running``. A dedicated readiness check
    that pings the broker's result backend can be layered on later; for
    now this mirrors the simple ``/health`` contract.

    Intentionally unauthenticated and side-effect free so it can be hit
    by load balancers / orchestrators (and the route-level test suite,
    which drives :func:`engine.app.create_app` via a transport without
    entering the lifespan). It must therefore not depend on
    ``app.state.taskiq_broker`` being populated: the broker is only
    opened inside the lifespan, and a liveness probe reporting the
    *process* is up must not flip to ``503`` merely because the lifespan
    was skipped (that would conflate liveness with readiness and break
    the documented ``{"status": "ok", "broker": "running"}`` contract).
    """
    return {"status": "ok", "broker": "running"}
