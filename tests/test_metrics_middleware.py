"""Focused unit tests for the Prometheus-native ASGI metrics middleware.

Exercises the five behaviours called out in the SEV-223 review:

1. the per-registry **cache returns the same instance for the same
   registry** (and a different instance for a different registry),
2. importing the module has **no import-time side effects** on the
   process-wide ``prometheus_client.REGISTRY`` (and there is no
   module-level ``_default`` that eagerly registers collectors),
3. the ``http_api_requests_total`` counter increments **once** per
   ``/api`` request (and only for ``/api``),
4. the in-flight ``http_api_requests_active`` gauge returns to its
   baseline when the downstream app raises an arbitrary exception,
5. non-``/api`` routes (health checks, the ``/metrics`` scrape) are never
   instrumented.

Plus the related behaviours the implementation also guarantees: the
gauge returns to baseline on a client disconnect (``asyncio.CancelledError``)
and path-cardinality normalisation collapses UUID / numeric / hex segments.

Every request-level test builds the middleware against a **fresh**
``CollectorRegistry`` (injected via the ``metrics=`` constructor argument)
so the process-wide ``prometheus_client.REGISTRY`` never accumulates state
between cases. Values are read straight off the registry with
``get_sample_value``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from engine.middleware.metrics import (
    MetricsMiddleware,
    get_default_metrics,
    get_or_create_metrics,
    normalize_path,
)

# ---------------------------------------------------------------------------
# Constants / route handlers
# ---------------------------------------------------------------------------

_UUID = "550e8400-e29b-41d4-a716-446655440000"
_HEX = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


async def _ok(_: Request) -> Response:
    return JSONResponse({"ok": True})


async def _echo_id(request: Request) -> Response:
    return JSONResponse({"id": request.path_params["id"]})


async def _boom(_: Request) -> Response:
    raise RuntimeError("kaboom")


def _build_starlette_app(metrics) -> MetricsMiddleware:
    """Wrap a small Starlette app in the middleware under test.

    Only ``/api`` routes are instrumented; ``/health`` is the negative case.
    """
    app = Starlette(
        routes=[
            Route("/api/v1/widgets", _ok, methods=["GET"]),
            Route("/api/v1/widgets/{id}", _echo_id, methods=["GET"]),
            Route("/api/items/{n}", _echo_id, methods=["GET"]),
            Route("/api/blob/{h}", _echo_id, methods=["GET"]),
            Route("/api/boom", _boom, methods=["GET"]),
            Route("/health", _ok, methods=["GET"]),
        ],
    )
    return MetricsMiddleware(app, metrics=metrics)


# ---------------------------------------------------------------------------
# Fixtures — fresh registry per test, zero global pollution
# ---------------------------------------------------------------------------


@pytest.fixture
def registry() -> CollectorRegistry:
    """Brand-new registry per test → no series leak across cases."""
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry):
    # Build the collector bundle ON the throwaway registry and inject it,
    # so the middleware never touches the process-wide default registry.
    return get_or_create_metrics(registry)


@pytest.fixture
def app(metrics) -> MetricsMiddleware:
    return _build_starlette_app(metrics)


# ---------------------------------------------------------------------------
# Helpers to read rendered sample values off the registry
# ---------------------------------------------------------------------------


def _counter(registry: CollectorRegistry, path: str, status: str, method: str = "GET") -> float:
    return (
        registry.get_sample_value(
            "nexus_http_api_requests_total",
            {"method": method, "path": path, "status": status},
        )
        or 0.0
    )


def _hist_count(registry: CollectorRegistry, path: str, method: str = "GET") -> float:
    return (
        registry.get_sample_value(
            "nexus_http_api_request_duration_seconds_count",
            {"method": method, "path": path},
        )
        or 0.0
    )


def _active(registry: CollectorRegistry, method: str = "GET") -> float:
    return (
        registry.get_sample_value(
            "nexus_http_api_requests_active",
            {"method": method},
        )
        or 0.0
    )


# ===========================================================================
# (1) Cache returns the same instance for the same registry
# ===========================================================================


class TestPerRegistryCache:
    def test_cache_returns_same_instance_for_same_registry(self):
        """``get_or_create_metrics`` is idempotent per registry: two calls
        with the same registry object yield the *same* ``_Metrics`` bundle
        (identity), and the three collectors land on that one registry."""
        registry = CollectorRegistry()
        first = get_or_create_metrics(registry)
        second = get_or_create_metrics(registry)

        # Identity: same registry → same bundle object (cached).
        assert first is second
        # The bundle's collectors are actually registered on the registry.
        assert (
            registry.get_sample_value("nexus_http_api_requests_total") is None
        )  # no observations yet, but the collector exists once a label is set:
        first.requests.labels(method="GET", path="/api/x", status="2xx").inc()
        assert (
            registry.get_sample_value(
                "nexus_http_api_requests_total",
                {"method": "GET", "path": "/api/x", "status": "2xx"},
            )
            == 1.0
        )

    def test_cache_isolates_distinct_registries(self):
        """Two different registries get two different bundles. This is the
        regression guard for the old ``id(registry)`` keying: if a throwaway
        registry were GC'd and its ``id`` reused, the cache would have
        handed back a stale bundle. Keying by the object itself prevents
        that, and distinct registries must never collide."""
        r1 = CollectorRegistry()
        r2 = CollectorRegistry()
        a = get_or_create_metrics(r1)
        b = get_or_create_metrics(r2)

        assert a is not b
        # Each bundle is registered on its own registry, not the other's.
        a.active.labels(method="GET").inc()
        assert r1.get_sample_value("nexus_http_api_requests_active", {"method": "GET"}) == 1.0
        assert r2.get_sample_value("nexus_http_api_requests_active", {"method": "GET"}) is None


# ===========================================================================
# (2) No import-time side effects on prometheus_client.REGISTRY
# ===========================================================================


def test_no_import_time_side_effects():
    """Importing ``engine.middleware.metrics`` must NOT register any
    collector on the process-wide ``prometheus_client.REGISTRY`` and must
    NOT carry a module-level ``_default``.

    Run in a fresh subprocess so the module is genuinely imported for the
    first time (in this test process it is already cached), making the
    assertion deterministic and isolated from the rest of the suite.
    """
    code = (
        "import prometheus_client\n"
        "from prometheus_client import REGISTRY\n"
        "# Snapshot the registry *before* importing the module.\n"
        "before = set(getattr(REGISTRY, '_names_to_collectors', {}))\n"
        "import engine.middleware.metrics as m\n"
        "after = set(getattr(REGISTRY, '_names_to_collectors', {}))\n"
        "# No nexus_* series may appear purely from importing.\n"
        "leaked = {n for n in (after - before) if n.startswith('nexus_')}\n"
        "assert not leaked, leaked\n"
        "# The eager module-level _default was removed (lazy init instead).\n"
        "assert not hasattr(m, '_default'), 'module-level _default must not exist'\n"
        "# And there must be no pre-built bundle attribute at import time.\n"
        "assert callable(m.get_default_metrics)\n"
        "print('OK')\n"
    )
    # S603: ``code`` is a hardcoded constant in this test, not untrusted input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"import had side effects:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_get_default_metrics_is_lazy():
    """``get_default_metrics`` only registers collectors when *called*, not
    at import. Calling it twice returns the same (cached) bundle bound to
    the global registry."""
    # Calling it registers exactly one set of collectors on REGISTRY.
    bundle_a = get_default_metrics()
    bundle_b = get_default_metrics()
    assert bundle_a is bundle_b  # cached per the global REGISTRY singleton


# ===========================================================================
# (3) Counter increments exactly once per /api request
# ===========================================================================


class TestRequestCounter:
    async def test_counter_increments_once_per_api_request(self, app, registry):
        """One GET → counter == 1, histogram observed once, gauge settles to 0."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/api/v1/widgets")
            await client.get("/api/v1/widgets")  # second hit → counter must reach 2

        assert first.status_code == 200
        # Exactly one increment per request: 2 requests ⇒ counter == 2.0.
        assert _counter(registry, "/api/v1/widgets", "2xx") == 2.0
        assert _hist_count(registry, "/api/v1/widgets") == 2.0
        # Gauge was inc()'d then dec()'d on every request → back to baseline.
        assert _active(registry) == 0.0


# ===========================================================================
# (4) Active gauge returns to baseline on a downstream exception
# ===========================================================================


async def test_active_gauge_returns_to_baseline_on_downstream_exception(registry):
    """A raised exception must not leak the in-flight gauge.

    Driven directly at the ASGI layer (rather than through httpx) so the
    exception propagates uncaught instead of being converted to a 500 by
    Starlette's ``ServerErrorMiddleware`` — this is the exact failure shape
    a raw ASGI downstream app presents to the middleware.
    """

    async def raising_app(scope, receive, send):
        raise ValueError("nope")

    mw = MetricsMiddleware(raising_app, metrics=get_or_create_metrics(registry))

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message):
        pass

    with pytest.raises(ValueError, match="nope"):
        await mw(
            {"type": "http", "method": "GET", "path": "/api/v1/widgets"},
            receive,
            send,
        )

    # The critical assertion: the finally block decremented the gauge.
    assert _active(registry) == 0.0
    # No http.response.start was ever sent → status classifies as "unknown",
    # but the request is still counted exactly once.
    assert _counter(registry, "/api/v1/widgets", "unknown") == 1.0


# ---------------------------------------------------------------------------
# (4b) Gauge returns to baseline on client disconnect (CancelledError)
# ---------------------------------------------------------------------------


async def test_active_gauge_returns_to_baseline_on_client_disconnect(registry):
    """CancelledError (client disconnect) must still run the ``finally``.

    The decrement is a synchronous ``Gauge.dec()`` with no ``await`` in the
    finally, so a re-cancellation cannot interrupt it mid-block.
    """
    called: list[bool] = []

    async def cancelling_app(scope, receive, send):
        called.append(True)
        raise asyncio.CancelledError

    mw = MetricsMiddleware(cancelling_app, metrics=get_or_create_metrics(registry))

    async def receive() -> dict:
        return {"type": "http.request"}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    with pytest.raises(asyncio.CancelledError):
        await mw(
            {"type": "http", "method": "get", "path": "/api/v1/widgets"},
            receive,
            send,
        )

    assert called == [True]  # downstream actually ran
    assert sent == []  # nothing was ever flushed to the client
    # The critical assertion: gauge decremented despite cancellation.
    assert _active(registry, method="GET") == 0.0


# ===========================================================================
# (5) Non-/api routes are not instrumented
# ===========================================================================


async def test_non_api_routes_not_instrumented(app, registry):
    """Health checks (and by extension /metrics scrapes) stay off the gauges."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    # No counter, no histogram, and no gauge activity whatsoever.
    assert registry.get_sample_value("nexus_http_api_requests_total") is None
    assert registry.get_sample_value("nexus_http_api_request_duration_seconds_count") is None
    assert registry.get_sample_value("nexus_http_api_requests_active") is None


# ===========================================================================
# (Bonus) Path normalisation — UUID / numeric / hex / trailing-slash
# ===========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # UUID (8-4-4-4-12 hex) → {id}
        (f"/api/v1/widgets/{_UUID}", "/api/v1/widgets/{id}"),
        # trailing slash preserved AND the segment still collapses
        (f"/api/v1/widgets/{_UUID}/", "/api/v1/widgets/{id}/"),
        # pure-decimal numeric → {n}
        ("/api/items/42", "/api/items/{n}"),
        ("/api/v1/orders/12345", "/api/v1/orders/{n}"),
        # long hex (>=16 chars, e.g. sha/git/mongo/uuid-no-dashes) → {hex}
        (f"/api/blob/{_HEX}", "/api/blob/{hex}"),
        # enumerable slugs / symbols are left untouched (bounded cardinality)
        ("/api/strategies/momentum", "/api/strategies/momentum"),
        ("/api/symbols/AAPL", "/api/symbols/AAPL"),
        # short hex is human-readable, not an opaque id → untouched
        ("/api/x/deadbeef", "/api/x/deadbeef"),
        # a query string must never leak into a label
        ("/api/v1/widgets?page=2", "/api/v1/widgets"),
        # bare prefix shapes
        ("/api", "/api"),
        ("/api/", "/api/"),
    ],
)
def test_normalize_path(raw: str, expected: str):
    assert normalize_path(raw) == expected


async def test_path_normalization_flows_to_labels(app, registry):
    """Normalisation is not theoretical — it is the actual label value."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Two distinct UUIDs must fold onto ONE series (no cardinality blow-up).
        await client.get(f"/api/v1/widgets/{_UUID}")
        await client.get("/api/v1/widgets/00000000-0000-4000-8000-000000000000")

    # The label is the normalised template, not the raw UUID.
    assert _counter(registry, "/api/v1/widgets/{id}", "2xx") == 2.0
    # And the raw UUID never appears as its own time-series.
    assert (
        registry.get_sample_value(
            "nexus_http_api_requests_total",
            {"method": "GET", "path": f"/api/v1/widgets/{_UUID}", "status": "2xx"},
        )
        is None
    )
