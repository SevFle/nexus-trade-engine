"""Tests for HttpMetricsMiddleware (gh#34 follow-up).

The middleware wraps the FastAPI app and emits three metrics through
the active ``MetricsBackend``:

- ``http.request.count`` counter, exactly once per terminated request.
- ``http.request.duration_ms`` histogram, one observation per request.
- ``http.request.in_flight`` gauge, set on each state change.

Tags: ``method`` and ``status_class`` (``2xx``/``3xx``/``4xx``/``5xx``/
``1xx``/``unknown``). The full path is intentionally not labelled.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from engine.observability.http_metrics import (
    HttpMetricsMiddleware,
    _status_class,
)
from engine.observability.metrics import RecordingBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ok(_: Request) -> Response:
    return JSONResponse({"ok": True})


async def _server_error(_: Request) -> Response:
    return JSONResponse({"err": "boom"}, status_code=500)


async def _redirect(_: Request) -> Response:
    return Response(status_code=302, headers={"location": "/elsewhere"})


def _app(metrics: RecordingBackend) -> Starlette:
    app = Starlette(
        routes=[
            Route("/ok", _ok, methods=["GET"]),
            Route("/boom", _server_error, methods=["GET"]),
            Route("/redirect", _redirect, methods=["GET"]),
            Route("/post", _ok, methods=["POST"]),
        ]
    )
    return HttpMetricsMiddleware(app, metrics=metrics)


def _counter_total(backend: RecordingBackend, name: str) -> float:
    return sum(v for (n, _t), v in backend.counters.items() if n == name)


def _counter_with(
    backend: RecordingBackend, name: str, tags: dict[str, str]
) -> float:
    expected = tuple(sorted(tags.items()))
    return sum(
        v
        for (n, t), v in backend.counters.items()
        if n == name and all(item in t for item in expected)
    )


def _histogram_count(
    backend: RecordingBackend, name: str, tags: dict[str, str]
) -> int:
    expected = tuple(sorted(tags.items()))
    return sum(
        len(v)
        for (n, t), v in backend.histograms.items()
        if n == name and all(item in t for item in expected)
    )


def _last_gauge(backend: RecordingBackend, name: str) -> float | None:
    matches = [v for (n, _t), v in backend.gauges.items() if n == name]
    return matches[-1] if matches else None


@pytest.fixture
def metrics() -> RecordingBackend:
    return RecordingBackend()


# ---------------------------------------------------------------------------
# Status-class helper
# ---------------------------------------------------------------------------


class TestStatusClass:
    def test_buckets(self):
        assert _status_class(100) == "1xx"
        assert _status_class(200) == "2xx"
        assert _status_class(299) == "2xx"
        assert _status_class(302) == "3xx"
        assert _status_class(404) == "4xx"
        assert _status_class(500) == "5xx"
        assert _status_class(599) == "5xx"
        assert _status_class(None) == "unknown"
        assert _status_class(700) == "unknown"


# ---------------------------------------------------------------------------
# Per-request metrics
# ---------------------------------------------------------------------------


class TestRequestMetrics:
    async def test_2xx_get_records_count_and_histogram(self, metrics):
        transport = ASGITransport(app=_app(metrics))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/ok")

        assert r.status_code == 200
        assert (
            _counter_with(
                metrics,
                "http.request.count",
                {"method": "GET", "status_class": "2xx"},
            )
            == 1
        )
        assert (
            _histogram_count(
                metrics,
                "http.request.duration_ms",
                {"method": "GET", "status_class": "2xx"},
            )
            == 1
        )

    async def test_500_records_5xx_status_class(self, metrics):
        transport = ASGITransport(app=_app(metrics))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/boom")

        assert r.status_code == 500
        assert (
            _counter_with(
                metrics,
                "http.request.count",
                {"method": "GET", "status_class": "5xx"},
            )
            == 1
        )

    async def test_302_records_3xx_status_class(self, metrics):
        transport = ASGITransport(app=_app(metrics))
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as c:
            r = await c.get("/redirect")

        assert r.status_code == 302
        assert (
            _counter_with(
                metrics,
                "http.request.count",
                {"method": "GET", "status_class": "3xx"},
            )
            == 1
        )

    async def test_method_tag_propagates(self, metrics):
        transport = ASGITransport(app=_app(metrics))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/post", json={})

        assert (
            _counter_with(
                metrics,
                "http.request.count",
                {"method": "POST"},
            )
            == 1
        )


class TestInFlightGauge:
    async def test_returns_to_zero_after_request(self, metrics):
        transport = ASGITransport(app=_app(metrics))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get("/ok")

        # Last write is the post-request decrement back to 0.
        assert _last_gauge(metrics, "http.request.in_flight") == 0.0

    async def test_three_serial_requests_all_settle_at_zero(self, metrics):
        transport = ASGITransport(app=_app(metrics))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            for _ in range(3):
                await c.get("/ok")

        assert _counter_total(metrics, "http.request.count") == 3
        assert _last_gauge(metrics, "http.request.in_flight") == 0.0


class TestNonHttpScopePassthrough:
    async def test_lifespan_scope_is_passed_through_unchanged(self, metrics):
        # Build a tiny app that responds to a lifespan scope. The
        # middleware must not record metrics for it.
        async def lifespan_only_app(scope, receive, send):  # noqa: ARG001
            assert scope["type"] == "lifespan"
            msg = await receive()
            assert msg["type"] == "lifespan.startup"
            await send({"type": "lifespan.startup.complete"})
            msg = await receive()
            assert msg["type"] == "lifespan.shutdown"
            await send({"type": "lifespan.shutdown.complete"})

        wrapped = HttpMetricsMiddleware(lifespan_only_app, metrics=metrics)

        # Drive the lifespan dance manually.
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

        assert _counter_total(metrics, "http.request.count") == 0
        assert sent == [
            {"type": "lifespan.startup.complete"},
            {"type": "lifespan.shutdown.complete"},
        ]
