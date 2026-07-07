"""Prometheus-native ASGI metrics middleware (SEV-223 follow-up).

A *second*, complementary HTTP metrics middleware that emits metrics
through ``prometheus_client``'s real ``Counter`` / ``Histogram`` /
``Gauge`` collectors (rather than the pluggable
:class:`engine.observability.metrics.MetricsBackend` the existing
:class:`engine.observability.http_metrics.HttpMetricsMiddleware`
uses). The two systems coexist:

* :class:`engine.observability.http_metrics.HttpMetricsMiddleware` —
  backend-agnostic, default ``NullBackend`` so it is zero-cost until an
  operator wires an exporter. No path label.
* :class:`MetricsMiddleware` (this module) — Prometheus-native, so the
  ``/metrics`` scrape gets proper bucketed histograms and ``# HELP`` /
  ``# TYPE`` metadata for free. Adds a *normalized* path label so
  operators get per-route latency on the API surface *without* the
  cardinality explosion that raw path labelling causes.

Design goals (from the task spec)
---------------------------------

1. **Lazy collector registration.** Importing this module has *zero*
   side effects: no collector is created, nothing touches the global
   :data:`prometheus_client.REGISTRY`. Collectors are materialized only
   when :func:`get_or_create_metrics` runs — which :func:`get_default_metrics`
   defers until :func:`engine.app.create_app` calls it. This keeps test
   isolation honest (no stale series leaking onto the process-wide
   registry just because someone imported the module).

2. **Path cardinality normalisation.** A naive ``path`` label turns
   ``/api/v1/users/<uuid>`` into one time-series per user — unbounded
   cardinality, the classic Prometheus foot-gun. :func:`normalize_path`
   collapses the dynamic segments that are guaranteed to be opaque
   identifiers:

   * UUIDs (``8-4-4-4-12`` hex with dashes) → ``{id}``
   * pure-decimal numerics → ``{n}``
   * long hex blobs (``>= 16`` hex chars — sha/git/mongo/uuid-no-dashes
     territory) → ``{hex}``

   Human-readable slugs (``/api/strategies/momentum``, symbols like
   ``AAPL``) are left untouched: they are enumerable and bounded, so
   per-slug series are useful rather than dangerous.

3. **Exception-safe active-request gauge.** The in-flight gauge is
   incremented *before* the downstream app runs and decremented in a
   ``finally`` block that fires on **every** exit path — normal return,
   a raised :class:`Exception`, a server error, and crucially an
   :class:`asyncio.CancelledError` (client disconnect). The decrement
   itself is a synchronous ``Gauge.dec()`` call with no ``await`` in
   the ``finally``, so a re-cancellation cannot interrupt it mid-block.
   Every metric mutation is wrapped so a metrics failure can *never*
   break or mask a request.

The middleware is a **raw ASGI** implementation (not Starlette's
``BaseHTTPMiddleware``) for the same reasons documented on
:class:`engine.observability.middleware.CorrelationIdMiddleware`:
``BaseHTTPMiddleware`` returns from ``dispatch`` before the streaming
body is flushed, which would short-circuit the duration histogram and
let the active-request gauge leak when a response streams slowly.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger()

__all__ = [
    "MetricsMiddleware",
    "get_default_metrics",
    "get_or_create_metrics",
    "normalize_path",
    "reset_for_testing",
]


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------

#: Canonical UUID v1-v5 shape: ``8-4-4-4-12`` hex digits separated by dashes.
#: Anchored so a segment must be *exactly* a UUID (not merely contain one).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
#: Long opaque hex blob. ``>= 16`` chars is the threshold at which a hex
#: string is overwhelmingly likely to be a machine-generated identifier
#: (sha1/sha256 = 40/64, git = 7-40, mongo ObjectId = 24, uuid-no-dashes = 32,
#: ksuid = 27) rather than a human-readable slug. Short hex like ``dead`` or
#: ``cafebabe`` (<= 8) is left alone so readable routes survive.
_LONG_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")


def _normalize_segment(segment: str) -> str:
    """Collapse a single path segment that looks like an opaque identifier.

    Order matters: UUID is checked first (it is the most specific shape),
    then pure-decimal numeric, then long-hex. A segment matching none of
    those is returned verbatim so enumerable slugs (strategy names,
    symbols, route names) keep their useful per-route cardinality.
    """
    if not segment:
        return segment
    if _UUID_RE.match(segment):
        return "{id}"
    if segment.isdigit():
        return "{n}"
    if _LONG_HEX_RE.match(segment):
        return "{hex}"
    return segment


def normalize_path(path: str) -> str:
    """Return a cardinality-safe version of ``path``.

    Splits on ``/`` and collapses each opaque-identifier segment. The
    leading slash, trailing slash, and segment count are preserved so
    ``/api/v1/users/`` stays distinguishable from ``/api/v1/users``.

    A query string, if somehow present (ASGI ``scope["path"]`` normally
    excludes it), is dropped — query parameters are never a label.
    """
    if not path:
        return path
    # Defensive: never let a query string leak into a label.
    no_query = path.split("?", 1)[0]
    return "/".join(_normalize_segment(seg) for seg in no_query.split("/"))


#: Maps the hundreds digit of an HTTP status code to its class label.
#: Classifying via a single dict lookup (rather than a chain of literal
#: boundary comparisons) keeps every status boundary a named constant and
#: shrinks the classifier to a single computed return.
_STATUS_CLASSES: dict[int, str] = {
    1: "1xx",
    2: "2xx",
    3: "3xx",
    4: "4xx",
    5: "5xx",
}
#: Divisor extracting the status-class hundreds digit (kept named so the
#: classifier carries no bare magic number).
_HUNDREDS = 100


def _status_class(status: int | None) -> str:
    """Bucket an HTTP status code into a bounded label value."""
    if status is None:
        return "unknown"
    return _STATUS_CLASSES.get(status // _HUNDREDS, "unknown")


# ---------------------------------------------------------------------------
# Collector registry / lazy construction
# ---------------------------------------------------------------------------

_NAMESPACE = "nexus"
# Collector base names. The ``Counter`` keeps its ``_total`` suffix because
# we pass it explicitly; prometheus_client then also derives ``_created``.
_REQUESTS_NAME = "http_api_requests_total"
_DURATION_NAME = "http_api_request_duration_seconds"
_ACTIVE_NAME = "http_api_requests_active"

# Fully-qualified metric names as they appear in a registry (namespace +
# base). Used by the duplicate-registration fallback below.
_REQUESTS_FULL = f"{_NAMESPACE}_{_REQUESTS_NAME}"
_DURATION_FULL = f"{_NAMESPACE}_{_DURATION_NAME}"
_ACTIVE_FULL = f"{_NAMESPACE}_{_ACTIVE_NAME}"


@dataclass(frozen=True, slots=True)
class _Metrics:
    """Bundle of the three collectors this middleware owns.

    Frozen + slotted so the bundle is hashable-by-identity-stable and
    immutable: once :func:`get_or_create_metrics` hands one out, callers
    cannot rewire a counter out from under the middleware.
    """

    requests: Counter
    duration: Histogram
    active: Gauge


# Per-registry cache. Keyed by the registry object itself — ``CollectorRegistry``
# instances are hashable (default ``object`` identity hash) and the default
# ``REGISTRY`` is a process-wide singleton, so the common case (one app, one
# registry) creates collectors exactly once regardless of how many times
# ``create_app()`` runs. Keeping the registry as the key (not ``id(registry)``)
# holds a strong reference, preventing a stale id-reuse bug if a throwaway
# test registry is GC'd.
_REGISTRY_CACHE: dict[CollectorRegistry, _Metrics] = {}
# Construction is guarded so two concurrent first-callers (e.g. an async
# lifespan and a request landing simultaneously) cannot race into a
# ``Duplicated timeseries`` ValueError.
_LOCK = threading.Lock()


def _build_metrics(registry: CollectorRegistry) -> _Metrics:
    """Construct the three collectors on ``registry``.

    On a ``ValueError`` ("Duplicated timeseries") — which happens when a
    collector with our names was already registered through some other
    path (a stale test fixture, a prior import in a long-lived REPL) — we
    fall back to reusing the existing collectors rather than crashing.
    Prometheus identifies collectors by name, so reusing the same-named
    object is semantically identical to the freshly built one.
    """
    try:
        requests = Counter(
            _REQUESTS_NAME,
            "Total HTTP API requests handled by MetricsMiddleware.",
            ["method", "path", "status"],
            namespace=_NAMESPACE,
            registry=registry,
        )
        duration = Histogram(
            _DURATION_NAME,
            "HTTP API request latency in seconds (MetricsMiddleware).",
            ["method", "path"],
            namespace=_NAMESPACE,
            registry=registry,
        )
        active = Gauge(
            _ACTIVE_NAME,
            "Number of HTTP API requests currently in flight.",
            ["method"],
            namespace=_NAMESPACE,
            registry=registry,
        )
    except ValueError:
        reused = _lookup_existing(registry)
        if reused is not None:
            return reused
        raise
    return _Metrics(requests=requests, duration=duration, active=active)


def _lookup_existing(registry: CollectorRegistry) -> _Metrics | None:
    """Recover a ``_Metrics`` bundle from already-registered collectors.

    Returns ``None`` if any of the three names is missing or is the wrong
    type — in that case the caller re-raises the original ``ValueError``
    rather than silently building a Frankenstein bundle.
    """
    names: dict[str, object] = getattr(registry, "_names_to_collectors", {}) or {}
    requests = names.get(_REQUESTS_FULL)
    duration = names.get(_DURATION_FULL)
    active = names.get(_ACTIVE_FULL)
    if not (
        isinstance(requests, Counter)
        and isinstance(duration, Histogram)
        and isinstance(active, Gauge)
    ):
        return None
    return _Metrics(requests=requests, duration=duration, active=active)  # type: ignore[arg-type]


def get_or_create_metrics(registry: CollectorRegistry) -> _Metrics:
    """Return the cached ``_Metrics`` for ``registry``, building it on demand.

    Idempotent: the same registry always yields the same ``_Metrics``
    object, so repeated :func:`engine.app.create_app` calls in one process
    (ubiquitous in the test suite) register collectors exactly once.
    """
    with _LOCK:
        cached = _REGISTRY_CACHE.get(registry)
        if cached is not None:
            return cached
        metrics = _build_metrics(registry)
        _REGISTRY_CACHE[registry] = metrics
        return metrics


def get_default_metrics() -> _Metrics:
    """Return the ``_Metrics`` bound to the process-wide default registry.

    Deliberately a *function* and deliberately not called at import time:
    it is invoked from :func:`engine.app.create_app` (and from a
    ``MetricsMiddleware`` constructed without explicit metrics) so that
    merely importing this module never registers anything on
    :data:`prometheus_client.REGISTRY`.
    """
    return get_or_create_metrics(REGISTRY)


def reset_for_testing() -> None:
    """Tear down every cached bundle and unregister its collectors.

    Test-only escape hatch for suites that need a pristine
    :data:`prometheus_client.REGISTRY` between cases (the default registry
    is a process singleton and otherwise accumulates series across the
    whole run). Unregistering is best-effort: a collector that has already
    gone away (or a registry that rejects ``unregister``) is logged and
    skipped so one stuck entry can't poison the rest of the teardown.
    """
    with _LOCK:
        for registry, metrics in list(_REGISTRY_CACHE.items()):
            for collector in (metrics.requests, metrics.duration, metrics.active):
                try:
                    registry.unregister(collector)
                except Exception:
                    logger.warning(
                        "metrics.reset.unregister_failed",
                        collector=getattr(collector, "_name", "?"),
                    )
        _REGISTRY_CACHE.clear()


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------


class MetricsMiddleware:
    """Raw-ASGI middleware emitting Prometheus metrics for ``/api`` routes.

    Wraps the downstream app and, for HTTP requests whose path starts with
    ``api_prefix`` (default ``/api``), records:

    * ``nexus_http_api_requests_total`` — counter labelled by
      ``method``, ``path`` (normalised) and ``status`` (status class).
    * ``nexus_http_api_request_duration_seconds`` — histogram labelled by
      ``method`` and ``path`` (normalised), observed once per request.
    * ``nexus_http_api_requests_active`` — gauge labelled by ``method``,
      incremented before the downstream app runs and decremented in a
      ``finally`` that runs on *every* exit path.

    Requests outside ``/api`` (health checks, the ``/metrics`` scrape
    itself, static assets, docs) pass straight through with zero
    instrumentation, so an operator scraping ``/metrics`` never observes
    its own scrape. Non-HTTP scopes (``lifespan``, ``websocket``) are
    likewise passed through unchanged.

    Parameters
    ----------
    app:
        The wrapped ASGI application.
    metrics:
        Optional pre-built :class:`_Metrics` bundle. Lets tests (and
        operators with a bespoke registry) inject exactly the collectors
        they want. When omitted, ``registry`` is consulted; when that is
        also omitted, the default-registry bundle is resolved **lazily on
        the first request** so constructing the middleware has no side
        effects.
    registry:
        Optional explicit registry. Ignored when ``metrics`` is supplied.
    api_prefix:
        Path prefix that selects which requests get instrumented.
        Default ``/api``. An empty string instruments everything.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: _Metrics | None = None,
        registry: CollectorRegistry | None = None,
        api_prefix: str = "/api",
    ) -> None:
        self.app = app
        self._explicit_metrics = metrics
        self._registry = registry
        self.api_prefix = api_prefix
        # Resolved lazily so construction is side-effect-free; see _metrics.
        self._resolved: _Metrics | None = None

    @property
    def metrics(self) -> _Metrics:
        """Resolve the collector bundle on first use, then cache it.

        Lazy resolution means :func:`engine.app.create_app` can
        ``add_middleware(MetricsMiddleware)`` without registering anything
        until a request actually arrives — keeping import *and* app
        construction free of registry side effects.
        """
        if self._explicit_metrics is not None:
            return self._explicit_metrics
        if self._resolved is None:
            if self._registry is not None:
                self._resolved = get_or_create_metrics(self._registry)
            else:
                self._resolved = get_default_metrics()
        return self._resolved

    def _should_instrument(self, path: str) -> bool:
        """Only ``/api`` (or the configured prefix) paths get instrumented."""
        if not self.api_prefix:
            return True
        return path.startswith(self.api_prefix)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan, websocket, and any other non-http scope: pass through.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if not self._should_instrument(path):
            # Non-API path (health, docs, the /metrics scrape, ...): never
            # instrument, so a scrape can't observe itself and so static /
            # documentation traffic stays off the API latency histograms.
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "UNKNOWN").upper()
        normalized = normalize_path(path)
        metrics = self.metrics

        # --- Active-request gauge: increment, ALWAYS decrement in finally ---
        # ``inc()`` sits *outside* the try block: if it raises we never enter
        # the try, so the finally never runs and we don't over-decrement.
        # Once we are inside the try the finally is guaranteed to run,
        # including on asyncio.CancelledError (client disconnect) — the
        # decrement is a synchronous call with no await point, so a
        # re-cancellation cannot interrupt it mid-block.
        try:
            metrics.active.labels(method=method).inc()
        except Exception:
            logger.exception("metrics.active_gauge.increment_failed", method=method)
            # We could not mark the request in-flight; skip straight to the
            # downstream app. There is nothing to decrement later.
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            # Capture the status from the first http.response.start. Starlette
            # sends exactly one; guarding with ``not in status_holder`` keeps
            # us robust against a misbehaving downstream that emits two.
            if message["type"] == "http.response.start" and "status" not in status_holder:
                try:
                    status_holder["status"] = int(message.get("status", 0))
                except (TypeError, ValueError):
                    status_holder["status"] = 0
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 1) Gauge decrement — exception-safe, synchronous, no await.
            try:
                metrics.active.labels(method=method).dec()
            except Exception:
                logger.exception("metrics.active_gauge.decrement_failed", method=method)

            # 2) Duration histogram — observe wall time regardless of outcome.
            elapsed = time.monotonic() - start
            try:
                metrics.duration.labels(method=method, path=normalized).observe(elapsed)
            except Exception:
                logger.exception("metrics.histogram.observe_failed", path=normalized)

            # 3) Request counter — one increment per terminated request.
            status = _status_class(status_holder.get("status"))
            try:
                metrics.requests.labels(method=method, path=normalized, status=status).inc()
            except Exception:
                logger.exception("metrics.counter.increment_failed", path=normalized)
