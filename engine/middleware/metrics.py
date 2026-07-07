"""Prometheus metrics middleware built on the official ``prometheus_client``.

A thin, self-contained ASGI layer that records request telemetry into a
:class:`prometheus_client.registry.CollectorRegistry` for routes under
``/api``. It is parallel to (and does not replace) the engine's own
pluggable :class:`~engine.observability.metrics.MetricsBackend` stack —
that stack is rendered by ``engine.observability.prometheus``; this module
writes to the real ``prometheus_client`` registry so the standard
``/metrics`` scrape endpoint (see :mod:`engine.api.routes.metrics`) exposes
the default-collector payload operators expect from a Prometheus target.

Three metrics are defined on the registry:

- ``http_requests_total``              — :class:`~prometheus_client.Counter`
  labelled ``method``, ``status``, ``path`` — incremented exactly once per
  terminated ``/api`` request.
- ``http_request_duration_seconds``    — :class:`~prometheus_client.Histogram`
  labelled ``method``, ``status``, ``path`` — one latency observation per
  request, with buckets tuned for HTTP latency.
- ``http_requests_active``             — :class:`~prometheus_client.Gauge`
  labelled ``method`` — tracks in-flight ``/api`` requests.

Cardinality note
----------------
The ``path`` label is normalised before recording: UUID-shaped and
pure-numeric / long-hex segments collapse to ``:id`` so routes such as
``/api/v1/portfolio/<uuid>`` map to a single time series. Only ``/api``
routes are instrumented, which keeps the label space bounded to the API
surface (scrapes, docs, and health pings are intentionally excluded).
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.registry import REGISTRY, CollectorRegistry

if TYPE_CHECKING:
    from prometheus_client import Counter as _CounterT
    from prometheus_client import Gauge as _GaugeT
    from prometheus_client import Histogram as _HistogramT
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "ACTIVE_REQUESTS",
    "CONTENT_TYPE_LATEST",
    "REQUEST_COUNT",
    "REQUEST_LATENCY",
    "PrometheusMetricsMiddleware",
    "generate_latest",
    "get_or_create_metrics",
    "normalize_path",
]

#: Only routes whose path starts with this prefix are instrumented.
DEFAULT_API_PREFIX = "/api"

# Latency buckets in seconds, tuned for HTTP request latency: from a fast
# in-memory lookup (~5ms) out to a slow endpoint / p99 tail (~10s). ``+Inf``
# is appended implicitly by ``prometheus_client``.
HTTP_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# Collapse a full UUID (8-4-4-4-12 hex) to ``:id``.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Match a single path segment that is purely numeric or a long hex blob
# (>=12 chars) — covers Postgres bigserial ids, MongoDB ObjectIds, hashes.
_HEX_OR_NUM_RE = re.compile(r"^[0-9a-fA-F]{12,}$|^\d+$")


def normalize_path(path: str) -> str:
    """Return a cardinality-safe ``path`` label.

    UUID segments and pure-numeric / long-hex segments are replaced with
    ``:id`` so each logical route maps to a single label value. Static
    segments (``portfolio``, ``api``, ``v1``, ``orders`` ...) are left
    untouched. An empty path is returned unchanged.
    """
    if not path:
        return path
    path = _UUID_RE.sub(":id", path)
    segments = [":id" if _HEX_OR_NUM_RE.match(seg) else seg for seg in path.split("/")]
    return "/".join(segments)


class _Metrics:
    """Holds the three collectors for one registry."""

    __slots__ = ("counter", "gauge", "histogram")

    def __init__(
        self,
        counter: _CounterT,
        histogram: _HistogramT,
        gauge: _GaugeT,
    ) -> None:
        self.counter = counter
        self.histogram = histogram
        self.gauge = gauge


# Identity-keyed cache so constructing the middleware repeatedly against the
# same registry (e.g. once per test via ``create_app``) does not raise
# ``Duplicated timeseries``. The default global registry is a singleton, so
# its entry is created exactly once at import time below.
_metrics_cache: dict[int, _Metrics] = {}


def get_or_create_metrics(registry: CollectorRegistry) -> _Metrics:
    """Return the cached :class:`_Metrics` for ``registry``, creating it on
    first use. Public so tests can fetch handles to a private registry."""
    key = id(registry)
    cached = _metrics_cache.get(key)
    if cached is not None:
        return cached
    metrics = _Metrics(
        counter=Counter(
            "http_requests_total",
            "Total HTTP requests handled by /api routes.",
            ["method", "status", "path"],
            registry=registry,
        ),
        histogram=Histogram(
            "http_request_duration_seconds",
            "Latency of /api route requests in seconds.",
            ["method", "status", "path"],
            buckets=HTTP_LATENCY_BUCKETS,
            registry=registry,
        ),
        gauge=Gauge(
            "http_requests_active",
            "Number of in-flight /api requests.",
            ["method"],
            registry=registry,
        ),
    )
    _metrics_cache[key] = metrics
    return metrics


# Module-level handles bound to the default registry. Importing this module
# registers the collectors once; the app factory and tests can reference these
# directly. ``generate_latest(REGISTRY)`` includes them in every scrape.
_default = get_or_create_metrics(REGISTRY)
REQUEST_COUNT: _CounterT = _default.counter
REQUEST_LATENCY: _HistogramT = _default.histogram
ACTIVE_REQUESTS: _GaugeT = _default.gauge


class PrometheusMetricsMiddleware:
    """Raw-ASGI middleware that records Prometheus metrics for ``/api`` routes.

    Wraps the downstream app and, for each ``http`` request whose path starts
    with ``prefix`` (default ``"/api"``):

    1. increments ``http_requests_active`` on entry, decrements on exit
       (including the exception path), so the gauge always reflects live
       in-flight traffic;
    2. captures the first ``http.response.start`` status code;
    3. on completion records one ``http_requests_total`` increment and one
       ``http_request_duration_seconds`` observation, both tagged with the
       normalised path.

    Requests outside ``/api`` (docs, ``/metrics`` scrapes, ``/health``)
    pass straight through with zero overhead beyond a prefix check.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        registry: CollectorRegistry | None = None,
        prefix: str = DEFAULT_API_PREFIX,
    ) -> None:
        self.app = app
        self.registry = registry if registry is not None else REGISTRY
        self.prefix = prefix
        self._metrics = get_or_create_metrics(self.registry)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if not path.startswith(self.prefix):
            # Scoping: only /api routes are instrumented.
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "UNKNOWN") or "UNKNOWN"
        label_path = normalize_path(path)
        status_holder: dict[str, str] = {}

        self._metrics.gauge.labels(method=method).inc()

        async def send_wrapper(message: Message) -> None:
            if (
                message.get("type") == "http.response.start"
                and "status" not in status_holder
            ):
                status_holder["status"] = str(message.get("status", 0))
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            status = status_holder.get("status", "0")
            self._metrics.counter.labels(
                method=method, status=status, path=label_path
            ).inc()
            self._metrics.histogram.labels(
                method=method, status=status, path=label_path
            ).observe(elapsed)
            self._metrics.gauge.labels(method=method).dec()

    def expose(self) -> bytes:
        """Return the Prometheus exposition payload for this middleware's
        registry. Convenience wrapper around :func:`generate_latest`."""
        return generate_latest(self.registry)
