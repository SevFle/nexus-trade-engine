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
    _collectors_lock,
    _get_collectors,
    normalize_path,
    reset_collectors_for_tests,
)


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


async def _drive(middleware, *, method: str = "GET", path: str = "/foo", scope_type: str = "http"):
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
# Collector cache: weak-keyed by the registry object
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
        # The cache is two-level: outer WeakKeyDictionary keyed by the
        # registry, inner dict keyed by ``(metric_prefix, buckets)``.
        inner = _collectors_cache[registry]
        assert isinstance(inner, dict)
        assert collectors in inner.values()
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
        a = CollectorRegistry()
        a_collectors = _get_collectors(a)
        assert len(_collectors_cache) == 1
        # Capture the id() of the *registry* (not the collectors dict)
        # before it goes away — once ``a`` is del'd, ``id(a)`` would
        # read a live but unrelated object. We then drive up to 1024
        # fresh registries looking for one that CPython recycles into
        # the same address.
        target_id = id(a)
        del a
        gc.collect()
        assert len(_collectors_cache) == 0, (
            "stale entry lingering in the cache — WeakKeyDictionary not in effect"
        )

        for _ in range(1024):
            b = CollectorRegistry()
            if id(b) == target_id:
                fresh = _get_collectors(b)
                assert fresh is not a_collectors, (
                    "id() reuse resurrected stale collectors — corruption"
                )
                del b
                gc.collect()
                break
            del b

        # Whether or not we hit the recycled id, a fresh registry must
        # always land a brand-new entry in the cache.
        c = CollectorRegistry()
        c_collectors = _get_collectors(c)
        assert c in _collectors_cache
        # Two-level cache: outer keyed by registry, inner keyed by
        # ``(metric_prefix, buckets)``.
        inner = _collectors_cache[c]
        assert next(iter(inner.values())) is c_collectors
        assert len(_collectors_cache) == 1


# ---------------------------------------------------------------------------
# Cache key includes metric_prefix and buckets
# ---------------------------------------------------------------------------


class TestCacheKeyIncludesPrefixAndBuckets:
    """The cache key must be the full
    ``(registry, metric_prefix, tuple(buckets))`` triple.

    Before this fix the cache was keyed only on the registry, so two
    middlewares constructed against the same registry with *different*
    prefixes or bucket layouts would silently share the first one's
    collector set — and the second middleware would record metrics
    under the wrong metric names. The 2-level cache structure makes
    the inner key ``(metric_prefix, tuple(buckets))`` so each variant
    gets its own collector objects.

    Note: ``prometheus_client`` itself refuses to register two
    Counters / Gauges with the same name in one registry, so in
    practice each distinct ``(prefix, buckets)`` pair needs a distinct
    prefix to coexist. The cache still records buckets in the key so
    that two calls with the *same* prefix but *different* buckets do
    not silently return the wrong cached Histogram.
    """

    def test_different_prefix_yields_different_collectors(self):
        registry = CollectorRegistry()
        http = _get_collectors(registry, metric_prefix="http")
        ws = _get_collectors(registry, metric_prefix="websocket")
        assert http is not ws
        # ``prometheus_client.Counter`` strips the ``_total`` suffix
        # from its internal ``_name`` attribute (the suffix is
        # re-appended automatically at exposition time), so checking
        # ``._name`` directly would compare ``"http_requests"`` against
        # ``"http_requests_total"``. The prefix-is-applied guarantee
        # is observable through the *exposed* metric surface, which is
        # what scrapers ultimately consume — so use ``generate_latest``
        # the same way ``test_two_middlewares_with_different_prefix_coexist``
        # does. This also verifies the cache key really did incorporate
        # the prefix: if it had not, the second call would have returned
        # the first one's collectors and only one TYPE line would appear.
        out = generate_latest(registry).decode()
        assert "# TYPE http_requests_total counter" in out
        assert "# TYPE websocket_requests_total counter" in out

    def test_different_buckets_yields_different_histograms(self):
        """Same prefix but different buckets cannot coexist in one
        registry (Counter/Gauge names would collide), so we verify
        via two separate registries that the buckets value really is
        part of the cache lookup and the resulting Histogram reflects
        the requested buckets.
        """
        registry_a = CollectorRegistry()
        registry_b = CollectorRegistry()
        default_buckets = _get_collectors(registry_a, buckets=(0.005, 0.01, 0.025))
        custom_buckets = _get_collectors(registry_b, buckets=(0.1, 1.0, 10.0))
        assert default_buckets["latency"] is not custom_buckets["latency"]
        # The buckets difference must actually land on the Histogram.
        # ``prometheus_client.Histogram`` stores ``_upper_bounds`` as a
        # ``list`` (it appends ``+Inf`` to the configured buckets), so
        # normalise to a tuple before comparing values — the cache key
        # itself is already tuple-normalised (see
        # ``test_cache_key_buckets_is_a_tuple_not_a_list``), this is
        # purely about reading the library's internal attribute.
        assert tuple(default_buckets["latency"]._upper_bounds) == (0.005, 0.01, 0.025, float("inf"))
        assert tuple(custom_buckets["latency"]._upper_bounds) == (0.1, 1.0, 10.0, float("inf"))

    def test_same_prefix_and_buckets_returns_same_collectors(self):
        registry = CollectorRegistry()
        first = _get_collectors(
            registry, metric_prefix="http", buckets=(0.1, 1.0, 10.0)
        )
        second = _get_collectors(
            registry, metric_prefix="http", buckets=(0.1, 1.0, 10.0)
        )
        assert first is second

    def test_two_middlewares_with_different_prefix_coexist(self):
        """Constructing two middlewares against the same registry but
        with different prefixes must not blow up with
        ``Duplicated timeseries`` and each must record under its own
        metric names.
        """
        registry = CollectorRegistry()
        mw_http = PrometheusMiddleware(
            _ok_app(200), registry=registry, metric_prefix="http"
        )
        mw_ws = PrometheusMiddleware(
            _ok_app(200), registry=registry, metric_prefix="websocket"
        )
        assert mw_http._counter is not mw_ws._counter

        out = generate_latest(registry).decode()
        assert "# TYPE http_requests_total counter" in out
        assert "# TYPE websocket_requests_total counter" in out

    def test_inner_cache_holds_multiple_prefix_entries_per_registry(self):
        registry = CollectorRegistry()
        _get_collectors(registry, metric_prefix="http")
        _get_collectors(registry, metric_prefix="websocket")
        _get_collectors(registry, metric_prefix="grpc")
        inner = _collectors_cache[registry]
        # Three distinct (prefix, buckets) entries for one registry.
        assert len(inner) == 3

    def test_cache_key_buckets_is_a_tuple_not_a_list(self):
        """``buckets`` may be passed as any iterable; the cache key must
        normalise to a ``tuple`` so two calls with semantically equal
        but type-different iterables share an entry.
        """
        registry = CollectorRegistry()
        first = _get_collectors(
            registry, metric_prefix="http", buckets=[0.1, 1.0, 10.0]
        )
        inner = _collectors_cache[registry]
        # Key is normalised to a tuple, not the list we passed in.
        assert ("http", (0.1, 1.0, 10.0)) in inner
        second = _get_collectors(
            registry, metric_prefix="http", buckets=(0.1, 1.0, 10.0)
        )
        assert first is second


# ---------------------------------------------------------------------------
# Constructor failure: partial cleanup
# ---------------------------------------------------------------------------


class TestConstructorFailureCleansUp:
    """If any of the three collector constructors raises, every
    collector that *did* register must be unregistered before the
    exception propagates — otherwise the registry is left polluted
    with half-built collectors and every subsequent attempt to build
    the set fails the same way (``Duplicated timeseries``).
    """

    def test_partial_collectors_unregistered_on_failure(self, monkeypatch):
        registry = CollectorRegistry()

        # Make the *second* constructor (Histogram) blow up after the
        # Counter has already been registered. We patch the Histogram
        # class in the prometheus_client namespace that the module
        # under test imported.
        #
        # The failure is raised as ``ValueError`` to mirror what
        # ``prometheus_client`` itself does for ``Duplicated
        # timeseries`` — the narrowed ``except ValueError`` in
        # :func:`_get_collectors` must catch exactly this exception
        # family. A broader ``RuntimeError`` would now propagate
        # *without* cleanup (intentional: unrelated programming errors
        # should not be masked as registration failures).
        from engine.middleware import prometheus as prom_module

        original_histogram = prom_module.Histogram

        class _ExplodingHistogram(original_histogram):  # type: ignore[misc]
            def __init__(self, *args, **kwargs):
                raise ValueError("Duplicated timeseries in registry")

        monkeypatch.setattr(prom_module, "Histogram", _ExplodingHistogram)

        with pytest.raises(ValueError, match="Duplicated timeseries"):
            _get_collectors(registry, metric_prefix="http")

        # The Counter that was successfully created before the
        # Histogram blew up must have been unregistered — otherwise
        # it would leak into the registry and break the next attempt.
        out = generate_latest(registry).decode()
        assert "http_requests_total" not in out
        assert "http_request_duration_seconds" not in out
        assert "http_requests_in_flight" not in out

        # And nothing should be cached under the ("http", default)
        # inner key — the failed attempt must not poison the cache.
        inner = _collectors_cache.get(registry, {})
        assert ("http", tuple(_default_buckets_tuple())) not in inner

    def test_retry_after_failure_succeeds(self, monkeypatch):
        """After a failed construction, the registry must be clean
        enough that a retry (with the failure patched out) succeeds
        instead of raising ``Duplicated timeseries``.
        """
        registry = CollectorRegistry()
        from engine.middleware import prometheus as prom_module

        original_gauge = prom_module.Gauge
        call_count = {"n": 0}

        class _ExplodingGauge(original_gauge):  # type: ignore[misc]
            def __init__(self, *args, **kwargs):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise ValueError("first attempt fails")
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(prom_module, "Gauge", _ExplodingGauge)

        # First attempt fails partway through.
        with pytest.raises(ValueError):
            _get_collectors(registry, metric_prefix="http")

        # Second attempt must succeed — proving partial cleanup worked.
        collectors = _get_collectors(registry, metric_prefix="http")
        assert "requests" in collectors
        assert "latency" in collectors
        assert "in_flight" in collectors

    def test_non_value_error_propagates_without_cleanup(self, monkeypatch):
        """Only ``ValueError`` (the ``Duplicated timeseries`` family)
        should trigger the partial-collector cleanup. Any other
        exception — e.g. a ``RuntimeError`` from a buggy
        ``prometheus_client`` upgrade, or a ``TypeError`` from a
        programming error — must propagate *untouched* WITHOUT running
        the cleanup loop. This is the whole point of narrowing the
        ``except Exception`` to ``except ValueError``: we don't want
        unrelated errors to be masked by best-effort teardown logic
        that itself might fail.

        Note: because cleanup does not run, the Counter that was
        already registered against the registry stays put. The test
        asserts exactly this — it is the contract of the narrowed
        catch, not a bug.
        """
        registry = CollectorRegistry()
        from engine.middleware import prometheus as prom_module

        original_histogram = prom_module.Histogram

        class _ExplodingHistogram(original_histogram):  # type: ignore[misc]
            def __init__(self, *args, **kwargs):
                raise RuntimeError("unrelated prometheus_client bug")

        monkeypatch.setattr(prom_module, "Histogram", _ExplodingHistogram)

        # The RuntimeError propagates without being caught and
        # re-raised by the narrowed ``except ValueError``.
        with pytest.raises(RuntimeError, match="unrelated"):
            _get_collectors(registry, metric_prefix="http")

        # Cleanup did NOT run, so the Counter that successfully
        # registered before the Histogram blew up is still there.
        # This is the intentional contract of the narrowed catch.
        out = generate_latest(registry).decode()
        assert "http_requests_total" in out

    def test_cleanup_warning_emitted_on_unregister_failure(
        self, monkeypatch
    ):
        """If the best-effort ``unregister`` call itself raises during
        cleanup, the exception must be suppressed AND a ``structlog``
        warning must be emitted so the failure is observable in logs.

        Before this change only ``KeyError`` was suppressed, so an
        unexpected ``unregister`` failure mode would have crashed the
        cleanup loop and masked the original ``ValueError``. Now any
        ``Exception`` from ``unregister`` is logged at warning level
        and swallowed.
        """
        registry = CollectorRegistry()
        from engine.middleware import prometheus as prom_module

        original_histogram = prom_module.Histogram

        class _ExplodingHistogram(original_histogram):  # type: ignore[misc]
            def __init__(self, *args, **kwargs):
                raise ValueError("Duplicated timeseries in registry")

        monkeypatch.setattr(prom_module, "Histogram", _ExplodingHistogram)

        # Force the ``CollectorRegistry.unregister`` call itself to
        # blow up with a non-``KeyError`` exception, so we can verify
        # the broadened suppression + warning path is exercised.
        def _boom_unregister(_collector):
            raise OSError("simulated registry corruption")

        monkeypatch.setattr(registry, "unregister", _boom_unregister)

        # Capture ``structlog`` warnings by spying on the module-level
        # logger. We can't rely on ``caplog`` because structlog's
        # ConsoleRenderer writes straight to stdout under the test
        # config and does not propagate to the stdlib root logger that
        # ``caplog`` hooks.
        warnings: list[dict] = []
        original_warn = prom_module._log.warning

        def _spy_warning(event, **kwargs):
            warnings.append({"event": event, **kwargs})
            original_warn(event, **kwargs)

        monkeypatch.setattr(prom_module._log, "warning", _spy_warning)

        # ``ValueError`` from Histogram construction still propagates —
        # the cleanup-loop failures are suppressed.
        with pytest.raises(ValueError, match="Duplicated timeseries"):
            _get_collectors(registry, metric_prefix="http")

        # A warning must have been emitted for the failed cleanup call.
        # The structured event name is ``prometheus_collector_cleanup_failed``
        # and it carries the offending collector's name plus the
        # underlying error string.
        assert warnings, (
            "expected a structlog warning to be emitted when the "
            "best-effort unregister fails during cleanup"
        )
        assert warnings[0]["event"] == "prometheus_collector_cleanup_failed"
        assert warnings[0]["collector"] == "http_requests"
        assert "simulated registry corruption" in str(warnings[0]["error"])


def _default_buckets_tuple():
    """Return the module's default buckets as a tuple (used by tests
    that need to assert against the inner cache key)."
    """
    from engine.middleware.prometheus import _DEFAULT_BUCKETS

    return _DEFAULT_BUCKETS


# ---------------------------------------------------------------------------
# Thread safety: _collectors_lock
# ---------------------------------------------------------------------------


class TestCollectorsLock:
    """Both :func:`_get_collectors` and :func:`reset_collectors_for_tests`
    must serialise on :data:`_collectors_lock`. Without it, two threads
    instantiating middleware concurrently against the same registry
    can race past the ``cached is None`` check and both attempt to
    register ``http_requests_total`` — the loser raises
    ``Duplicated timeseries``.
    """

    def test_lock_exists_and_is_a_lock(self):
        import threading

        # The guard must be either a plain ``threading.Lock`` or a
        # re-entrant ``threading.RLock`` — both serialise different
        # threads correctly. ``RLock`` is the current choice so that
        # ``reset_collectors_for_tests`` can be re-entered from a
        # code path that already holds the lock on the same thread.
        assert isinstance(
            _collectors_lock,
            (type(threading.Lock()), type(threading.RLock())),
        )

    def test_lock_is_reentrant(self):
        """The lock must be re-entrant (an ``RLock``), not a plain
        ``Lock``. ``reset_collectors_for_tests`` can legitimately be
        invoked from code paths that already hold the lock on the same
        thread; a plain non-reentrant ``Lock`` would deadlock there.
        Acquiring the lock twice in succession on the same thread must
        therefore succeed without raising ``RecursiveError`` or
        blocking.
        """
        # Both acquisitions on the same thread must succeed (RLock).
        with _collectors_lock, _collectors_lock:
            pass

    def test_reset_is_safe_to_call_while_holding_lock(self):
        """``reset_collectors_for_tests`` runs under :data:`_collectors_lock`.
        With a re-entrant ``RLock`` it must be safe to call from a
        context that already holds the lock (no self-deadlock).
        """
        registry = CollectorRegistry()
        _get_collectors(registry, metric_prefix="http")
        assert registry in _collectors_cache

        with _collectors_lock:
            # This re-acquires the same lock on the same thread. A
            # plain ``Lock`` would deadlock here; ``RLock`` allows it.
            reset_collectors_for_tests()

        assert registry not in _collectors_cache

    def test_concurrent_get_collectors_does_not_raise_duplicate(self):
        """Spin up many threads all racing to lazily create the same
        collector set against the same registry. The lock must
        serialise them; without it we would see
        ``Duplicated timeseries`` from the loser.
        """
        import threading

        registry = CollectorRegistry()
        results: list[Exception | dict] = []
        results_lock = threading.Lock()

        def worker():
            try:
                collectors = _get_collectors(registry, metric_prefix="http")
            except Exception as exc:
                with results_lock:
                    results.append(exc)
            else:
                with results_lock:
                    results.append(collectors)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No thread should have raised.
        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"concurrent get_collectors raised: {errors!r}"
        # All threads must have received the same collector dict.
        collector_dicts = [r for r in results if not isinstance(r, Exception)]
        assert len(collector_dicts) == 16
        first = collector_dicts[0]
        assert all(c is first for c in collector_dicts[1:])

    def test_reset_clears_multiple_prefix_entries(self):
        """``reset_collectors_for_tests`` must unregister every
        ``(prefix, buckets)`` variant for a registry, not just the
        first one. Regression for the inner-cache-key change.
        """
        registry = CollectorRegistry()
        _get_collectors(registry, metric_prefix="http")
        _get_collectors(registry, metric_prefix="websocket")
        _get_collectors(registry, metric_prefix="grpc")

        out_before = generate_latest(registry).decode()
        assert "http_requests_total" in out_before
        assert "websocket_requests_total" in out_before
        assert "grpc_requests_total" in out_before

        reset_collectors_for_tests()

        out_after = generate_latest(registry).decode()
        assert "http_requests_total" not in out_after
        assert "websocket_requests_total" not in out_after
        assert "grpc_requests_total" not in out_after
        assert "http_request_duration_seconds" not in out_after
        assert registry not in _collectors_cache


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
        # Re-creating against the same registry must not blow up.
        _get_collectors(REGISTRY)

    def test_reset_is_a_noop_when_cache_is_empty(self):
        # Defensive: calling reset on an empty cache must not raise.
        reset_collectors_for_tests()

    def test_reset_unregisters_from_non_default_registries(self):
        """Regression for the medium-severity issue: ``reset`` used to
        call ``REGISTRY.unregister`` for every entry, which was a
        silent no-op for collectors that lived in a throwaway registry.
        A live non-default registry must have its own collectors
        removed too — otherwise its state leaks across tests.
        """
        registry = CollectorRegistry()
        _get_collectors(registry)
        out_before = generate_latest(registry).decode()
        assert "http_requests_total" in out_before

        reset_collectors_for_tests()

        assert registry not in _collectors_cache

        out_after = generate_latest(registry).decode()
        assert "http_requests_total" not in out_after
        assert "http_request_duration_seconds" not in out_after
        assert "http_requests_in_flight" not in out_after


# ---------------------------------------------------------------------------
# Middleware request-recording hot path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMiddlewareRecordsMetrics:
    async def test_counter_and_histogram_recorded(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, method="GET", path="/foo")

        out = generate_latest(registry).decode()

        assert "# TYPE http_requests_total counter" in out
        assert "# TYPE http_request_duration_seconds histogram" in out
        assert "# TYPE http_requests_in_flight gauge" in out
        assert 'http_requests_total{method="GET",path="/foo",status="200"} 1.0' in out

    async def test_in_flight_gauge_returns_to_zero(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, path="/foo")

        out = generate_latest(registry).decode()

        assert "http_requests_in_flight 0.0" in out

    async def test_status_is_captured_from_response_start(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(503), registry=registry)
        await _drive(mw, path="/boom")

        out = generate_latest(registry).decode()

        assert 'status="503"' in out

    async def test_path_is_normalised_in_labels(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, path="/api/v1/portfolio/12345")

        out = generate_latest(registry).decode()

        assert 'path="/api/v1/portfolio/:id"' in out

    async def test_exempt_path_records_nothing(self):
        registry = CollectorRegistry()
        mw = PrometheusMiddleware(_ok_app(200), registry=registry)
        await _drive(mw, path="/metrics")

        out = generate_latest(registry).decode()

        assert "http_requests_total{" not in out
        assert "http_request_duration_seconds_bucket{" not in out
        assert "http_request_duration_seconds_count{" not in out
        assert "http_requests_in_flight 0.0" in out

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

    async def test_malformed_response_status_falls_back_to_zero(self):
        """A non-integer / missing ``status`` on the response.start
        message must not crash the middleware; it falls back to ``0``
        so the request is still counted (under status="0").
        """

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": "oops"})
            await send({"type": "http.response.body", "body": b""})

        registry = CollectorRegistry()
        mw = PrometheusMiddleware(app, registry=registry)
        await _drive(mw, path="/weird")

        out = generate_latest(registry).decode()
        assert 'status="0"' in out

    async def test_exception_in_wrapped_app_still_decrements_in_flight(self):
        """If the wrapped app raises, the in-flight gauge must still
        settle back to zero — metric recording must never leak a
        phantom in-flight request.
        """

        async def app(scope, receive, send):
            raise RuntimeError("boom")

        registry = CollectorRegistry()
        mw = PrometheusMiddleware(app, registry=registry)
        with pytest.raises(RuntimeError):
            await _drive(mw, path="/explode")

        out = generate_latest(registry).decode()
        assert "http_requests_in_flight 0.0" in out


# ---------------------------------------------------------------------------
# Middleware defaults
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
# Path normalisation
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
