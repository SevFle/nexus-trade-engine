"""Lightweight Prometheus metrics middleware (gh#34 follow-up).

A small, self-contained ASGI middleware backed by ``prometheus_client``
that instruments every HTTP request with a ``Counter`` and a
``Histogram``. Unlike the pluggable ``MetricsBackend`` abstraction in
``engine.observability.metrics`` — which deliberately avoids the
``prometheus_client`` dependency and renders its own text format — this
module speaks the *real* Prometheus client library so a default scrape
target works out of the box.

Two metrics are emitted, both labelled with ``method``, ``path`` and
``status``:

- ``http_requests_total`` — counter, incremented exactly once per
  terminated request.
- ``http_request_duration_seconds`` — histogram, one observation per
  request (wall-clock seconds the request spent inside the app).

Wire :class:`PrometheusMetricsMiddleware` onto a Starlette/FastAPI app
and call :func:`register_metrics_endpoint` to expose ``/metrics``. The
exposition payload comes from :func:`prometheus_client.generate_latest`,
which serialises the process-global default registry.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total number of HTTP requests handled.",
    ["method", "path", "status"],
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Wall-clock duration of HTTP requests in seconds.",
    ["method", "path", "status"],
)


class PrometheusMetricsMiddleware:
    """ASGI middleware recording request count + latency per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        labels = {
            "method": scope.get("method", "UNKNOWN"),
            "path": scope.get("path", "/"),
            "status": "0",
        }
        start = time.monotonic()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                labels["status"] = str(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            REQUEST_COUNT.labels(**labels).inc()
            REQUEST_DURATION.labels(**labels).observe(time.monotonic() - start)


def metrics_response() -> Response:
    """Return the current Prometheus exposition payload."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def register_metrics_endpoint(app: Starlette, path: str = "/metrics") -> None:
    """Attach a GET ``path`` route returning the Prometheus exposition.

    Works with both Starlette and FastAPI applications (FastAPI subclasses
    Starlette and inherits ``add_route``).
    """

    async def _expose(_request: Request) -> Response:
        return metrics_response()

    app.add_route(path, _expose, methods=["GET"])


__all__ = [
    "REQUEST_COUNT",
    "REQUEST_DURATION",
    "PrometheusMetricsMiddleware",
    "metrics_response",
    "register_metrics_endpoint",
]
