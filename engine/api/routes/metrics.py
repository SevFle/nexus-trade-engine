"""Prometheus-style ``/metrics`` route (gh#34 follow-up).

Exposes the active :class:`MetricsBackend`'s state in Prometheus
exposition format when the operator has wired up a
:class:`PrometheusBackend` (or any other :class:`RecordingBackend`
subclass). When the active backend is the default :class:`NullBackend`
the endpoint returns a placeholder payload with HTTP 200 — Prometheus
is happy with that, and operators can scrape unconditionally.

The endpoint is intentionally unauthenticated. Operators who need to
restrict access put a network ACL or a reverse-proxy auth check in
front of it (the standard Prometheus pattern).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Response

from engine.observability.metrics import RecordingBackend, get_metrics
from engine.observability.prometheus import render_prometheus

router = APIRouter()
logger = structlog.get_logger()


_PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics")
async def metrics() -> Response:
    backend = get_metrics()
    if not isinstance(backend, RecordingBackend):
        # NullBackend (or any other non-recording adapter) cannot be
        # rendered. Empty body keeps Prometheus from flagging the
        # scrape as failed; operators see the placeholder.
        return Response(
            content="# metrics backend does not support exposition\n",
            media_type=_PROM_CONTENT_TYPE,
        )
    body = render_prometheus(backend)
    return Response(content=body, media_type=_PROM_CONTENT_TYPE)
