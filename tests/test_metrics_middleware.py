"""Tests for ``engine.middleware.metrics`` — the lightweight Prometheus
middleware backed by ``prometheus_client``.

These tests build a *standalone* Starlette app (not the main Nexus app,
which already exposes its own custom-rendered ``/metrics`` route) so the
real ``prometheus_client`` registry can be exercised end-to-end via the
ASGI transport.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from engine.middleware.metrics import (
    PrometheusMetricsMiddleware,
    register_metrics_endpoint,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _hello(_: Request) -> Response:
    return JSONResponse({"hello": "world"})


async def _created(_: Request) -> Response:
    return JSONResponse({"created": True}, status_code=201)


def _build_app() -> Starlette:
    """A minimal app: two sample routes + the middleware + /metrics."""
    app = Starlette(
        routes=[
            Route("/hello", _hello, methods=["GET"]),
            Route("/create", _created, methods=["POST"]),
        ],
    )
    app.add_middleware(PrometheusMetricsMiddleware)
    register_metrics_endpoint(app)
    return app


def _metric_value(body: str, name: str, must_contain: tuple[str, ...]) -> float:
    """Return the numeric value of the first exposition line whose name
    starts with ``name`` and contains every substring in ``must_contain``.
    Returns ``0.0`` when no such line exists (e.g. metric never observed)."""
    for line in body.splitlines():
        if line.startswith(name) and all(sub in line for sub in must_contain):
            return float(line.split()[-1])
    return 0.0


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPrometheusMetricsMiddleware:
    async def test_counter_and_histogram_appear_in_metrics(self) -> None:
        """Drive a GET through the test app, then scrape /metrics and
        assert both the counter and the histogram are present and have
        recorded the request."""
        app = _build_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Baseline snapshot so the test is deterministic regardless
            # of any state already accumulated on the global registry.
            baseline = await client.get("/metrics")
            assert baseline.status_code == 200
            before = _metric_value(
                baseline.text,
                "http_requests_total",
                ('method="GET"', 'path="/hello"', 'status="200"'),
            )

            # The request under test.
            resp = await client.get("/hello")
            assert resp.status_code == 200
            assert resp.json() == {"hello": "world"}

            metrics = await client.get("/metrics")
            assert metrics.status_code == 200

            body = metrics.text

            # Content type is the Prometheus exposition content type.
            assert metrics.headers["content-type"].startswith("text/plain;")

            # Counter is declared and observed for our label set.
            assert "# TYPE http_requests_total counter" in body
            assert "# HELP http_requests_total" in body
            after = _metric_value(
                body,
                "http_requests_total",
                ('method="GET"', 'path="/hello"', 'status="200"'),
            )
            assert after > before  # the GET /hello incremented the counter

            # Histogram is declared with its count + sum child series.
            assert "# TYPE http_request_duration_seconds histogram" in body
            assert "http_request_duration_seconds_count" in body
            assert "http_request_duration_seconds_sum" in body
            hist_count = _metric_value(
                body,
                "http_request_duration_seconds_count",
                ('method="GET"', 'path="/hello"', 'status="200"'),
            )
            assert hist_count > 0

    async def test_metrics_endpoint_is_itself_scrapable(self) -> None:
        """The /metrics scrape request is also served (and instrumented)."""
        app = _build_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics")
            assert resp.status_code == 200
            # A GET /metrics scrape lands in the counter too.
            assert 'path="/metrics"' in resp.text
            assert 'method="GET"' in resp.text

    async def test_non_2xx_status_recorded_as_label(self) -> None:
        """A 404 (no matching route) is recorded with status="404"."""
        app = _build_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            missing = await client.get("/does-not-exist")
            assert missing.status_code == 404

            metrics = await client.get("/metrics")
            assert 'path="/does-not-exist"' in metrics.text
            assert 'status="404"' in metrics.text
