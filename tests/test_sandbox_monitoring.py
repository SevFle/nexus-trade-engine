"""Comprehensive tests for engine.plugins.sandbox.monitoring (metrics + event logger)."""

from __future__ import annotations

import time

from engine.plugins.sandbox.core.violation import (
    ImportViolation,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEvent, SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import PluginMetrics, SandboxMetricsCollector

# ---------------------------------------------------------------------------
# PluginMetrics tests
# ---------------------------------------------------------------------------


class TestPluginMetrics:
    def test_defaults(self) -> None:
        m = PluginMetrics(plugin_id="test")
        assert m.plugin_id == "test"
        assert m.total_evaluations == 0
        assert m.total_signals_emitted == 0
        assert m.total_cpu_time_ms == 0.0
        assert m.avg_evaluation_ms == 0.0
        assert m.peak_memory_bytes == 0
        assert m.current_memory_bytes == 0
        assert m.api_calls == 0
        assert m.errors == 0
        assert m.last_error is None
        assert m.security_violations == 0
        assert m.file_operations == 0
        assert m.network_requests == 0

    def test_to_dict(self) -> None:
        m = PluginMetrics(plugin_id="test", total_evaluations=5, errors=1)
        d = m.to_dict()
        assert d["plugin_id"] == "test"
        assert d["total_evaluations"] == 5
        assert d["errors"] == 1
        assert "avg_evaluation_ms" in d
        assert "total_cpu_time_ms" in d

    def test_to_dict_rounds_floats(self) -> None:
        m = PluginMetrics(
            plugin_id="test",
            total_cpu_time_ms=123.456789,
            avg_evaluation_ms=45.6789,
        )
        d = m.to_dict()
        assert d["total_cpu_time_ms"] == 123.46
        assert d["avg_evaluation_ms"] == 45.68


# ---------------------------------------------------------------------------
# SandboxMetricsCollector tests
# ---------------------------------------------------------------------------


class TestSandboxMetricsCollector:
    def test_get_or_create_new(self) -> None:
        collector = SandboxMetricsCollector()
        m = collector.get_or_create("plugin_a")
        assert m.plugin_id == "plugin_a"

    def test_get_or_create_existing(self) -> None:
        collector = SandboxMetricsCollector()
        m1 = collector.get_or_create("plugin_a")
        m1.total_evaluations = 5
        m2 = collector.get_or_create("plugin_a")
        assert m2.total_evaluations == 5
        assert m1 is m2

    def test_record_evaluation_success(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 3)
        m = collector.get_or_create("p1")
        assert m.total_evaluations == 1
        assert m.total_signals_emitted == 3
        assert m.total_cpu_time_ms == 100.0
        assert m.errors == 0

    def test_record_evaluation_with_error(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 50.0, 0, error="crashed")
        m = collector.get_or_create("p1")
        assert m.errors == 1
        assert m.last_error == "crashed"

    def test_record_evaluation_accumulates(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 2)
        collector.record_evaluation("p1", 200.0, 3)
        m = collector.get_or_create("p1")
        assert m.total_evaluations == 2
        assert m.total_signals_emitted == 5
        assert m.total_cpu_time_ms == 300.0
        assert m.avg_evaluation_ms == 150.0

    def test_record_violation(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_violation("p1")
        collector.record_violation("p1")
        m = collector.get_or_create("p1")
        assert m.security_violations == 2

    def test_get_all_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        all_m = collector.get_all_metrics()
        assert "p1" in all_m
        assert "p2" in all_m
        assert all_m["p1"]["total_evaluations"] == 1
        assert all_m["p2"]["total_evaluations"] == 1

    def test_get_plugin_metrics_existing(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        result = collector.get_plugin_metrics("p1")
        assert result is not None
        assert result["total_evaluations"] == 1

    def test_get_plugin_metrics_nonexistent(self) -> None:
        collector = SandboxMetricsCollector()
        assert collector.get_plugin_metrics("nonexistent") is None

    def test_reset_specific_plugin(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        collector.reset("p1")
        assert collector.get_plugin_metrics("p1") is None
        assert collector.get_plugin_metrics("p2") is not None

    def test_reset_all(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        collector.reset()
        assert collector.get_all_metrics() == {}

    def test_multiple_plugins_independent(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        m1 = collector.get_or_create("p1")
        m2 = collector.get_or_create("p2")
        assert m1.total_evaluations == 1
        assert m2.total_evaluations == 1
        assert m1.total_signals_emitted == 1
        assert m2.total_signals_emitted == 2


# ---------------------------------------------------------------------------
# SecurityEvent tests
# ---------------------------------------------------------------------------


class TestSecurityEvent:
    def test_fields(self) -> None:
        event = SecurityEvent(
            timestamp=1000.0,
            category=SandboxViolationCategory.IMPORT,
            detail="os blocked",
            plugin_id="p1",
            attempted_action="import os",
            stack_trace=None,
        )
        assert event.timestamp == 1000.0
        assert event.category is SandboxViolationCategory.IMPORT
        assert event.detail == "os blocked"
        assert event.plugin_id == "p1"


# ---------------------------------------------------------------------------
# SecurityEventLogger tests
# ---------------------------------------------------------------------------


class TestSecurityEventLogger:
    def test_empty_logger(self) -> None:
        logger = SecurityEventLogger()
        assert logger.event_count == 0
        assert logger.get_events() == []

    def test_log_violation(self) -> None:
        logger = SecurityEventLogger(plugin_id="p1")
        v = ImportViolation("os", plugin_id="p1")
        logger.log_violation(v)
        assert logger.event_count == 1
        events = logger.get_events()
        assert events[0].category is SandboxViolationCategory.IMPORT
        assert "os" in events[0].detail

    def test_log_violation_uses_logger_plugin_id_fallback(self) -> None:
        logger = SecurityEventLogger(plugin_id="fallback_id")
        v = ImportViolation("os")
        logger.log_violation(v)
        events = logger.get_events()
        assert events[0].plugin_id == "fallback_id"

    def test_log_violation_uses_violation_plugin_id(self) -> None:
        logger = SecurityEventLogger(plugin_id="fallback_id")
        v = ImportViolation("os", plugin_id="explicit_id")
        logger.log_violation(v)
        events = logger.get_events()
        assert events[0].plugin_id == "explicit_id"

    def test_log_event(self) -> None:
        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_event(
            category=SandboxViolationCategory.NETWORK,
            detail="blocked host",
            attempted_action="connect:evil.com",
        )
        assert logger.event_count == 1
        events = logger.get_events()
        assert events[0].category is SandboxViolationCategory.NETWORK
        assert events[0].detail == "blocked host"

    def test_get_events_filter_by_category(self) -> None:
        logger = SecurityEventLogger()
        logger.log_event(category=SandboxViolationCategory.IMPORT, detail="import v")
        logger.log_event(category=SandboxViolationCategory.NETWORK, detail="network v")
        logger.log_event(category=SandboxViolationCategory.IMPORT, detail="import v2")
        import_events = logger.get_events(category=SandboxViolationCategory.IMPORT)
        assert len(import_events) == 2
        network_events = logger.get_events(category=SandboxViolationCategory.NETWORK)
        assert len(network_events) == 1

    def test_get_events_limit(self) -> None:
        logger = SecurityEventLogger()
        for i in range(10):
            logger.log_event(category=SandboxViolationCategory.IMPORT, detail=f"v{i}")
        events = logger.get_events(limit=3)
        assert len(events) == 3
        assert "v7" in events[0].detail
        assert "v9" in events[2].detail

    def test_get_events_since(self) -> None:
        logger = SecurityEventLogger()
        logger.log_event(category=SandboxViolationCategory.IMPORT, detail="old")
        time.sleep(0.01)
        cutoff = time.time()
        logger.log_event(category=SandboxViolationCategory.IMPORT, detail="new1")
        logger.log_event(category=SandboxViolationCategory.IMPORT, detail="new2")
        recent = logger.get_events_since(cutoff)
        assert len(recent) == 2

    def test_clear(self) -> None:
        logger = SecurityEventLogger()
        logger.log_event(category=SandboxViolationCategory.IMPORT, detail="v")
        assert logger.event_count == 1
        logger.clear()
        assert logger.event_count == 0

    def test_to_dicts(self) -> None:
        logger = SecurityEventLogger(plugin_id="p1")
        v = ImportViolation("os", plugin_id="p1")
        logger.log_violation(v)
        result = logger.to_dicts()
        assert len(result) == 1
        assert result[0]["category"] == "import"
        assert result[0]["plugin_id"] == "p1"
        assert "os" in result[0]["detail"]

    def test_to_dicts_limit(self) -> None:
        logger = SecurityEventLogger()
        for i in range(5):
            logger.log_event(category=SandboxViolationCategory.IMPORT, detail=f"v{i}")
        result = logger.to_dicts(limit=2)
        assert len(result) == 2

    def test_stack_trace_captured(self) -> None:
        logger = SecurityEventLogger()
        v = ImportViolation("os")
        logger.log_violation(v)
        events = logger.get_events()
        assert events[0].stack_trace is not None
