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
    """
    return {"status": "ok", "broker": "running"}
