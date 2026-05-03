"""ASGI middleware that emits per-request HTTP metrics (gh#34).

Three metrics per request, all routed through the active
:class:`MetricsBackend` (NullBackend → zero cost):

- ``http.request.count`` counter — exactly once per terminated request.
- ``http.request.duration_ms`` histogram — wall-time the request spent
  inside the application, measured from the moment the middleware
  receives the scope to the moment ``http.response.start`` is sent.
- ``http.request.in_flight`` gauge — set on every state change so the
  scrape always sees a fresh value.

All metrics are tagged by ``method`` and ``status_class`` (``2xx``,
``3xx``, ``4xx``, ``5xx``, ``1xx``, ``unknown``). The full path is *not*
used as a label: in a typical FastAPI app it carries ids that explode
the time-series cardinality. Operators who need per-route latency
should layer a route-aware exporter on top (deferred).

The middleware is a raw ASGI implementation so streaming responses and
``BackgroundTasks`` keep the timing measurement honest. It writes the
status code only on the first ``http.response.start`` message, which is
all Starlette will send.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from engine.observability.metrics import MetricsBackend, get_metrics

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _status_class(status: int | None) -> str:
    if status is None:
        return "unknown"
    if 100 <= status < 200:
        return "1xx"
    if 200 <= status < 300:
        return "2xx"
    if 300 <= status < 400:
        return "3xx"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "unknown"


class HttpMetricsMiddleware:
    """Wraps the downstream app, recording counters + histogram + gauge."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: MetricsBackend | None = None,
    ) -> None:
        self.app = app
        self._metrics = metrics
        self._in_flight = 0

    @property
    def metrics(self) -> MetricsBackend:
        """Resolve the metrics backend lazily so tests can swap the
        process-wide singleton via :func:`set_metrics` after construction."""
        return self._metrics if self._metrics is not None else get_metrics()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "UNKNOWN")
        metrics = self.metrics
        self._in_flight += 1
        metrics.gauge("http.request.in_flight", float(self._in_flight))

        start = time.monotonic()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and "status" not in status_holder
            ):
                status_holder["status"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            tags = {
                "method": method,
                "status_class": _status_class(status_holder.get("status")),
            }
            metrics.counter("http.request.count", tags=tags)
            metrics.histogram("http.request.duration_ms", elapsed_ms, tags=tags)
            self._in_flight -= 1
            metrics.gauge("http.request.in_flight", float(self._in_flight))


__all__ = ["HttpMetricsMiddleware"]
