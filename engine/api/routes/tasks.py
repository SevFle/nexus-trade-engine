"""Task queue (taskiq) health and management routes.

The taskiq broker shared with the worker process is opened/closed in the
FastAPI app lifespan (see :func:`engine.app.lifespan`). These routes
expose lightweight probes over that subsystem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from taskiq.abc.broker import AsyncBroker

router = APIRouter()


async def _broker_ready(broker: AsyncBroker | None) -> bool:
    """Return whether ``broker`` is ready to enqueue/deliver tasks.

    The probe is layered so it stays accurate across taskiq versions and
    broker backends, while never blocking the hot path unless the broker
    explicitly opts in:

    1. A ``None`` broker means the app lifespan never wired one up (or its
       ``startup()`` raised — see :func:`engine.app._init_taskiq_broker`),
       which is definitively *not* ready.
    2. Prefer an explicit ``is_started`` flag when the broker exposes one.
    3. Probe a ``ping()`` coroutine if the backend implements one, treating
       any raised exception as "not ready" rather than 500-ing the probe.
    4. Fall back to the broker's ``state`` attribute. The base
       :class:`taskiq.AsyncBroker` only populates ``state`` on a live,
       constructed instance, and the lifespan only publishes a broker into
       ``app.state.taskiq_broker`` once ``await broker.startup()`` has
       succeeded — so a present broker carrying ``state`` is ready.
    """
    if broker is None:
        return False
    if getattr(broker, "is_started", False):
        return True
    ping = getattr(broker, "ping", None)
    if callable(ping):
        try:
            return bool(await ping())
        except Exception:
            # A backend that implements ping but cannot reach its datastore
            # is not ready; surface that as a normal "not_ready" probe
            # result rather than turning the health check into a 500.
            return False
    return hasattr(broker, "state")


@router.get("/status")
async def task_status(request: Request) -> dict[str, str]:
    """Report taskiq broker readiness.

    Unauthenticated by design: this is an infrastructure liveness/readiness
    probe (k8s, load balancers, CI smoke checks) that must be reachable
    without credentials. The route never returns task payloads or user
    data — only a coarse ``ready``/``not_ready`` aggregate — so exposing
    it carries no information-leak risk.

    The broker instance is resolved from ``app.state.taskiq_broker`` (set
    during app lifespan startup). When the broker is not ready the probe
    returns HTTP 503 so orchestrators pull the pod out of rotation; when
    ready it returns the documented 200 contract.
    """
    broker: Any = getattr(request.app.state, "taskiq_broker", None)
    ready = await _broker_ready(broker)
    if not ready:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "broker": "not_ready"},
        )
    return {"status": "ok", "broker": "ready"}
