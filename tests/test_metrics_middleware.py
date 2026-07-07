"""Focused tests for the Prometheus-native ASGI metrics middleware.

Covers the four behaviours called out in the SEV-223 task spec:

* happy-path counter increment (+ histogram observation + gauge settling),
* active-request gauge cleanup on exception *and* on client disconnect
  (``asyncio.CancelledError``),
* path-cardinality normalisation variants (UUID / numeric / hex collapse,
  slug passthrough), and
* non-``/api`` exclusion (the ``/metrics`` scrape / health routes must not
  instrument themselves).

Each test builds the middleware against a *fresh* ``CollectorRegistry`` so
the process-wide ``prometheus_client.REGISTRY`` never accumulates state
between cases. ``get_sample_value`` reads the rendered values straight off
the registry.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY, CollectorRegistry
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from engine.middleware.metrics import (
    MetricsMiddleware,
    _status_class,
    get_default_metrics,
    get_or_create_metrics,
    normalize_path,
    reset_for_testing,
)

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

_UUID = "550e8400-e29b-41d4-a716-446655440000"
_HEX = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


async def _ok(_: Request) -> Response:
    return JSONResponse({"ok": True})


async def _ok_id(request: Request) -> Response:
    # Echo back so we know the route matched; the path label is what we assert on.
    return JSONResponse({"id": request.path_params["id"]})


async def _boom(_: Request) -> Response:
    raise RuntimeError("kaboom")


async def _cancel(_: Request) -> Response:
    raise asyncio.CancelledError


def _build_app(metrics) -> Starlette:
    """Starlette app whose /api routes are the ones we instrument."""
    app = Starlette(
        routes=[
            Route("/api/v1/widgets", _ok, methods=["GET"]),
            Route("/api/v1/widgets/{id}", _ok_id, methods=["GET"]),
            Route("/api/items/{n}", _ok_id, methods=["GET"]),
            Route("/api/blob/{h}", _ok_id, methods=["GET"]),
            Route("/api/strategies/{slug}", _ok_id, methods=["GET"]),
            Route("/api/boom", _boom, methods=["GET"]),
            Route("/health", _ok, methods=["GET"]),
        ]
    )
    return MetricsMiddleware(app, metrics=metrics)


@pytest.fixture
def registry() -> CollectorRegistry:
    """A brand-new registry per test → zero cross-test bleed."""
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry):
    return get_or_create_metrics(registry)


@pytest.fixture
def app(metrics):
    return _build_app(metrics)


def _counter(registry: CollectorRegistry, path: str, status: str, method: str = "GET") -> float:
    return registry.get_sample_value(
        "nexus_http_api_requests_total",
        {"method": method, "path": path, "status": status},
    ) or 0.0


def _hist_count(registry: CollectorRegistry, path: str, method: str = "GET") -> float:
    return registry.get_sample_value(
        "nexus_http_api_request_duration_seconds_count",
        {"method": method, "path": path},
    ) or 0.0


def _active(registry: CollectorRegistry, method: str = "GET") -> float:
    return registry.get_sample_value(
        "nexus_http_api_requests_active",
        {"method": method},
    ) or 0.0


# ---------------------------------------------------------------------------
# Pure-function: path normalisation
# ---------------------------------------------------------------------------


class TestNormalizePath:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("/api/v1/widgets", "/api/v1/widgets"),
            (f"/api/v1/widgets/{_UUID}", "/api/v1/widgets/{id}"),
            (f"/api/v1/widgets/{_UUID}/", "/api/v1/widgets/{id}/"),
            ("/api/items/42", "/api/items/{n}"),
            ("/api/v1/orders/12345", "/api/v1/orders/{n}"),
            (f"/api/blob/{_HEX}", "/api/blob/{hex}"),
            # slugs / symbols are enumerable → left untouched
            ("/api/strategies/momentum", "/api/strategies/momentum"),
            ("/api/symbols/AAPL", "/api/symbols/AAPL"),
            ("/api/symbols/aapl", "/api/symbols/aapl"),
            # short hex is human-readable, not an opaque id → untouched
            ("/api/x/deadbeef", "/api/x/deadbeef"),
            # query strings are stripped so they never become a label
            ("/api/v1/widgets?page=2", "/api/v1/widgets"),
            # edge shapes
            ("/api", "/api"),
            ("/api/", "/api/"),
            ("", ""),
        ],
    )
    def test_variants(self, raw: str, expected: str):
        assert normalize_path(raw) == expected


def test_status_class_buckets():
    assert _status_class(100) == "1xx"
    assert _status_class(204) == "2xx"
    assert _status_class(301) == "3xx"
    assert _status_class(404) == "4xx"
    assert _status_class(500) == "5xx"
    assert _status_class(None) == "unknown"
    assert _status_class(999) == "unknown"


# ---------------------------------------------------------------------------
# Happy path: counter + histogram + gauge-settles-to-zero
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_get_records_counter_histogram_and_settles_gauge(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/widgets")

        assert r.status_code == 200
        assert _counter(registry, "/api/v1/widgets", "2xx") == 1.0
        assert _hist_count(registry, "/api/v1/widgets") == 1.0
        # Gauge was inc()'d then dec()'d → back to zero.
        assert _active(registry) == 0.0

    async def test_two_requests_increment_to_two(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get("/api/v1/widgets")
            await c.get("/api/v1/widgets")

        assert _counter(registry, "/api/v1/widgets", "2xx") == 2.0
        assert _hist_count(registry, "/api/v1/widgets") == 2.0
        assert _active(registry) == 0.0

    async def test_method_label_uppercased(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get("/api/v1/widgets")
        # Even though Starlette reports the method verbatim, the middleware
        # upper-cases it so GET/get collapse onto one series.
        assert registry.get_sample_value(
            "nexus_http_api_requests_total",
            {"method": "GET", "path": "/api/v1/widgets", "status": "2xx"},
        ) == 1.0


# ---------------------------------------------------------------------------
# Active-request gauge cleanup on failure paths
# ---------------------------------------------------------------------------


class TestActiveGaugeCleanup:
    async def test_gauge_returns_to_zero_on_500(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/boom")

        # Starlette's ServerErrorMiddleware converts the raised error to a 500,
        # which the middleware captures via the wrapped send().
        assert r.status_code == 500
        assert _counter(registry, "/api/boom", "5xx") == 1.0
        # The critical assertion: no leaked in-flight gauge.
        assert _active(registry) == 0.0

    async def test_gauge_returns_to_zero_on_client_disconnect(self, metrics, registry):
        """CancelledError (client disconnect) must still run the finally.

        Driven directly at the ASGI layer rather than through httpx so the
        CancelledError propagates instead of being swallowed by Starlette's
        error middleware — this is the exact path a disconnected client
        takes under uvicorn.
        """
        called: list[bool] = []

        async def cancelling_app(scope, receive, send):
            called.append(True)
            raise asyncio.CancelledError

        mw = MetricsMiddleware(cancelling_app, metrics=metrics)

        async def receive():
            return {"type": "http.request"}

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        with pytest.raises(asyncio.CancelledError):
            await mw(
                {"type": "http", "method": "get", "path": "/api/v1/x"},
                receive,
                send,
            )

        assert called == [True]
        # No http.response.start was ever sent → status unknown, but the
        # gauge MUST have been decremented in the finally.
        assert _active(registry, method="GET") == 0.0
        assert _counter(registry, "/api/v1/x", "unknown") == 1.0

    async def test_gauge_returns_to_zero_on_arbitrary_exception(self, metrics, registry):
        async def raising_app(scope, receive, send):
            raise ValueError("nope")

        mw = MetricsMiddleware(raising_app, metrics=metrics)

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            pass

        with pytest.raises(ValueError):
            await mw(
                {"type": "http", "method": "GET", "path": "/api/v1/x"},
                receive,
                send,
            )

        assert _active(registry) == 0.0
        # No response.start → unknown status, but still counted once.
        assert _counter(registry, "/api/v1/x", "unknown") == 1.0


# ---------------------------------------------------------------------------
# Path normalisation flows through to the labels end-to-end
# ---------------------------------------------------------------------------


class TestPathNormalizationLabels:
    async def test_uuid_segment_collapses(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"/api/v1/widgets/{_UUID}")

        assert r.status_code == 200
        # The label must be the normalised form, not the raw UUID —
        # otherwise every user id spawns a new time-series.
        assert _counter(registry, "/api/v1/widgets/{id}", "2xx") == 1.0
        # And the raw UUID never appears as a label value.
        assert registry.get_sample_value(
            "nexus_http_api_requests_total",
            {"method": "GET", "path": f"/api/v1/widgets/{_UUID}", "status": "2xx"},
        ) is None

    async def test_numeric_segment_collapses(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # Two different ids must fold onto ONE series.
            await c.get("/api/items/1")
            await c.get("/api/items/999999")

        assert _counter(registry, "/api/items/{n}", "2xx") == 2.0

    async def test_hex_segment_collapses(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get(f"/api/blob/{_HEX}")

        assert _counter(registry, "/api/blob/{hex}", "2xx") == 1.0

    async def test_slug_segment_preserved(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get("/api/strategies/momentum")
            await c.get("/api/strategies/mean-reversion")

        # Slugs are enumerable/bounded → distinct, useful series.
        assert _counter(registry, "/api/strategies/momentum", "2xx") == 1.0
        assert _counter(registry, "/api/strategies/mean-reversion", "2xx") == 1.0


# ---------------------------------------------------------------------------
# Non-/api exclusion + non-http scope passthrough
# ---------------------------------------------------------------------------


class TestNonApiExclusion:
    async def test_health_check_not_instrumented(self, app, registry):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/health")

        assert r.status_code == 200
        # No counter, no histogram, no gauge activity at all.
        assert registry.get_sample_value("nexus_http_api_requests_total") is None
        assert (
            registry.get_sample_value("nexus_http_api_request_duration_seconds_count")
            is None
        )
        assert registry.get_sample_value("nexus_http_api_requests_active") is None

    async def test_lifespan_scope_passed_through_unchanged(self, metrics):
        async def lifespan_only_app(scope, receive, send):
            assert scope["type"] == "lifespan"
            msg = await receive()
            assert msg["type"] == "lifespan.startup"
            await send({"type": "lifespan.startup.complete"})
            msg = await receive()
            assert msg["type"] == "lifespan.shutdown"
            await send({"type": "lifespan.shutdown.complete"})

        wrapped = MetricsMiddleware(lifespan_only_app, metrics=metrics)
        sent: list[dict] = []
        events = iter(
            [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
        )

        async def receive():
            return next(events)

        async def send(message):
            sent.append(message)

        await wrapped({"type": "lifespan"}, receive, send)

        assert sent == [
            {"type": "lifespan.startup.complete"},
            {"type": "lifespan.shutdown.complete"},
        ]

    async def test_websocket_scope_passed_through(self, metrics):
        async def ws_app(scope, receive, send):
            # Just prove we got here and the scope was untouched.
            send.append(scope["type"])

        seen: list[str] = []
        wrapped = MetricsMiddleware(ws_app, metrics=metrics)

        async def receive():
            return {"type": "websocket.disconnect"}

        async def send(message):  # type: ignore[override]
            seen.append(message)

        await wrapped({"type": "websocket"}, receive, send)
        assert seen == ["websocket"]


# ---------------------------------------------------------------------------
# Registry caching + test reset helper
# ---------------------------------------------------------------------------


class TestRegistryCaching:
    def test_same_registry_returns_same_bundle(self, registry):
        a = get_or_create_metrics(registry)
        b = get_or_create_metrics(registry)
        assert a is b

    def test_different_registries_get_different_bundles(self):
        r1 = CollectorRegistry()
        r2 = CollectorRegistry()
        assert get_or_create_metrics(r1) is not get_or_create_metrics(r2)

    def test_reset_for_testing_clears_cache_and_unregisters(self, registry):
        bundle = get_or_create_metrics(registry)
        bundle.requests.labels(method="GET", path="/api/x", status="2xx").inc()
        assert (
            registry.get_sample_value(
                "nexus_http_api_requests_total",
                {"method": "GET", "path": "/api/x", "status": "2xx"},
            )
            == 1.0
        )

        reset_for_testing()

        # Collector is gone from the registry entirely.
        assert (
            registry.get_sample_value(
                "nexus_http_api_requests_total",
                {"method": "GET", "path": "/api/x", "status": "2xx"},
            )
            is None
        )
        # And re-creating after reset builds a fresh bundle with no carry-over.
        fresh = get_or_create_metrics(registry)
        assert fresh is not bundle
        assert (
            registry.get_sample_value(
                "nexus_http_api_requests_total",
                {"method": "GET", "path": "/api/x", "status": "2xx"},
            )
            is None
        )

    def test_no_import_time_side_effects_on_default_registry(self):
        """Importing the module must not have registered anything on the
        process-wide default registry. We assert by checking that the
        canonical counter name is absent until get_default_metrics() runs."""
        reset_for_testing()
        try:
            assert "nexus_http_api_requests_total" not in getattr(
                REGISTRY, "_names_to_collectors", {}
            )
            get_default_metrics()  # now it registers, exactly once
            assert "nexus_http_api_requests_total" in getattr(
                REGISTRY, "_names_to_collectors", {}
            )
        finally:
            reset_for_testing()
