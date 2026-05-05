"""Tests for the Prometheus-style /metrics route (gh#34 follow-up)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from engine.observability.metrics import (
    NullBackend,
    RecordingBackend,
    get_metrics,
    set_metrics,
)
from engine.observability.prometheus import PrometheusBackend

_PROM_PREFIX = "text/plain; version=0.0.4"


@pytest.fixture
def _restore_backend():
    """Snapshot the active backend so each test can mutate the singleton
    without polluting the others."""
    original = get_metrics()
    yield
    set_metrics(original)


class TestRecordingBackendExposed:
    async def test_returns_text_plain_with_prom_version(
        self, client: AsyncClient, _restore_backend
    ):
        backend = PrometheusBackend()
        backend.counter("test.metric")
        set_metrics(backend)

        resp = await client.get("/metrics")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(_PROM_PREFIX)
        assert "test_metric" in resp.text
        assert "# TYPE test_metric counter" in resp.text

    async def test_renders_live_state_per_request(
        self, client: AsyncClient, _restore_backend
    ):
        backend = PrometheusBackend()
        set_metrics(backend)

        first = await client.get("/metrics")
        assert "test_increment" not in first.text

        # Mutate state between two scrapes — the second one must reflect
        # the new observation.
        backend.counter("test.increment")
        second = await client.get("/metrics")
        assert "test_increment 1" in second.text

    async def test_plain_recording_backend_also_renders(
        self, client: AsyncClient, _restore_backend
    ):
        # Operators are not required to use PrometheusBackend specifically;
        # any RecordingBackend subclass works because the route checks the
        # Protocol-level type.
        backend = RecordingBackend()
        backend.gauge("recording.gauge", 7.0)
        set_metrics(backend)

        resp = await client.get("/metrics")

        assert resp.status_code == 200
        assert "recording_gauge 7" in resp.text


class TestNullBackendPlaceholder:
    async def test_null_backend_returns_placeholder_with_200(
        self, client: AsyncClient, _restore_backend
    ):
        set_metrics(NullBackend())

        resp = await client.get("/metrics")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(_PROM_PREFIX)
        assert "metrics backend does not support exposition" in resp.text


class TestUnauthenticated:
    async def test_route_does_not_require_auth(
        self, client: AsyncClient, _restore_backend
    ):
        # The route does not depend on any auth dependency. Operators
        # restrict access via reverse-proxy / network ACLs (standard
        # Prometheus pattern). This guard exists so a careless dependency
        # addition cannot quietly break scrapes.
        set_metrics(PrometheusBackend())

        resp = await client.get("/metrics")

        assert resp.status_code == 200
