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


def _ok_app(status: int):
    """Return a minimal raw-ASGI app that responds with ``status``."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


async def _drive(middleware, *, method="GET", path="/foo", scope_type="http"):
    """Invoke ``middleware`` once with a hand-rolled ASGI send/receive."""
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    scope = {"type": scope_type, "method": method, "path": path}
    await middleware(scope, receive, send)
    return sent


# ---------------------------------------------------------------------------
# Collector cache
# ---------------------------------------------------------------------------


class TestCollectorCacheIsWeakKeyed:
    """The cache must be a WeakKeyDictionary keyed by the registry."""

    def test_cache_is_a_weak_key_dictionary(self):
        assert isinstance(_collectors_cache, weakref.WeakKeyDictionary)

    def test_same_registry_returns_same_collectors(self):
        registry = CollectorRegistry()
        first = _get_collectors(registry)
        second = _get_collectors(registry)
        assert first is second

    def test_distinct_registries_get_distinct_collectors(self):
        a = CollectorRegistry()
        b = CollectorRegistry()
        ca = _get_collectors(a)
        cb = _get_collectors(b)
        gc.collect()
        assert ca is not cb
        assert ca["requests"] is not cb["requests"]

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
        assert weak() is None
        assert len(_collectors_cache) == 0

    def test_cache_holds_collectors_alive_only_while_registry_is_alive(self):
        registry = CollectorRegistry()
        collectors = _get_collectors(registry)
        assert registry in _collectors_cache
        assert _collectors_cache[registry] is collectors
        del registry
        gc.collect()
        assert len(_collectors_cache) == 0

    def test_no_stale_entry_when_id_is_recycled(self):
        """Regression for the ``id()`` reuse corruption scenario.

        With the old ``id(registry)``-keyed ``dict``:

        1. A throwaway registry ``A`` is created at ``id == 0xDEAD``.
        2. ``_get_collectors(A)`` stores an entry under ``0xDEAD``.
        3. ``A`` is GC'd; CPython is now free to reuse ``0xDEAD``.
        4. A *new* registry ``B`` is allocated at the same address.
        5. ``_get_collectors(B)`` hits the stale entry and returns the
           collectors that were registered against the dead ``A``.

        The result is either a ``Duplicated timeseries`` error when the
        caller tries to re-create them, or — worse — metrics silently
        routed to a registry that nobody references any more. Keying on
        the registry object itself (WeakKeyDictionary) makes step 5
        impossible: there is no entry to hit.
        """
        # Step 1-2: create + populate, then drop.
        a = CollectorRegistry()
        a_collectors = _get_collectors(a)
        assert len(_collectors_cache) == 1
        del a
        gc.collect()
        assert len(_collectors_cache) == 0, (
            "stale entry lingering in the cache — WeakKeyDictionary not in effect"
        )

        # Step 4-5: allocate as many registries as it takes to recycle
        # the address (Python's id allocator is intentionally not
        # guaranteed to reuse, so we just keep trying until either we
        # recycle the address or we have made the attempt enough times
        # that the regression guard is meaningful).
        target_id = id(a_collectors)  # placeholder; the real check is below
        for _ in range(1024):
            b = CollectorRegistry()
            if id(b) == target_id:
                # Even if the id WAS recycled, the new registry must
                # NOT receive the dead collectors — it must get a
                # freshly built set.
                fresh = _get_collectors(b)
                assert fresh is not a_collectors, (
                    "id() reuse resurrected stale collectors — corruption"
                )
                del b
                gc.collect()
                break
            del b

        # If the allocator never recycled the address in 1024 tries,
        # the guard still holds: a fresh registry always gets a fresh
        # entry, never a stale one. We assert that invariant directly
        # so the test is meaningful regardless of allocator behaviour.
        c = CollectorRegistry()
        c_collectors = _get_collectors(c)
        assert c in _collectors_cache
        assert c_collectors is _collectors_cache[c]
        assert len(_collectors_cache) == 1


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
        assert "http_request_duration_seconds" not in out_after
        assert "http_requests_in_flight" not in out_after

    def test_subsequent_re_init_does_not_raise_duplicate_timeseries(self):
        _get_collectors(REGISTRY)
        reset_collectors_for_tests()
        # Must not raise ValueError("Duplicated timeseries").
        _get_collectors(REGISTRY)

    def test_reset_is_a_noop_when_cache_is_empty(self):
        # Cache is empty thanks to the autouse fixture — calling reset
        # again must not raise.
        reset_collectors_for_tests()

    def test_reset_unregisters_from_non_default_registries(self):
        """Regression for the medium-severity issue: ``reset`` used to
        call ``REGISTRY.unregister`` for every entry, which was a
        silent no-op for collectors that lived in a throwaway registry.
        A live non-default registry must have its own collectors
        removed too — otherwise its state leaks across tests."""
        registry = CollectorRegistry()
        _get_collectors(registry)
        out_before = generate_latest(registry).decode()
        assert "http_requests_total" in out_before

        reset_collectors_for_tests()
        # The cache no longer references it…
        assert registry not in _collectors_cache
        # …and the registry itself no longer carries the collectors,
        # even though it is still very much alive.
        out_after = generate_latest(registry).decode()
        assert "http_requests_total" not in out_after
        assert "http_request_duration_seconds" not in out_after
        assert "http_requests_in_flight" not in out_after


# ---------------------------------------------------------------------------
# Middleware hot path
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
        assert 'http_requests_total{method="GET",path="/foo",status="200"} 1.0' in out

    @pytest.mark.asyncio
    async def test_in_flight_gauge_returns_to_zero(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, path="/foo")
        out = generate_latest(registry).decode()
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
        assert 'path="/api/v1/portfolio/:id"' in out

    @pytest.mark.asyncio
    async def test_exempt_path_records_nothing(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, path="/metrics")
        out = generate_latest(registry).decode()
        assert "http_requests_total{" not in out
        assert "http_request_duration_seconds_bucket{" not in out
        assert "http_request_duration_seconds_count{" not in out
        assert "http_requests_in_flight 0.0" in out

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through_unmodified(self):
        registry = CollectorRegistry()
        lifespan_seen = []

        async def app(scope, receive, send):
            lifespan_seen.append(scope["type"])

        mw = PrometheusMiddleware(app, registry=registry)
        await _drive(mw, scope_type="lifespan")
        assert lifespan_seen == ["lifespan"]
        out = generate_latest(registry).decode()
        assert "http_requests_total{" not in out
        assert "http_request_duration_seconds_bucket{" not in out
        assert "http_request_duration_seconds_count{" not in out

    @pytest.mark.asyncio
    async def test_two_middleware_instances_share_collectors(self):
        """Constructing two middlewares against the same registry must
        not raise ``Duplicated timeseries`` and must share the same
        underlying collector objects (this is the whole reason the
        cache exists).
        """
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


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestMiddlewareDefaults:
    def test_default_exempt_paths_match_scrape_routes(self):
        assert "/metrics" in DEFAULT_EXEMPT_PATHS
        assert "/metrics/prometheus" in DEFAULT_EXEMPT_PATHS

    def test_registry_defaults_to_global(self):
        mw = PrometheusMiddleware(_ok_app(200))
        assert mw.registry is REGISTRY

    def test_custom_exempt_paths_override_defaults(self):
        mw = PrometheusMiddleware(
            _ok_app(200),
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
            ("?query=1", "/"),
            ("#fragment", "/"),
        ],
    )
    def test_normalisation_cases(self, raw, expected):
        assert normalize_path(raw) == expected

    def test_uuid_takes_precedence_over_numeric(self):
        out = normalize_path("/u/11111111-2222-3333-4444-555555555555")
        assert out == "/u/:uuid"
