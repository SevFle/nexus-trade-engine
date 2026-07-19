"""Prometheus ASGI middleware backed by ``prometheus_client`` (SEV-223+).

A *raw-ASGI* middleware (not a Starlette ``BaseHTTPMiddleware``) that
records three HTTP-layer metrics directly into the
:data:`prometheus_client.REGISTRY` default registry:

- ``http_requests_total`` — :class:`prometheus_client.Counter`, labelled
  ``method``, ``status``, ``path``. Incremented exactly once per
  terminated HTTP request.
- ``http_request_duration_seconds`` — :class:`prometheus_client.Histogram`
  with the default Prometheus latency buckets, same labels. One
  observation per request.
- ``http_requests_in_flight`` — :class:`prometheus_client.Gauge` with no
  labels. Incremented on entry, decremented in a ``finally`` so it
  always settles back to zero.

Why a second metrics middleware?
--------------------------------
The engine already has :class:`engine.observability.http_metrics.HttpMetricsMiddleware`,
which routes the same three measurements through the pluggable
:class:`engine.observability.metrics.MetricsBackend` abstraction and is
exposed (in a custom exposition format) at ``/metrics`` by
:mod:`engine.api.routes.metrics`. This module is intentionally a *thin,
parallel* layer that records straight into the ``prometheus_client``
default registry — the wire-format and tooling integration that the
Prometheus ecosystem (``prometheus_client``'s ``generate_latest``,
default ``process_*`` / ``python_*`` collectors, the official
``/metrics`` scrape contract) expect. Operators pick whichever surface
matches their scraper; both can run side-by-side without interference
because they write to disjoint backends.

Design notes
------------
- **Lazy collector registration.** No ``prometheus_client`` collectors
  are constructed at import time. They are created on the first call to
  :func:`_get_collectors` (i.e. when the first ``PrometheusMiddleware``
  instance is built). Importing this module is therefore free of
  registry side-effects — important because the engine is imported as a
  library by tests, the SDK, and CLI tooling that may never start an
  HTTP server.
- **Raw ASGI, not ``BaseHTTPMiddleware``.** Matches the convention used
  by :class:`engine.observability.http_metrics.HttpMetricsMiddleware`
  and :class:`engine.observability.middleware.CorrelationIdMiddleware`:
  ``BaseHTTPMiddleware`` has a well-known streaming-timing hazard that
  drops timing accuracy on streaming responses and ``BackgroundTasks``.
- **Path cardinality normalisation.** The raw request path is run
  through :func:`normalize_path`, which replaces UUIDs and numeric id
  segments with ``:uuid`` / ``:id`` placeholders. Without this, every
  ``/api/v1/portfolio/{uuid}`` request would create a distinct label
  series and blow up the registry.
- **Scrape-path exemption.** The middleware does not record metrics for
  ``/metrics`` or ``/metrics/prometheus`` themselves; otherwise a tight
  Prometheus scrape interval would dominate the latency histogram. The
  exemption set is configurable via ``exempt_paths``.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
import weakref
from typing import TYPE_CHECKING

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


__all__ = [
    "DEFAULT_EXEMPT_PATHS",
    "METRIC_PREFIX",
    "PrometheusMiddleware",
    "normalize_path",
    "reset_collectors_for_tests",
]


METRIC_PREFIX = "http"


# Default Prometheus histogram buckets (seconds) — matches
# ``prometheus_client.Histogram``'s built-in default so engine latency
# series line up with the rest of the ecosystem. Listed explicitly so a
# future bump of ``prometheus_client`` does not silently change the
# bucket layout under operators' dashboards.
_DEFAULT_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


# ---------------------------------------------------------------------------
# Path cardinality normalisation
# ---------------------------------------------------------------------------

# UUIDv4-ish: 8-4-4-4-12 hex digits. Matched before the numeric pattern
# so a UUID made entirely of digits (vanishingly rare but legal hex) is
# collapsed to ``:uuid`` rather than ``:id``.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Numeric path segment: a run of digits bounded by ``/`` or end-of-string.
# Replaces the digits only (keeps the leading ``/``).
_NUMERIC_RE = re.compile(r"(?<=/)\d+(?=/|$)")


def normalize_path(path: str) -> str:
    """Collapse dynamic path segments so the ``path`` label stays bounded.

    Replaces UUIDs with ``:uuid`` and bare-numeric segments with ``:id``.
    Query strings are stripped defensively — ASGI puts them in
    ``scope['query_string']``, not ``scope['path']``, so this is a
    belt-and-braces guard against callers that hand us a full URL.
    Returns ``"/"`` for empty input so the label value is always
    non-empty (Prometheus treats empty label values as ``""``).
    """
    if not path:
        return "/"
    # Drop any query string that snuck in.
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path:
        return "/"
    path = _UUID_RE.sub(":uuid", path)
    return _NUMERIC_RE.sub(":id", path)


# ---------------------------------------------------------------------------
# Lazy collector registration
# ---------------------------------------------------------------------------

# Module-level cache of the lazily-created collectors.
#
# The conceptual cache key is the tuple
# ``(registry, metric_prefix, tuple(buckets))`` — two middleware
# instances built against the same registry but with different
# prefixes (or different histogram bucket layouts) must receive
# *distinct* collector objects, otherwise the second construction
# would either silently reuse the first one's prefix/buckets or
# blow up with ``Duplicated timeseries`` when it tried to register
# its own.
#
# Because plain ``tuple`` objects are not weak-referenceable, the
# key is split across two levels:
#
# - The *outer* level is a :class:`weakref.WeakKeyDictionary` keyed
#   by the registry object itself. Tests that pass a throwaway
#   ``CollectorRegistry`` get their own fresh collector set without
#   colliding with the production default registry, and — crucially
#   — the entry vanishes automatically as soon as the registry is
#   garbage-collected. Keying on the object (rather than
#   ``id(registry)``) closes a subtle corruption window: CPython is
#   free to recycle the memory address of a GC'd registry, which
#   under the old ``id()``-keyed ``dict`` could resurrect a stale
#   cache entry pointing at collectors registered against a
#   now-dead registry.
# - The *inner* level is a plain :class:`dict` keyed by
#   ``(metric_prefix, tuple(buckets))`` holding the collector sets
#   for that registry. It is created lazily on first insertion.
#
# ``reset_collectors_for_tests`` is still provided for the
# default-registry teardown path because the default
# :data:`REGISTRY` is process-global and never dies.
_collectors_cache: weakref.WeakKeyDictionary[
    CollectorRegistry,
    dict[tuple[str, tuple[float, ...]], dict[str, Counter | Gauge | Histogram]],
] = weakref.WeakKeyDictionary()

# Module-level lock guarding both lazy collector creation in
# :func:`_get_collectors` and the iterate-unregister-clear teardown
# in :func:`reset_collectors_for_tests`. The app factory and several
# test fixtures construct middleware from multiple threads (and
# ``RQ``/``arq`` workers can instantiate the app per-worker); without
# serialisation two threads could race past the ``cached is None``
# check and both try to register ``http_requests_total``, with the
# second raising ``Duplicated timeseries``. The lock is held for the
# whole create-and-store critical section because ``prometheus_client``
# constructor calls are fast and never re-enter this module.
_collectors_lock = threading.Lock()


def _get_collectors(
    registry: CollectorRegistry,
    *,
    metric_prefix: str = METRIC_PREFIX,
    buckets: tuple[float, ...] = _DEFAULT_BUCKETS,
) -> dict[str, Counter | Gauge | Histogram]:
    """Return the cached collector set for ``registry``, creating it
    on first access. Each call with the same
    ``(registry, metric_prefix, buckets)`` triple returns the same
    objects — re-creating a named collector in a ``prometheus_client``
    registry raises ``ValueError``, so this memoisation is required for
    any code path that constructs more than one middleware against the
    same registry (notably the app factory when it is invoked more than
    once in a process, e.g. some test harnesses).

    The cache key is the full triple
    ``(registry, metric_prefix, tuple(buckets))`` so two middlewares
    that differ only in prefix or bucket layout each get their own
    collector set rather than silently sharing the first one's. The
    registry half of the key lives in the outer
    :class:`weakref.WeakKeyDictionary` (so the entry disappears
    automatically once the registry itself is unreferenced — there is
    no stale entry for a later ``id()``-recycled object to hit); the
    ``(metric_prefix, buckets)`` half lives in an inner :class:`dict`.

    Construction of the three collectors is performed under
    :data:`_collectors_lock` and wrapped in a ``try``/``except`` so
    that if any one of them raises (e.g. ``Duplicated timeseries``
    from a partial prior registration, or a ``prometheus_client``
    version bump that rejects our labelnames), the collectors that
    *were* already registered are unregistered before the exception
    propagates — leaving the registry clean for a retry instead of
    leaking half-built collectors that would cause every subsequent
    attempt to fail the same way.
    """
    inner_key = (metric_prefix, tuple(buckets))
    with _collectors_lock:
        registry_cache = _collectors_cache.get(registry)
        if registry_cache is not None:
            cached = registry_cache.get(inner_key)
            if cached is not None:
                return cached

        labelnames = ("method", "status", "path")
        # Track every collector we successfully register so that if a
        # later constructor raises we can unregister the earlier ones
        # and leave the registry in a clean state. Without this, a
        # failure partway through would leave e.g. ``Counter``
        # registered but ``Histogram`` missing, and the next call would
        # blow up on the Counter re-registration forever.
        created: list[Counter | Gauge | Histogram] = []
        try:
            requests = Counter(
                f"{metric_prefix}_requests_total",
                "Total number of HTTP requests received by the API.",
                labelnames,
                registry=registry,
            )
            created.append(requests)
            latency = Histogram(
                f"{metric_prefix}_request_duration_seconds",
                "HTTP request handling latency in seconds (wall time).",
                labelnames,
                buckets=buckets,
                registry=registry,
            )
            created.append(latency)
            in_flight = Gauge(
                f"{metric_prefix}_requests_in_flight",
                "Number of HTTP requests currently being processed.",
                registry=registry,
            )
            created.append(in_flight)
        except Exception:
            # Best-effort cleanup of any partially-created collectors.
            # ``unregister`` raises ``KeyError`` if the collector was
            # never fully registered (defensive against future
            # ``prometheus_client`` internals); that is suppressed.
            for partial in created:
                with contextlib.suppress(KeyError):
                    registry.unregister(partial)
            raise

        collectors: dict[str, Counter | Gauge | Histogram] = {
            "requests": requests,
            "latency": latency,
            "in_flight": in_flight,
        }
        if registry_cache is None:
            registry_cache = {}
            _collectors_cache[registry] = registry_cache
        registry_cache[inner_key] = collectors
        return collectors


def reset_collectors_for_tests() -> None:
    """Drop the module-level collector cache and unregister anything we
    previously created from *each* registry that still has a live entry.

    Intended *only* for unit tests that need a clean slate between
    cases. Production code never calls this. Both the default
    :data:`REGISTRY` and any still-live throwaway registries are
    cleaned explicitly because merely clearing the cache would leave
    the registered collectors behind, which would make the *next*
    lazy init blow up with ``Duplicated timeseries`` (default registry)
    or leak state across tests that reuse the same throwaway registry.

    Entries whose registry has already been garbage-collected are
    gone from the :class:`weakref.WeakKeyDictionary` already, so there
    is nothing to unregister for them — ``KeyError`` is suppressed for
    the rare race where a registry is collected mid-iteration.

    The whole iterate-unregister-clear sequence runs under
    :data:`_collectors_lock` so that a concurrent
    :func:`_get_collectors` call on another thread cannot observe the
    cache half-cleared (which could lead it to re-register a
    collector that ``reset`` is about to unregister, or to grab a
    reference to a collector dict whose entries are being torn down).
    """
    with _collectors_lock:
        for registry, registry_cache in list(_collectors_cache.items()):
            for collectors in registry_cache.values():
                for collector in collectors.values():
                    # ``unregister`` raises ``KeyError`` if the
                    # collector is not registered against this
                    # registry (e.g. already removed by a prior
                    # reset). That is the only expected failure mode
                    # — ``AttributeError`` used to be suppressed too
                    # but masked real bugs, so it is no longer caught.
                    with contextlib.suppress(KeyError):
                        registry.unregister(collector)
        _collectors_cache.clear()


# ---------------------------------------------------------------------------
# Default exempt paths
# ---------------------------------------------------------------------------

#: Paths the middleware skips by default. The Prometheus scrape targets
#: themselves are exempt so a 15s scrape interval cannot dominate the
#: latency histogram. The bare ``/metrics`` route lives in
#: :mod:`engine.api.routes.metrics` and serves the engine's custom
#: backend; ``/metrics/prometheus`` (see
#: :mod:`engine.api.routes.prometheus_metrics`) serves the default
#: ``prometheus_client`` registry populated by this middleware.
DEFAULT_EXEMPT_PATHS: tuple[str, ...] = (
    "/metrics",
    "/metrics/prometheus",
    "/health",
    "/health/live",
    "/health/ready",
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class PrometheusMiddleware:
    """Raw-ASGI middleware recording HTTP metrics into ``prometheus_client``.

    Parameters
    ----------
    app:
        The wrapped ASGI application.
    registry:
        Optional ``CollectorRegistry`` to register collectors in.
        Defaults to :data:`prometheus_client.REGISTRY`. Tests pass a
        throwaway registry to isolate themselves from process-wide
        state.
    exempt_paths:
        Paths whose requests should not be recorded. Defaults to
        :data:`DEFAULT_EXEMPT_PATHS`. Matching uses the *normalised*
        path so e.g. ``/metrics`` is matched literally.
    metric_prefix:
        Override the ``http`` metric name prefix. Exposed for tests
        that need to avoid colliding with another ``http_requests_total``
        registered by a sibling library.
    buckets:
        Override the histogram bucket layout (seconds). Defaults to
        ``prometheus_client``'s standard latency buckets.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        registry: CollectorRegistry | None = None,
        exempt_paths: tuple[str, ...] | None = None,
        metric_prefix: str = METRIC_PREFIX,
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        self.app = app
        # Resolve the registry eagerly so the constructor fails fast on
        # a bad value rather than at first request. We do NOT pass
        # ``None`` down to prometheus_client because the lazy cache is
        # keyed (in part) on the registry object itself — always have a
        # concrete object to key on.
        self.registry: CollectorRegistry = registry if registry is not None else REGISTRY
        self.exempt_paths: frozenset[str] = frozenset(
            exempt_paths if exempt_paths is not None else DEFAULT_EXEMPT_PATHS
        )
        self._collectors = _get_collectors(
            self.registry,
            metric_prefix=metric_prefix,
            buckets=buckets if buckets is not None else _DEFAULT_BUCKETS,
        )
        # Cache the typed references so the hot path doesn't do dict
        # lookups on every request.
        self._counter: Counter = self._collectors["requests"]  # type: ignore[assignment]
        self._histogram: Histogram = self._collectors["latency"]  # type: ignore[assignment]
        self._in_flight: Gauge = self._collectors["in_flight"]  # type: ignore[assignment]

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        # Pass through non-HTTP scopes (lifespan, websocket) untouched.
        # Recording HTTP metrics against a lifespan startup event would
        # be nonsense and would also crash on the ``method`` lookup.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = normalize_path(scope.get("path", "") or "/")
        if path in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        method = scope.get("method") or "UNKNOWN"

        # Wrap ``send`` to capture the response status code off the
        # first ``http.response.start`` message. Starlette/uvicorn only
        # ever send one such message per request.
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and "status" not in status_holder
            ):
                try:
                    status_holder["status"] = int(message.get("status", 0))
                except (TypeError, ValueError):
                    status_holder["status"] = 0
            await send(message)

        self._in_flight.inc()
        start = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.monotonic() - start
            status = str(status_holder.get("status", 0))
            labels = {"method": method, "status": status, "path": path}
            try:
                self._counter.labels(**labels).inc()
                self._histogram.labels(**labels).observe(elapsed)
            finally:
                # Always decrement the gauge, even if the counter /
                # histogram writes threw — a stray exception in metric
                # recording must never leak an in-flight request.
                self._in_flight.dec()
