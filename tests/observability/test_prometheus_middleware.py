"""Tests for :mod:`engine.middleware.prometheus`.

Covers the lazy collector cache (now a :class:`weakref.WeakKeyDictionary`
keyed by the registry object rather than ``id(registry)``), the raw-ASGI
:class:`PrometheusMiddleware` hot path, path normalisation, and the
``reset_collectors_for_tests`` teardown helper.
"""

from __future__ import annotations

import gc
import weakref

import pytest
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest

from engine.middleware.prometheus import (
    DEFAULT_EXEMPT_PATHS,
    PrometheusMiddleware,
    _collectors_cache,
    _get_collectors,
    normalize_path,
    reset_collectors_for_tests,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_default_registry():
    """Ensure each test starts (and ends) with a clean default REGISTRY.

    The default ``prometheus_client.REGISTRY`` is process-global, so any
    collectors we register against it would leak across tests without
    explicit teardown. Throwaway registries do not have this problem
    because the cache is now a :class:`WeakKeyDictionary`.
    """
    reset_collectors_for_tests()
    yield
    reset_collectors_for_tests()


# ---------------------------------------------------------------------------
# Tiny ASGI helpers
# ---------------------------------------------------------------------------


def _ok_app(status: int = 200):
    """Return a minimal raw-ASGI app that responds with ``status``."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


async def _drive(middleware, *, method="GET", path="/foo", scope_type="http"):
    """Invoke ``middleware`` once with a hand-rolled ASGI send/receive."""
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    scope = {"type": scope_type, "method": method, "path": path}
    await middleware(scope, receive, send)
    return sent


# ---------------------------------------------------------------------------
# Collector cache: WeakKeyDictionary semantics
# ---------------------------------------------------------------------------


class TestCollectorCacheIsWeakKeyed:
    """The cache must be a WeakKeyDictionary keyed by the registry."""

    def test_cache_is_a_weak_key_dictionary(self):
        from weakref import WeakKeyDictionary

        assert isinstance(_collectors_cache, WeakKeyDictionary)

    def test_same_registry_returns_same_collectors(self):
        registry = CollectorRegistry()
        try:
            first = _get_collectors(registry)
            second = _get_collectors(registry)
            assert first is second
            # Same dict object cached under the registry key.
            assert _collectors_cache[registry] is first
        finally:
            del registry
            gc.collect()

    def test_distinct_registries_get_distinct_collectors(self):
        a = CollectorRegistry()
        b = CollectorRegistry()
        try:
            ca = _get_collectors(a)
            cb = _get_collectors(b)
            assert ca is not cb
            assert ca["requests"] is not cb["requests"]
        finally:
            del a, b
            gc.collect()

    def test_throwaway_registry_is_gc_d_from_cache(self):
        """Once the registry is unreferenced, its cache entry vanishes.

        This is the headline benefit of WeakKeyDictionary over the old
        ``id(registry)`` keyed dict: there is no stale entry to be
        resurrected by ``id()`` reuse, and tests do not need to remember
        to call :func:`reset_collectors_for_tests` for isolation.
        """
        registry = CollectorRegistry()
        weak = weakref.ref(registry)
        _get_collectors(registry)
        assert registry in _collectors_cache
        del registry
        gc.collect()
        # The registry itself is gone…
        assert weak() is None
        # …and so is its cache entry, automatically.
        assert len(_collectors_cache) == 0

    def test_cache_holds_collectors_alive_only_while_registry_is_alive(self):
        registry = CollectorRegistry()
        collectors = _get_collectors(registry)
        del collectors
        # The cache still holds the collectors dict while the registry lives.
        assert registry in _collectors_cache
        assert _collectors_cache[registry] is not None
        del registry
        gc.collect()
        # Now the registry and its collectors are both gone.
        assert len(_collectors_cache) == 0


# ---------------------------------------------------------------------------
# reset_collectors_for_tests
# ---------------------------------------------------------------------------


class TestResetCollectorsForTests:
    def test_drops_default_registry_entry(self):
        _get_collectors(REGISTRY)
        assert REGISTRY in _collectors_cache
        reset_collectors_for_tests()
        assert REGISTRY not in _collectors_cache

    def test_unregisters_collectors_from_default_registry(self):
        _get_collectors(REGISTRY)
        out_before = generate_latest(REGISTRY).decode()
        assert "http_requests_total" in out_before
        reset_collectors_for_tests()
        out_after = generate_latest(REGISTRY).decode()
        assert "http_requests_total" not in out_after
        assert "http_request_duration_seconds" not in out_after
        assert "http_requests_in_flight" not in out_after

    def test_subsequent_re_init_does_not_raise_duplicate_timeseries(self):
        # Register once, reset, then register again — pre-refactor this
        # would have raised ``Duplicated timeseries`` because the
        # collectors stayed attached to REGISTRY.
        _get_collectors(REGISTRY)
        reset_collectors_for_tests()
        # No exception:
        _get_collectors(REGISTRY)

    def test_reset_is_a_noop_when_cache_is_empty(self):
        # No collectors have been registered against REGISTRY in this
        # test (autouse fixture reset both before and after).
        reset_collectors_for_tests()  # must not raise.


# ---------------------------------------------------------------------------
# Middleware end-to-end (ASGI shape)
# ---------------------------------------------------------------------------


class TestMiddlewareRecordsMetrics:
    @pytest.mark.asyncio
    async def test_counter_and_histogram_recorded(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, method="GET", path="/foo")
        out = generate_latest(registry).decode()
        assert "# TYPE http_requests_total counter" in out
        assert "# TYPE http_request_duration_seconds histogram" in out
        assert "# TYPE http_requests_in_flight gauge" in out
        # The single request landed with status 200 on path /foo.
        assert 'http_requests_total{method="GET",path="/foo",status="200"} 1.0' in out

    @pytest.mark.asyncio
    async def test_in_flight_gauge_returns_to_zero(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, path="/foo")
        out = generate_latest(registry).decode()
        # After a clean request, in_flight must be back to 0.
        assert "http_requests_in_flight 0.0" in out

    @pytest.mark.asyncio
    async def test_status_is_captured_from_response_start(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(503), registry=registry)
        await _drive(mw, path="/boom")
        out = generate_latest(registry).decode()
        assert 'status="503"' in out

    @pytest.mark.asyncio
    async def test_path_is_normalised_in_labels(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, path="/api/v1/portfolio/12345")
        out = generate_latest(registry).decode()
        # Numeric segment collapsed to :id.
        assert 'path="/api/v1/portfolio/:id"' in out

    @pytest.mark.asyncio
    async def test_exempt_path_records_nothing(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, path="/metrics")
        out = generate_latest(registry).decode()
        # The Prometheus exposition format always emits HELP/TYPE metadata
        # for registered collectors — what we care about is that *no sample*
        # was recorded. Look for an actual labelled sample line, not the
        # metadata preamble.
        assert 'http_requests_total{' not in out
        assert 'http_request_duration_seconds_bucket{' not in out
        assert 'http_request_duration_seconds_count{' not in out
        # And the in-flight gauge reports zero (never incremented).
        assert "http_requests_in_flight 0.0" in out

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through_unmodified(self):
        registry = CollectorRegistry()
        # Lifespan / websocket scopes must not be measured.
        lifespan_seen: list[str] = []

        async def app(scope, receive, send):
            lifespan_seen.append(scope["type"])

        mw = PrometheusMiddleware(app, registry=registry)
        await _drive(mw, scope_type="lifespan")
        assert lifespan_seen == ["lifespan"]
        # No labelled sample was recorded — the metadata preamble still
        # appears (collectors are registered on middleware construction)
        # but the counter/histogram have zero observations.
        out = generate_latest(registry).decode()
        assert 'http_requests_total{' not in out
        assert 'http_request_duration_seconds_bucket{' not in out
        assert 'http_request_duration_seconds_count{' not in out

    @pytest.mark.asyncio
    async def test_two_middleware_instances_share_collectors(self):
        """Constructing two middlewares against the same registry must
        not raise ``Duplicated timeseries`` and must share the same
        underlying collector objects (this is the whole reason the
        cache exists)."""
        registry = CollectorRegistry()
        mw1 = PrometheusMiddleware(_ok_app(200), registry=registry)
        mw2 = PrometheusMiddleware(_ok_app(200), registry=registry)
        assert mw1._counter is mw2._counter
        assert mw1._histogram is mw2._histogram
        assert mw1._in_flight is mw2._in_flight
        await _drive(mw1, path="/a")
        await _drive(mw2, path="/b")
        out = generate_latest(registry).decode()
        assert 'path="/a"' in out
        assert 'path="/b"' in out

    @pytest.mark.asyncio
    async def test_malformed_response_status_falls_back_to_zero(self):
        """A non-integer / missing ``status`` on the response.start
        message must not crash the middleware; it falls back to ``0``
        so the request is still counted (under status="0")."""
        registry = CollectorRegistry()

        async def app(scope, receive, send):
            # Malformed: status is a non-numeric string.
            await send({"type": "http.response.start", "status": "oops"})
            await send({"type": "http.response.body", "body": b""})

        mw = PrometheusMiddleware(app, registry=registry)
        await _drive(mw, path="/weird")
        out = generate_latest(registry).decode()
        assert 'status="0"' in out

    @pytest.mark.asyncio
    async def test_exception_in_wrapped_app_still_decrements_in_flight(self):
        """If the wrapped app raises, the in-flight gauge must still
        settle back to zero — metric recording must never leak a
        phantom in-flight request."""
        registry = CollectorRegistry()

        async def app(scope, receive, send):
            raise RuntimeError("boom")

        mw = PrometheusMiddleware(app, registry=registry)
        with pytest.raises(RuntimeError):
            await _drive(mw, path="/explode")
        out = generate_latest(registry).decode()
        assert "http_requests_in_flight 0.0" in out


class TestMiddlewareDefaults:
    def test_default_exempt_paths_match_scrape_routes(self):
        # The Prometheus scrape routes must be exempt by default so a
        # tight scrape interval does not dominate the latency histogram.
        assert "/metrics" in DEFAULT_EXEMPT_PATHS
        assert "/metrics/prometheus" in DEFAULT_EXEMPT_PATHS

    def test_registry_defaults_to_global(self):
        mw = PrometheusMiddleware(_ok_app())
        assert mw.registry is REGISTRY

    def test_custom_exempt_paths_override_defaults(self):
        mw = PrometheusMiddleware(
            _ok_app(),
            registry=CollectorRegistry(),
            exempt_paths=("/custom",),
        )
        assert mw.exempt_paths == frozenset(("/custom",))


# ---------------------------------------------------------------------------
# normalize_path
# ---------------------------------------------------------------------------


class TestNormalizePath:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", "/"),
            ("/api/v1/portfolio/12345", "/api/v1/portfolio/:id"),
            (
                "/api/v1/portfolio/550e8400-e29b-41d4-a716-446655440000",
                "/api/v1/portfolio/:uuid",
            ),
            ("/orders/42/items/7", "/orders/:id/items/:id"),
            ("/health", "/health"),
            ("/path?query=1", "/path"),
            ("/path#fragment", "/path"),
            # Query-only or fragment-only input collapses to "/" after
            # stripping (covers the second emptiness guard).
            ("?query=1", "/"),
            ("#fragment", "/"),
        ],
    )
    def test_normalisation_cases(self, raw, expected):
        assert normalize_path(raw) == expected

    def test_uuid_takes_precedence_over_numeric(self):
        # A UUID whose segments are digit-only should still collapse to
        # ``:uuid`` rather than four ``:id`` segments.
        # 11111111-2222-3333-4444-555555555555 is a legal hex UUID.
        out = normalize_path(
            "/u/11111111-2222-3333-4444-555555555555"
        )
        assert out == "/u/:uuid"
