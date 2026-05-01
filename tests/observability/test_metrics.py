"""Tests for engine.observability.metrics — pluggable metrics backend."""

from __future__ import annotations

import time

import pytest

from engine.observability.metrics import (
    MetricsBackend,
    NullBackend,
    RecordingBackend,
    get_metrics,
    set_metrics,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    # Each test gets a clean slate; metrics is a process-singleton so
    # state would otherwise leak.
    yield
    set_metrics(NullBackend())


class TestProtocolShape:
    def test_null_backend_satisfies_protocol(self):
        assert isinstance(NullBackend(), MetricsBackend)

    def test_recording_backend_satisfies_protocol(self):
        assert isinstance(RecordingBackend(), MetricsBackend)


class TestNullBackend:
    def test_counter_does_not_raise(self):
        b = NullBackend()
        b.counter("orders.placed", 1)
        b.counter("orders.placed", 5, tags={"side": "buy"})

    def test_gauge_does_not_raise(self):
        NullBackend().gauge("queue.depth", 42)

    def test_histogram_does_not_raise(self):
        NullBackend().histogram("request.latency_ms", 12.3)

    def test_timer_is_no_op_context_manager(self):
        with NullBackend().timer("request.latency_ms"):
            time.sleep(0.001)


class TestRecordingBackend:
    # In-memory test double; production code never uses this but it's
    # the most ergonomic way to write assertions against emitted
    # metrics in unit tests.

    def test_counter_records_name_value_and_tags(self):
        b = RecordingBackend()
        b.counter("orders.placed", 1, tags={"side": "buy"})
        b.counter("orders.placed", 2, tags={"side": "buy"})
        assert b.counters == {("orders.placed", (("side", "buy"),)): 3.0}

    def test_counter_default_value_is_one(self):
        b = RecordingBackend()
        b.counter("orders.placed")
        b.counter("orders.placed")
        assert b.counters == {("orders.placed", ()): 2.0}

    def test_gauge_records_last_value(self):
        b = RecordingBackend()
        b.gauge("queue.depth", 10)
        b.gauge("queue.depth", 5)
        assert b.gauges == {("queue.depth", ()): 5.0}

    def test_histogram_records_observation_list(self):
        b = RecordingBackend()
        b.histogram("latency_ms", 1.0)
        b.histogram("latency_ms", 2.0)
        b.histogram("latency_ms", 3.0)
        assert b.histograms == {("latency_ms", ()): [1.0, 2.0, 3.0]}

    def test_timer_emits_histogram_observation(self):
        b = RecordingBackend()
        with b.timer("request.latency_ms"):
            pass
        key = ("request.latency_ms", ())
        assert key in b.histograms
        observations = b.histograms[key]
        assert len(observations) == 1
        assert observations[0] >= 0.0  # duration in ms

    def test_timer_records_even_on_exception(self):
        b = RecordingBackend()
        with pytest.raises(RuntimeError):
            with b.timer("request.latency_ms"):
                raise RuntimeError("boom")
        # Failure latency must still be captured.
        assert ("request.latency_ms", ()) in b.histograms

    def test_tag_order_does_not_change_aggregation_key(self):
        b = RecordingBackend()
        b.counter("x", 1, tags={"a": "1", "b": "2"})
        b.counter("x", 1, tags={"b": "2", "a": "1"})
        assert sum(b.counters.values()) == 2.0
        assert len(b.counters) == 1


class TestSingleton:
    def test_default_singleton_is_null_backend(self):
        assert isinstance(get_metrics(), NullBackend)

    def test_set_metrics_replaces_singleton(self):
        rec = RecordingBackend()
        set_metrics(rec)
        assert get_metrics() is rec

    def test_singleton_persists_across_calls(self):
        rec = RecordingBackend()
        set_metrics(rec)
        get_metrics().counter("x", 1)
        get_metrics().counter("x", 2)
        assert rec.counters == {("x", ()): 3.0}


class TestNameValidation:
    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            RecordingBackend().counter("")

    def test_whitespace_only_name_rejected(self):
        with pytest.raises(ValueError):
            RecordingBackend().counter("   ")
