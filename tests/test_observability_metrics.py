"""Tests for engine.observability.metrics — backends, helpers, singleton."""

from __future__ import annotations

import threading
import time

import pytest

from engine.observability.metrics import (
    MetricsBackend,
    NullBackend,
    RecordingBackend,
    _canonical_tags,
    _check_name,
    get_metrics,
    set_metrics,
)


class TestCheckName:
    @pytest.mark.parametrize("name", ["", "  ", "\t", "\n"])
    def test_empty_or_whitespace_raises(self, name: str):
        with pytest.raises(ValueError, match="non-empty"):
            _check_name(name)

    @pytest.mark.parametrize("name", ["foo", "namespace.metric", "a"])
    def test_valid_name_passes(self, name: str):
        _check_name(name)


class TestCanonicalTags:
    def test_none_returns_empty_tuple(self):
        assert _canonical_tags(None) == ()

    def test_empty_dict_returns_empty_tuple(self):
        assert _canonical_tags({}) == ()

    def test_sorted_order(self):
        result = _canonical_tags({"b": "2", "a": "1"})
        assert result == (("a", "1"), ("b", "2"))

    def test_different_order_same_result(self):
        r1 = _canonical_tags({"a": "1", "b": "2"})
        r2 = _canonical_tags({"b": "2", "a": "1"})
        assert r1 == r2

    def test_single_tag(self):
        result = _canonical_tags({"env": "prod"})
        assert result == (("env", "prod"),)


class TestNullBackend:
    def setup_method(self):
        self.backend = NullBackend()

    def test_counter_accepts_valid_name(self):
        self.backend.counter("test.counter")

    def test_counter_with_value_and_tags(self):
        self.backend.counter("test.counter", value=5.0, tags={"env": "prod"})

    def test_gauge_accepts_valid_name(self):
        self.backend.gauge("test.gauge", 42.0)

    def test_histogram_accepts_valid_name(self):
        self.backend.histogram("test.histogram", 1.5)

    def test_timer_accepts_valid_name(self):
        with self.backend.timer("test.timer"):
            pass

    def test_counter_empty_name_raises(self):
        with pytest.raises(ValueError):
            self.backend.counter("")

    def test_gauge_empty_name_raises(self):
        with pytest.raises(ValueError):
            self.backend.gauge("", 1.0)

    def test_histogram_empty_name_raises(self):
        with pytest.raises(ValueError):
            self.backend.histogram("", 1.0)

    def test_timer_empty_name_raises(self):
        with pytest.raises(ValueError):
            with self.backend.timer(""):
                pass

    def test_timer_does_not_time(self):
        start = time.monotonic()
        with self.backend.timer("test.timer"):
            time.sleep(0.05)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.04

    def test_whitespace_name_raises(self):
        with pytest.raises(ValueError):
            self.backend.counter("   ")


class TestRecordingBackend:
    def setup_method(self):
        self.backend = RecordingBackend()

    def test_counter_accumulates(self):
        self.backend.counter("reqs", value=2.0)
        self.backend.counter("reqs", value=3.0)
        key = ("reqs", ())
        assert self.backend.counters[key] == pytest.approx(5.0)

    def test_counter_default_value(self):
        self.backend.counter("reqs")
        key = ("reqs", ())
        assert self.backend.counters[key] == pytest.approx(1.0)

    def test_counter_with_tags(self):
        self.backend.counter("reqs", tags={"method": "GET"})
        self.backend.counter("reqs", tags={"method": "POST"})
        assert self.backend.counters[("reqs", (("method", "GET"),))] == pytest.approx(1.0)
        assert self.backend.counters[("reqs", (("method", "POST"),))] == pytest.approx(1.0)

    def test_counter_tags_order_independent(self):
        self.backend.counter("reqs", tags={"a": "1", "b": "2"})
        self.backend.counter("reqs", tags={"b": "2", "a": "1"})
        key = ("reqs", (("a", "1"), ("b", "2")))
        assert self.backend.counters[key] == pytest.approx(2.0)

    def test_gauge_last_write_wins(self):
        self.backend.gauge("cpu", 50.0)
        self.backend.gauge("cpu", 80.0)
        key = ("cpu", ())
        assert self.backend.gauges[key] == pytest.approx(80.0)

    def test_gauge_with_tags(self):
        self.backend.gauge("cpu", 50.0, tags={"host": "a"})
        self.backend.gauge("cpu", 70.0, tags={"host": "b"})
        assert self.backend.gauges[("cpu", (("host", "a"),))] == pytest.approx(50.0)
        assert self.backend.gauges[("cpu", (("host", "b"),))] == pytest.approx(70.0)

    def test_histogram_appends(self):
        self.backend.histogram("latency", 1.0)
        self.backend.histogram("latency", 2.0)
        self.backend.histogram("latency", 3.0)
        key = ("latency", ())
        assert self.backend.histograms[key] == pytest.approx([1.0, 2.0, 3.0])

    def test_histogram_with_tags(self):
        self.backend.histogram("latency", 1.5, tags={"endpoint": "/api"})
        key = ("latency", (("endpoint", "/api"),))
        assert self.backend.histograms[key] == [pytest.approx(1.5)]

    def test_timer_records_elapsed_ms(self):
        with self.backend.timer("duration"):
            time.sleep(0.05)
        key = ("duration", ())
        assert len(self.backend.histograms[key]) == 1
        assert self.backend.histograms[key][0] >= 40.0

    def test_timer_with_tags(self):
        with self.backend.timer("duration", tags={"op": "query"}):
            pass
        key = ("duration", (("op", "query"),))
        assert len(self.backend.histograms[key]) == 1

    def test_counter_empty_name_raises(self):
        with pytest.raises(ValueError):
            self.backend.counter("")

    def test_gauge_empty_name_raises(self):
        with pytest.raises(ValueError):
            self.backend.gauge("", 1.0)

    def test_histogram_empty_name_raises(self):
        with pytest.raises(ValueError):
            self.backend.histogram("", 1.0)

    def test_timer_empty_name_raises(self):
        with pytest.raises(ValueError):
            with self.backend.timer(""):
                pass

    def test_different_names_different_keys(self):
        self.backend.counter("a")
        self.backend.counter("b")
        assert ("a", ()) in self.backend.counters
        assert ("b", ()) in self.backend.counters
        assert self.backend.counters[("a", ())] == pytest.approx(1.0)
        assert self.backend.counters[("b", ())] == pytest.approx(1.0)

    def test_none_tags_and_empty_tags_same_key(self):
        self.backend.counter("x", tags=None)
        self.backend.counter("x", tags={})
        assert self.backend.counters[("x", ())] == pytest.approx(2.0)

    def test_timer_records_on_exception(self):
        with pytest.raises(ValueError):
            with self.backend.timer("error_timer"):
                raise ValueError("boom")
        key = ("error_timer", ())
        assert len(self.backend.histograms[key]) == 1

    def test_thread_safety_counter(self):
        errors: list[Exception] = []

        def increment():
            try:
                for _ in range(100):
                    self.backend.counter("threaded", value=1.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=increment) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert self.backend.counters[("threaded", ())] == pytest.approx(500.0)


class TestSingleton:
    def test_default_is_null_backend(self):
        backend = get_metrics()
        assert isinstance(backend, NullBackend)

    def test_set_and_get(self):
        original = get_metrics()
        recording = RecordingBackend()
        set_metrics(recording)
        assert get_metrics() is recording
        set_metrics(original)

    def test_satisfies_protocol(self):
        assert isinstance(NullBackend(), MetricsBackend)
        assert isinstance(RecordingBackend(), MetricsBackend)
