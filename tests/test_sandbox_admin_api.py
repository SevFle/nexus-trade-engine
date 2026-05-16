"""
Comprehensive tests for engine.plugins.sandbox.monitoring.admin_api.

Covers SandboxAdminAPI, PolicySnapshot, PolicyUpdate — the monitoring/admin
module that was added in the last 5 commits but has ZERO test coverage.

Tests are organized as:
  1. PolicySnapshot unit tests
  2. PolicyUpdate unit tests
  3. SandboxAdminAPI CRUD (register/unregister/list/get_policy)
  4. SandboxAdminAPI update_policy (field-by-field mutations)
  5. SandboxAdminAPI security events retrieval (filtering, pagination)
  6. SandboxAdminAPI violation reports
  7. SandboxAdminAPI policy history
  8. Integration: admin_api + SandboxContext + all 5 security layers
  9. Edge cases and error conditions
"""

from __future__ import annotations

import time

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import (
    FilesystemViolation,
    ImportViolation,
    IntrospectionViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.monitoring.admin_api import (
    PolicySnapshot,
    PolicyUpdate,
    SandboxAdminAPI,
)
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport


def _policy(**overrides):
    defaults = {
        "plugin_id": "test-plugin",
        "trust_level": "untrusted",
        "import_policy": ImportPolicy(blocked_modules={f"mod_{i}" for i in range(15)}),
        "resource_policy": ResourcePolicy(
            max_cpu_seconds=30,
            max_memory_bytes=512 * 1024**2,
            max_file_descriptors=64,
            max_threads=1,
            wall_time_seconds=60.0,
        ),
        "filesystem_policy": FilesystemPolicy(
            read_only_paths=["/data/artifacts"],
            read_write_paths=[],
        ),
        "network_policy": NetworkPolicy(
            allowed_endpoints=["api.example.com"],
            allowed_ports={443},
            block_dns=True,
        ),
        "introspection_policy": IntrospectionPolicy(),
    }
    defaults.update(overrides)
    return SandboxPolicy(**defaults)


# ═══════════════════════════════════════════════════════════════════════
# PolicySnapshot
# ═══════════════════════════════════════════════════════════════════════


class TestPolicySnapshot:
    def test_from_policy_captures_all_fields(self) -> None:
        policy = _policy()
        snap = PolicySnapshot.from_policy(policy)
        assert snap.plugin_id == "test-plugin"
        assert snap.trust_level == "untrusted"
        assert "mod_0" in snap.blocked_modules
        assert len(snap.blocked_modules) == 15
        assert snap.allowed_endpoints == ["api.example.com"]
        assert snap.max_cpu_seconds == 30
        assert snap.max_memory_bytes == 512 * 1024**2
        assert snap.max_file_descriptors == 64
        assert snap.max_threads == 1
        assert snap.wall_time_seconds == 60.0
        assert snap.read_only_paths == ["/data/artifacts"]
        assert snap.read_write_paths == []
        assert snap.snapshot_at > 0

    def test_from_policy_with_custom_resource(self) -> None:
        policy = _policy(
            resource_policy=ResourcePolicy(
                max_cpu_seconds=120,
                max_memory_bytes=1024 * 1024**2,
                max_file_descriptors=128,
                max_threads=4,
                wall_time_seconds=300.0,
            ),
        )
        snap = PolicySnapshot.from_policy(policy)
        assert snap.max_cpu_seconds == 120
        assert snap.max_memory_bytes == 1024 * 1024**2
        assert snap.max_file_descriptors == 128
        assert snap.max_threads == 4
        assert snap.wall_time_seconds == 300.0

    def test_snapshot_at_is_realtime(self) -> None:
        before = time.time()
        snap = PolicySnapshot.from_policy(_policy())
        after = time.time()
        assert before <= snap.snapshot_at <= after

    def test_snapshot_is_dataclass(self) -> None:
        snap = PolicySnapshot.from_policy(_policy())
        d = snap.__dict__
        assert "plugin_id" in d
        assert "trust_level" in d

    def test_from_policy_preserves_rw_paths(self) -> None:
        policy = _policy(
            filesystem_policy=FilesystemPolicy(
                read_write_paths=["/var/sandbox/output", "/var/sandbox/logs"],
            ),
        )
        snap = PolicySnapshot.from_policy(policy)
        assert "/var/sandbox/output" in snap.read_write_paths
        assert "/var/sandbox/logs" in snap.read_write_paths


class TestPolicyUpdate:
    def test_update_records_fields(self) -> None:
        update = PolicyUpdate(
            plugin_id="p1",
            updated_fields={"max_cpu_seconds": 60},
            previous_values={"max_cpu_seconds": 30},
        )
        assert update.plugin_id == "p1"
        assert update.updated_fields == {"max_cpu_seconds": 60}
        assert update.previous_values == {"max_cpu_seconds": 30}
        assert update.applied_at > 0

    def test_update_defaults(self) -> None:
        update = PolicyUpdate(plugin_id="p2", updated_fields={})
        assert update.previous_values == {}
        assert update.applied_at > 0

    def test_update_with_multiple_fields(self) -> None:
        update = PolicyUpdate(
            plugin_id="p3",
            updated_fields={
                "max_cpu_seconds": 60,
                "block_dns": False,
                "allowed_endpoints": ["new.host.com"],
            },
            previous_values={
                "max_cpu_seconds": 30,
                "block_dns": True,
                "allowed_endpoints": ["old.host.com"],
            },
        )
        assert len(update.updated_fields) == 3
        assert len(update.previous_values) == 3


# ═══════════════════════════════════════════════════════════════════════
# SandboxAdminAPI — Registration & Listing
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxAdminAPIRegistration:
    def test_register_context_stores_it(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        assert "test-plugin" in api._contexts

    def test_register_creates_snapshot(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        assert "test-plugin" in api._policy_snapshots
        snap = api._policy_snapshots["test-plugin"]
        assert snap.plugin_id == "test-plugin"

    def test_unregister_removes_context_and_snapshot(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        api.unregister_context("test-plugin")
        assert "test-plugin" not in api._contexts
        assert "test-plugin" not in api._policy_snapshots

    def test_unregister_unknown_plugin_noop(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.unregister_context("nonexistent")

    def test_list_plugins_empty(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        assert api.list_plugins() == []

    def test_list_plugins_returns_info(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        plugins = api.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["plugin_id"] == "test-plugin"
        assert plugins[0]["trust_level"] == "untrusted"
        assert plugins[0]["is_active"] is False
        assert "work_dir" in plugins[0]
        assert plugins[0]["violation_count"] == 0

    def test_list_plugins_multiple(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy(plugin_id="p1")))
        api.register_context(SandboxContext(_policy(plugin_id="p2")))
        plugins = api.list_plugins()
        assert len(plugins) == 2
        ids = {p["plugin_id"] for p in plugins}
        assert ids == {"p1", "p2"}


# ═══════════════════════════════════════════════════════════════════════
# SandboxAdminAPI — Get Policy
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxAdminAPIGetPolicy:
    def test_get_policy_returns_dict(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        result = api.get_policy("test-plugin")
        assert result is not None
        assert result["plugin_id"] == "test-plugin"
        assert result["trust_level"] == "untrusted"
        assert "import_policy" in result
        assert "network_policy" in result
        assert "resource_policy" in result
        assert "filesystem_policy" in result
        assert result["snapshot_at"] > 0

    def test_get_policy_import_policy_sorted(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        result = api.get_policy("test-plugin")
        blocked = result["import_policy"]["blocked_modules"]
        assert blocked == sorted(blocked)

    def test_get_policy_resource_fields(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        result = api.get_policy("test-plugin")
        rp = result["resource_policy"]
        assert rp["max_cpu_seconds"] == 30
        assert rp["max_memory_bytes"] == 512 * 1024**2
        assert rp["max_file_descriptors"] == 64
        assert rp["max_threads"] == 1
        assert rp["wall_time_seconds"] == 60.0

    def test_get_policy_filesystem_fields(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        result = api.get_policy("test-plugin")
        fp = result["filesystem_policy"]
        assert fp["read_only_paths"] == ["/data/artifacts"]
        assert fp["read_write_paths"] == []

    def test_get_policy_unknown_returns_none(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        assert api.get_policy("nonexistent") is None

    def test_get_policy_after_unregister_returns_none(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        api.unregister_context("test-plugin")
        assert api.get_policy("test-plugin") is None


# ═══════════════════════════════════════════════════════════════════════
# SandboxAdminAPI — Update Policy
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxAdminAPIUpdatePolicy:
    def test_update_cpu_seconds(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        result = api.update_policy("test-plugin", {"max_cpu_seconds": 60})
        assert result is not None
        assert result.updated_fields["max_cpu_seconds"] == 60
        assert result.previous_values["max_cpu_seconds"] == 30
        assert ctx.policy.resource_policy.max_cpu_seconds == 60

    def test_update_memory_bytes(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        new_mem = 1024 * 1024**2
        api.update_policy("test-plugin", {"max_memory_bytes": new_mem})
        assert ctx.policy.resource_policy.max_memory_bytes == new_mem

    def test_update_file_descriptors(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        api.update_policy("test-plugin", {"max_file_descriptors": 128})
        assert ctx.policy.resource_policy.max_file_descriptors == 128

    def test_update_max_threads(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        api.update_policy("test-plugin", {"max_threads": 8})
        assert ctx.policy.resource_policy.max_threads == 8

    def test_update_wall_time(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        api.update_policy("test-plugin", {"wall_time_seconds": 120.0})
        assert ctx.policy.resource_policy.wall_time_seconds == 120.0

    def test_update_block_dns(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        api.update_policy("test-plugin", {"block_dns": False})
        assert ctx.policy.network_policy.block_dns is False

    def test_update_allowed_endpoints(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        api.update_policy(
            "test-plugin",
            {"allowed_endpoints": ["new.api.com", "other.api.com"]},
        )
        assert ctx.policy.network_policy.allowed_endpoints == [
            "new.api.com",
            "other.api.com",
        ]

    def test_update_blocked_modules(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        new_blocked = {"os", "subprocess", "socket"}
        api.update_policy("test-plugin", {"blocked_modules": new_blocked})
        assert ctx.policy.import_policy.blocked_modules == new_blocked

    def test_update_read_only_paths(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        new_paths = ["/data/new_artifact.bin"]
        api.update_policy("test-plugin", {"read_only_paths": new_paths})
        assert ctx.policy.filesystem_policy.read_only_paths == new_paths

    def test_update_read_write_paths(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        new_paths = ["/var/sandbox/output"]
        api.update_policy("test-plugin", {"read_write_paths": new_paths})
        assert ctx.policy.filesystem_policy.read_write_paths == new_paths

    def test_update_multiple_fields_at_once(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        result = api.update_policy(
            "test-plugin",
            {
                "max_cpu_seconds": 120,
                "max_threads": 4,
                "block_dns": False,
            },
        )
        assert result is not None
        assert result.updated_fields["max_cpu_seconds"] == 120
        assert result.updated_fields["max_threads"] == 4
        assert result.updated_fields["block_dns"] is False
        assert ctx.policy.resource_policy.max_cpu_seconds == 120
        assert ctx.policy.resource_policy.max_threads == 4
        assert ctx.policy.network_policy.block_dns is False

    def test_update_unknown_field_ignored(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        result = api.update_policy(
            "test-plugin",
            {"nonexistent_field": 42},
        )
        assert result is not None
        assert result.updated_fields == {}

    def test_update_returns_none_for_unknown_plugin(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        result = api.update_policy("nonexistent", {"max_cpu_seconds": 60})
        assert result is None

    def test_update_returns_none_for_active_sandbox(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        ctx.activate()
        try:
            result = api.update_policy("test-plugin", {"max_cpu_seconds": 60})
            assert result is None
        finally:
            ctx.deactivate()
            ctx.cleanup()

    def test_update_refreshes_snapshot(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        api.update_policy("test-plugin", {"max_cpu_seconds": 99})
        snap = api._policy_snapshots["test-plugin"]
        assert snap.max_cpu_seconds == 99


# ═══════════════════════════════════════════════════════════════════════
# SandboxAdminAPI — Metrics
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxAdminAPIMetrics:
    def test_get_plugin_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("test-plugin", 100.0, 3)
        api = SandboxAdminAPI(collector)
        result = api.get_plugin_metrics("test-plugin")
        assert result is not None
        assert result["total_evaluations"] == 1
        assert result["total_signals_emitted"] == 3

    def test_get_plugin_metrics_nonexistent(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        assert api.get_plugin_metrics("nonexistent") is None

    def test_get_all_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p2", 200.0, 2)
        api = SandboxAdminAPI(collector)
        result = api.get_all_metrics()
        assert "p1" in result
        assert "p2" in result

    def test_get_all_metrics_empty(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        assert api.get_all_metrics() == {}


# ═══════════════════════════════════════════════════════════════════════
# SandboxAdminAPI — Security Events
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxAdminAPIEvents:
    def test_get_events_for_plugin(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._collect_violations()
        events = api.get_security_events("test-plugin")
        assert len(events) >= 1
        assert events[0]["category"] == "import"

    def test_get_events_filter_by_category(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._network_layer._violation_log.append(
            NetworkViolation("evil.com", plugin_id="test-plugin"),
        )
        ctx._collect_violations()
        import_events = api.get_security_events(
            "test-plugin",
            category=SandboxViolationCategory.IMPORT,
        )
        assert all(e["category"] == "import" for e in import_events)

    def test_get_events_since(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        before = time.time()
        time.sleep(0.01)
        ctx.event_logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="recent event",
        )
        events = api.get_security_events("test-plugin", since=before)
        assert len(events) >= 1
        assert events[0]["detail"] == "recent event"

    def test_get_events_respects_limit(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        for i in range(20):
            ctx.event_logger.log_event(
                category=SandboxViolationCategory.IMPORT,
                detail=f"event_{i}",
            )
        events = api.get_security_events("test-plugin", limit=5)
        assert len(events) == 5

    def test_get_events_all_plugins(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx1 = SandboxContext(_policy(plugin_id="p1"))
        ctx2 = SandboxContext(_policy(plugin_id="p2"))
        api.register_context(ctx1)
        api.register_context(ctx2)
        ctx1.event_logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="p1 event",
        )
        ctx2.event_logger.log_event(
            category=SandboxViolationCategory.NETWORK,
            detail="p2 event",
        )
        events = api.get_security_events()
        assert len(events) == 2
        categories = {e["category"] for e in events}
        assert "import" in categories
        assert "network" in categories

    def test_get_events_all_plugins_sorted_by_timestamp_desc(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx1 = SandboxContext(_policy(plugin_id="p1"))
        ctx2 = SandboxContext(_policy(plugin_id="p2"))
        api.register_context(ctx1)
        api.register_context(ctx2)
        ctx1.event_logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="first",
        )
        time.sleep(0.01)
        ctx2.event_logger.log_event(
            category=SandboxViolationCategory.NETWORK,
            detail="second",
        )
        events = api.get_security_events()
        assert events[0]["detail"] == "second"
        assert events[1]["detail"] == "first"

    def test_get_events_unknown_plugin_returns_empty(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        events = api.get_security_events("nonexistent")
        assert events == []

    def test_events_to_dicts_format(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        ctx.event_logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="test detail",
            attempted_action="import os",
        )
        events = api.get_security_events("test-plugin")
        assert len(events) == 1
        e = events[0]
        assert "timestamp" in e
        assert e["category"] == "import"
        assert e["detail"] == "test detail"
        assert e["plugin_id"] == "test-plugin"
        assert e["attempted_action"] == "import os"


# ═══════════════════════════════════════════════════════════════════════
# SandboxAdminAPI — Policy History
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxAdminAPIPolicyHistory:
    def test_history_empty_initially(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        history = api.get_policy_history("test-plugin")
        assert history == []

    def test_history_records_update(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        api.update_policy("test-plugin", {"max_cpu_seconds": 60})
        history = api.get_policy_history("test-plugin")
        assert len(history) == 1
        assert history[0]["plugin_id"] == "test-plugin"
        assert history[0]["updated_fields"]["max_cpu_seconds"] == 60
        assert history[0]["previous_values"]["max_cpu_seconds"] == 30
        assert history[0]["applied_at"] > 0

    def test_history_accumulates(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        api.update_policy("test-plugin", {"max_cpu_seconds": 60})
        api.update_policy("test-plugin", {"max_threads": 4})
        history = api.get_policy_history("test-plugin")
        assert len(history) == 2

    def test_history_respects_limit(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        for i in range(10):
            api.update_policy("test-plugin", {"max_cpu_seconds": 30 + i})
        history = api.get_policy_history("test-plugin", limit=3)
        assert len(history) == 3

    def test_history_unknown_plugin(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        assert api.get_policy_history("nonexistent") == []

    def test_history_default_limit_50(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        api.register_context(SandboxContext(_policy()))
        for i in range(60):
            api.update_policy("test-plugin", {"max_cpu_seconds": 30 + i})
        history = api.get_policy_history("test-plugin")
        assert len(history) == 50


# ═══════════════════════════════════════════════════════════════════════
# SandboxAdminAPI — Violation Reports
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxAdminAPIViolationReport:
    def test_get_violation_report_for_plugin(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._network_layer._violation_log.append(
            NetworkViolation("evil.com", plugin_id="test-plugin"),
        )
        ctx._collect_violations()
        report = api.get_violation_report("test-plugin")
        assert isinstance(report, ViolationReport)
        assert report.plugin_id == "test-plugin"
        assert report.total_violations == 2
        assert "import" in report.by_category
        assert "network" in report.by_category

    def test_get_violation_report_all_plugins(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx1 = SandboxContext(_policy(plugin_id="p1"))
        ctx2 = SandboxContext(_policy(plugin_id="p2"))
        api.register_context(ctx1)
        api.register_context(ctx2)
        ctx1._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="p1"),
        )
        ctx2._network_layer._violation_log.append(
            NetworkViolation("evil.com", plugin_id="p2"),
        )
        ctx1._collect_violations()
        ctx2._collect_violations()
        report = api.get_violation_report()
        assert report.total_violations == 2

    def test_get_violation_report_trust_level(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        report = api.get_violation_report("test-plugin")
        assert report.trust_level == "untrusted"

    def test_get_violation_report_empty(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        report = api.get_violation_report("test-plugin")
        assert report.total_violations == 0
        assert report.by_category == {}


# ═══════════════════════════════════════════════════════════════════════
# Integration: Admin API + SandboxContext + 5-Layer Violations
# ═══════════════════════════════════════════════════════════════════════


class TestIntegrationAdminAPIMultiLayerViolations:
    def test_violations_from_all_5_layers_visible(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)

        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._network_layer._violation_log.append(
            NetworkViolation("evil.com", plugin_id="test-plugin"),
        )
        ctx._resource_layer._violation_log.append(
            ResourceExhausted("cpu_time", 30, 35, plugin_id="test-plugin"),
        )
        ctx._filesystem_layer._violation_log.append(
            FilesystemViolation("/etc/passwd", "read", plugin_id="test-plugin"),
        )
        ctx._introspection_layer._violation_log.append(
            IntrospectionViolation("__subclasses__", plugin_id="test-plugin"),
        )
        ctx._collect_violations()

        report = api.get_violation_report("test-plugin")
        assert report.total_violations == 5
        expected_categories = {
            "import", "network", "resource", "filesystem", "introspection",
        }
        assert set(report.by_category.keys()) == expected_categories

    def test_events_serialization_all_layers(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)

        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._network_layer._violation_log.append(
            NetworkViolation("evil.com", plugin_id="test-plugin"),
        )
        ctx._collect_violations()

        events = api.get_security_events("test-plugin")
        categories = {e["category"] for e in events}
        assert "import" in categories
        assert "network" in categories

    def test_violation_report_to_dict(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._collect_violations()
        report = api.get_violation_report("test-plugin")
        d = report.to_dict()
        assert d["plugin_id"] == "test-plugin"
        assert d["total_violations"] == 1
        assert "by_category" in d
        assert "by_layer" in d

    def test_violation_report_to_json(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._collect_violations()
        report = api.get_violation_report("test-plugin")
        json_str = report.to_json()
        assert "test-plugin" in json_str
        assert '"total_violations": 1' in json_str

    def test_metrics_collector_tracks_violations_via_context(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy(), metrics_collector=collector)
        api.register_context(ctx)

        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._collect_violations()

        metrics = api.get_plugin_metrics("test-plugin")
        assert metrics is not None
        assert metrics["security_violations"] == 1


class TestIntegrationAdminAPIContextLifecycle:
    def test_list_shows_active_after_activate(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        assert not api.list_plugins()[0]["is_active"]
        ctx.activate()
        assert api.list_plugins()[0]["is_active"]
        ctx.deactivate()
        assert not api.list_plugins()[0]["is_active"]
        ctx.cleanup()

    def test_violation_count_updates_after_events(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)

        assert api.list_plugins()[0]["violation_count"] == 0

        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._collect_violations()
        assert api.list_plugins()[0]["violation_count"] == 1

        ctx._network_layer._violation_log.append(
            NetworkViolation("evil.com", plugin_id="test-plugin"),
        )
        ctx._collect_violations()
        assert api.list_plugins()[0]["violation_count"] == 2

    def test_unregister_stops_tracking_events(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        ctx._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="test-plugin"),
        )
        ctx._collect_violations()
        assert len(api.get_security_events("test-plugin")) >= 1
        api.unregister_context("test-plugin")
        assert api.get_security_events("test-plugin") == []

    def test_sequential_contexts_independent_via_admin(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)

        ctx1 = SandboxContext(_policy(plugin_id="p1"))
        api.register_context(ctx1)
        ctx1._import_layer._violation_log.append(
            ImportViolation("os", plugin_id="p1"),
        )
        ctx1._collect_violations()
        ctx1.cleanup()
        api.unregister_context("p1")

        ctx2 = SandboxContext(_policy(plugin_id="p2"))
        api.register_context(ctx2)
        ctx2._network_layer._violation_log.append(
            NetworkViolation("evil.com", plugin_id="p2"),
        )
        ctx2._collect_violations()

        report = api.get_violation_report("p2")
        assert report.total_violations == 1
        assert "network" in report.by_category
        assert "import" not in report.by_category

    def test_update_after_deactivate_applies(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)

        ctx.activate()
        result_active = api.update_policy("test-plugin", {"max_cpu_seconds": 99})
        assert result_active is None
        ctx.deactivate()

        result_inactive = api.update_policy("test-plugin", {"max_cpu_seconds": 99})
        assert result_inactive is not None
        assert ctx.policy.resource_policy.max_cpu_seconds == 99
        ctx.cleanup()


# ═══════════════════════════════════════════════════════════════════════
# Edge Cases & Error Conditions
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxAdminAPIEdgeCases:
    def test_register_same_plugin_id_replaces(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx1 = SandboxContext(_policy(plugin_id="p1"))
        ctx2 = SandboxContext(_policy(plugin_id="p1"))
        api.register_context(ctx1)
        api.register_context(ctx2)
        assert api._contexts["p1"] is ctx2

    def test_get_events_with_no_contexts(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        events = api.get_security_events()
        assert events == []

    def test_get_events_category_filter_all_contexts(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx1 = SandboxContext(_policy(plugin_id="p1"))
        ctx2 = SandboxContext(_policy(plugin_id="p2"))
        api.register_context(ctx1)
        api.register_context(ctx2)
        ctx1.event_logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="import v",
        )
        ctx2.event_logger.log_event(
            category=SandboxViolationCategory.NETWORK,
            detail="network v",
        )
        import_events = api.get_security_events(
            category=SandboxViolationCategory.IMPORT,
        )
        assert len(import_events) == 1
        assert import_events[0]["detail"] == "import v"

    def test_events_since_all_contexts(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx1 = SandboxContext(_policy(plugin_id="p1"))
        ctx2 = SandboxContext(_policy(plugin_id="p2"))
        api.register_context(ctx1)
        api.register_context(ctx2)
        cutoff = time.time()
        time.sleep(0.01)
        ctx1.event_logger.log_event(
            category=SandboxViolationCategory.IMPORT,
            detail="new p1",
        )
        ctx2.event_logger.log_event(
            category=SandboxViolationCategory.NETWORK,
            detail="new p2",
        )
        events = api.get_security_events(since=cutoff)
        assert len(events) == 2

    def test_get_violation_report_for_unknown_plugin_no_context(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        report = api.get_violation_report("nonexistent")
        assert report.total_violations == 0

    def test_update_policy_preserves_unmodified_fields(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        original_mem = ctx.policy.resource_policy.max_memory_bytes
        api.update_policy("test-plugin", {"max_cpu_seconds": 99})
        assert ctx.policy.resource_policy.max_memory_bytes == original_mem

    def test_snapshot_after_multiple_updates(self) -> None:
        collector = SandboxMetricsCollector()
        api = SandboxAdminAPI(collector)
        ctx = SandboxContext(_policy())
        api.register_context(ctx)
        api.update_policy("test-plugin", {"max_cpu_seconds": 60})
        api.update_policy("test-plugin", {"max_threads": 8})
        snap = api._policy_snapshots["test-plugin"]
        assert snap.max_cpu_seconds == 60
        assert snap.max_threads == 8
