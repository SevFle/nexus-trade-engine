"""Tests for sandbox monitoring: event logging, metrics, violation reporting, and admin API."""

from __future__ import annotations

import time

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import (
    ImportViolation,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.monitoring.admin_api import (
    PolicySnapshot,
    SandboxAdminAPI,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import (
    SandboxMetricsCollector,
)
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport
from engine.plugins.trust_levels import TrustLevel


class TestSecurityEventLogger:
    def test_log_violation(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        violation = ImportViolation("os", plugin_id="test")
        logger.log_violation(violation)
        assert logger.event_count == 1

    def test_log_event(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="test event",
            attempted_action="test_action",
        )
        assert logger.event_count == 1

    def test_get_events_no_filter(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="event1",
        )
        logger.log_event(
            category=SandboxViolationCategory.NETWORK,
            detail="event2",
        )
        events = logger.get_events()
        assert len(events) == 2

    def test_get_events_with_category_filter(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="import event",
        )
        logger.log_event(
            category=SandboxViolationCategory.NETWORK,
            detail="network event",
        )
        events = logger.get_events(category=SandboxViolationCategory.IMPORT)
        assert len(events) == 1
        assert events[0].category is SandboxViolationCategory.IMPORT

    def test_get_events_limit(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        for i in range(20):
            logger.log_event(
                category=SandboxViolationCategory.IMPORT,
                detail=f"event_{i}",
            )
        events = logger.get_events(limit=5)
        assert len(events) == 5

    def test_get_events_since(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="old",
        )
        cutoff = time.time()
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="new",
        )
        events = logger.get_events_since(cutoff)
        assert len(events) == 1
        assert events[0].detail == "new"

    def test_clear_events(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="event",
        )
        assert logger.event_count == 1
        logger.clear()
        assert logger.event_count == 0

    def test_to_dicts(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="event",
            attempted_action="action",
        )
        dicts = logger.to_dicts()
        assert len(dicts) == 1
        assert dicts[0]["category"] == "import"
        assert dicts[0]["detail"] == "event"
        assert dicts[0]["plugin_id"] == "test"


class TestSandboxMetricsCollector:
    def test_record_evaluation(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 5)
        metrics = collector.get_plugin_metrics("p1")
        assert metrics is not None
        assert metrics["total_evaluations"] == 1
        assert metrics["total_signals_emitted"] == 5
        assert metrics["total_cpu_time_ms"] == 100.0

    def test_record_evaluation_with_error(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 50.0, 0, error="timeout")
        metrics = collector.get_plugin_metrics("p1")
        assert metrics["errors"] == 1
        assert metrics["last_error"] == "timeout"

    def test_record_violation(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_violation("p1")
        collector.record_violation("p1")
        metrics = collector.get_plugin_metrics("p1")
        assert metrics["security_violations"] == 2

    def test_get_all_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        all_metrics = collector.get_all_metrics()
        assert "p1" in all_metrics
        assert "p2" in all_metrics

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

    def test_avg_evaluation_ms(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 0)
        collector.record_evaluation("p1", 200.0, 0)
        metrics = collector.get_plugin_metrics("p1")
        assert metrics["avg_evaluation_ms"] == 150.0


class TestViolationReport:
    def test_empty_report(self) -> None:
        report = ViolationReport(plugin_id="test")
        assert report.total_violations == 0
        assert report.by_category == {}

    def test_from_events(self) -> None:
        logger = SecurityEventLogger(plugin_id="test")
        logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="import violation",
        )
        logger.log_event(
            category=SandboxViolationCategory.NETWORK,
            detail="network violation",
        )
        events = logger.get_events()
        report = ViolationReport.from_events(events, plugin_id="test", trust_level="untrusted")
        assert report.total_violations == 2
        assert report.by_category.get("import") == 1
        assert report.by_category.get("network") == 1

    def test_to_dict(self) -> None:
        report = ViolationReport(plugin_id="test", trust_level="untrusted")
        d = report.to_dict()
        assert d["plugin_id"] == "test"
        assert d["trust_level"] == "untrusted"
        assert "generated_at" in d
        assert "by_category" in d
        assert "by_layer" in d

    def test_to_json(self) -> None:
        import json

        report = ViolationReport(plugin_id="test")
        j = report.to_json()
        parsed = json.loads(j)
        assert parsed["plugin_id"] == "test"

    def test_summary(self) -> None:
        report = ViolationReport(plugin_id="test_plugin", trust_level="untrusted")
        report.by_category = {"import": 3}
        report.total_violations = 3
        summary = report.summary()
        assert "test_plugin" in summary
        assert "untrusted" in summary
        assert "3" in summary

    def test_by_layer_structure(self) -> None:
        report = ViolationReport()
        assert "import" in report.by_layer
        assert "network" in report.by_layer
        assert "resource" in report.by_layer
        assert "filesystem" in report.by_layer
        assert "introspection" in report.by_layer


class TestPolicySnapshot:
    def test_from_policy(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            "snapshot_test",
        )
        snapshot = PolicySnapshot.from_policy(policy)
        assert snapshot.plugin_id == "snapshot_test"
        assert snapshot.trust_level == "untrusted"
        assert len(snapshot.blocked_modules) > 0


class TestSandboxAdminAPI:
    def test_register_and_list_plugins(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "admin_test")
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        plugins = admin.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["plugin_id"] == "admin_test"
        ctx.cleanup()

    def test_unregister_context(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "remove_test")
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        admin.unregister_context("remove_test")
        plugins = admin.list_plugins()
        assert len(plugins) == 0
        ctx.cleanup()

    def test_get_policy(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "policy_test")
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        policy_dict = admin.get_policy("policy_test")
        assert policy_dict is not None
        assert policy_dict["plugin_id"] == "policy_test"
        assert policy_dict["trust_level"] == "untrusted"
        ctx.cleanup()

    def test_get_policy_nonexistent(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        assert admin.get_policy("nonexistent") is None

    def test_get_plugin_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("metrics_test", 100.0, 5)
        admin = SandboxAdminAPI(collector)
        metrics = admin.get_plugin_metrics("metrics_test")
        assert metrics is not None
        assert metrics["total_evaluations"] == 1

    def test_update_policy_when_inactive(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "update_test")
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        result = admin.update_policy("update_test", {"max_cpu_seconds": 45.0})
        assert result is not None
        assert result.updated_fields["max_cpu_seconds"] == 45.0
        ctx.cleanup()

    def test_update_policy_active_sandbox_blocked(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "active_test")
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        ctx.activate()
        try:
            result = admin.update_policy("active_test", {"max_cpu_seconds": 99.0})
            assert result is None
        finally:
            ctx.cleanup()

    def test_update_policy_unknown_plugin(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        result = admin.update_policy("unknown", {"max_cpu_seconds": 10})
        assert result is None

    def test_get_policy_history(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "history_test")
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        admin.update_policy("history_test", {"max_cpu_seconds": 10})
        admin.update_policy("history_test", {"max_cpu_seconds": 20})
        history = admin.get_policy_history("history_test")
        assert len(history) == 2
        ctx.cleanup()

    def test_get_security_events(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "events_test")
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        events = admin.get_security_events("events_test")
        assert isinstance(events, list)
        ctx.cleanup()

    def test_get_violation_report(self) -> None:
        collector = SandboxMetricsCollector()
        admin = SandboxAdminAPI(collector)
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "vr_test")
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        report = admin.get_violation_report("vr_test")
        assert report.plugin_id == "vr_test"
        ctx.cleanup()

    def test_get_all_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        admin = SandboxAdminAPI(collector)
        all_metrics = admin.get_all_metrics()
        assert len(all_metrics) == 2
