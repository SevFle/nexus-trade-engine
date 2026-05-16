"""Comprehensive tests for engine.plugins.sandbox.monitoring.admin_api (SandboxAdminAPI).

Covers:
- SandboxAdminAPI: register/unregister, get_security_events, get_policy, update_policy,
  get_policy_history, get_violation_report, list_plugins, get_plugin_metrics, get_all_metrics
- PolicySnapshot: construction, from_policy factory, field mapping
- PolicyUpdate: dataclass behavior
- ViolationReport: from_events, to_dict, to_json, summary, empty report
- NetworkGuard: _is_private_ip, _parse_cidr_networks, _is_host_allowed, _is_port_allowed,
  install/uninstall lifecycle, violation logging
- ResourceLimiter: parse_memory, thread limit enforcement, CPU/wall timers, install/uninstall
"""

from __future__ import annotations

import json
import time

import pytest

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import (
    ImportViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.monitoring.admin_api import (
    PolicySnapshot,
    PolicyUpdate,
    SandboxAdminAPI,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEvent
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport


def _make_policy(
    plugin_id: str = "test_plugin",
    trust_level: str = "untrusted",
    blocked_modules: set[str] | None = None,
    allowed_endpoints: list[str] | None = None,
    max_cpu_seconds: float = 30.0,
    max_memory_bytes: int = 512 * 1024 * 1024,
    max_file_descriptors: int = 64,
    max_threads: int = 4,
    wall_time_seconds: float = 60.0,
    read_only_paths: list[str] | None = None,
    read_write_paths: list[str] | None = None,
) -> SandboxPolicy:
    return SandboxPolicy(
        plugin_id=plugin_id,
        trust_level=trust_level,
        import_policy=ImportPolicy(blocked_modules=blocked_modules or {"os", "subprocess"}),
        network_policy=NetworkPolicy(allowed_endpoints=allowed_endpoints or []),
        resource_policy=ResourcePolicy(
            max_cpu_seconds=max_cpu_seconds,
            max_memory_bytes=max_memory_bytes,
            max_file_descriptors=max_file_descriptors,
            max_threads=max_threads,
            wall_time_seconds=wall_time_seconds,
        ),
        filesystem_policy=FilesystemPolicy(
            read_only_paths=read_only_paths or [],
            read_write_paths=read_write_paths or [],
        ),
    )


def _make_context(
    plugin_id: str = "test_plugin",
    active: bool = False,
) -> SandboxContext:
    policy = _make_policy(
        plugin_id=plugin_id,
        blocked_modules={"os", "subprocess", "socket", "shutil", "pathlib",
                         "io", "ctypes", "signal", "sys", "importlib", "threading"},
    )
    ctx = SandboxContext(policy=policy)
    if active:
        ctx._active = True
    return ctx


def _make_admin_with_contexts(
    plugin_ids: list[str] | None = None,
) -> tuple[SandboxAdminAPI, dict[str, SandboxContext]]:
    metrics = SandboxMetricsCollector()
    admin = SandboxAdminAPI(metrics_collector=metrics)
    contexts: dict[str, SandboxContext] = {}
    for pid in plugin_ids or ["p1", "p2"]:
        ctx = _make_context(plugin_id=pid)
        admin.register_context(ctx)
        contexts[pid] = ctx
    return admin, contexts


class TestPolicySnapshot:
    def test_from_policy_maps_all_fields(self) -> None:
        policy = _make_policy(
            plugin_id="snap_test",
            trust_level="limited",
            blocked_modules={"os", "subprocess"},
            allowed_endpoints=["api.example.com"],
            max_cpu_seconds=45.0,
            max_memory_bytes=256 * 1024 * 1024,
            max_file_descriptors=32,
            max_threads=2,
            wall_time_seconds=90.0,
            read_only_paths=["/data"],
            read_write_paths=["/var/output"],
        )
        snap = PolicySnapshot.from_policy(policy)
        assert snap.plugin_id == "snap_test"
        assert snap.trust_level == "limited"
        assert snap.blocked_modules == {"os", "subprocess"}
        assert snap.allowed_endpoints == ["api.example.com"]
        assert snap.max_cpu_seconds == 45.0
        assert snap.max_memory_bytes == 256 * 1024 * 1024
        assert snap.max_file_descriptors == 32
        assert snap.max_threads == 2
        assert snap.wall_time_seconds == 90.0
        assert snap.read_only_paths == ["/data"]
        assert snap.read_write_paths == ["/var/output"]
        assert snap.snapshot_at > 0

    def test_from_policy_defaults(self) -> None:
        policy = _make_policy(plugin_id="defaults")
        snap = PolicySnapshot.from_policy(policy)
        assert snap.blocked_modules == {"os", "subprocess"}
        assert snap.allowed_endpoints == []
        assert snap.read_only_paths == []
        assert snap.read_write_paths == []

    def test_snapshot_at_auto_set(self) -> None:
        before = time.time()
        snap = PolicySnapshot.from_policy(_make_policy())
        after = time.time()
        assert before <= snap.snapshot_at <= after

    def test_snapshot_at_default_factory(self) -> None:
        snap = PolicySnapshot(
            plugin_id="x",
            trust_level="untrusted",
            blocked_modules=set(),
            allowed_endpoints=[],
            max_cpu_seconds=1.0,
            max_memory_bytes=100,
            max_file_descriptors=10,
            max_threads=1,
            wall_time_seconds=1.0,
            read_only_paths=[],
            read_write_paths=[],
        )
        assert snap.snapshot_at > 0


class TestPolicyUpdate:
    def test_fields(self) -> None:
        update = PolicyUpdate(
            plugin_id="p1",
            updated_fields={"max_cpu_seconds": 60.0},
            previous_values={"max_cpu_seconds": 30.0},
        )
        assert update.plugin_id == "p1"
        assert update.updated_fields == {"max_cpu_seconds": 60.0}
        assert update.previous_values == {"max_cpu_seconds": 30.0}
        assert update.applied_at > 0

    def test_default_factories(self) -> None:
        update = PolicyUpdate(plugin_id="p1", updated_fields={"k": "v"})
        assert update.previous_values == {}
        assert update.applied_at > 0


class TestSandboxAdminAPIRegisterUnregister:
    def test_register_context(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        assert "p1" in admin._contexts
        assert "p1" in admin._policy_snapshots

    def test_register_multiple_contexts(self) -> None:
        admin, _ = _make_admin_with_contexts(["a", "b", "c"])
        assert len(admin._contexts) == 3
        assert set(admin._contexts.keys()) == {"a", "b", "c"}

    def test_register_creates_policy_snapshot(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        snap = admin._policy_snapshots["p1"]
        assert snap.plugin_id == "p1"

    def test_unregister_context(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1", "p2"])
        admin.unregister_context("p1")
        assert "p1" not in admin._contexts
        assert "p1" not in admin._policy_snapshots
        assert "p2" in admin._contexts

    def test_unregister_nonexistent_no_error(self) -> None:
        admin = SandboxAdminAPI(metrics_collector=SandboxMetricsCollector())
        admin.unregister_context("nonexistent")

    def test_register_replaces_existing(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        new_ctx = _make_context(plugin_id="p1")
        admin.register_context(new_ctx)
        assert admin._contexts["p1"] is new_ctx


class TestSandboxAdminAPIGetSecurityEvents:
    def test_empty_events(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        events = admin.get_security_events()
        assert events == []

    def test_get_events_specific_plugin(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        events = admin.get_security_events(plugin_id="p1")
        assert len(events) == 1
        assert events[0]["category"] == "import"
        assert events[0]["plugin_id"] == "p1"

    def test_get_events_all_plugins(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1", "p2"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        ctxs["p2"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p2"))
        events = admin.get_security_events()
        assert len(events) == 2

    def test_get_events_filter_by_category(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        ctxs["p1"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        import_events = admin.get_security_events(
            plugin_id="p1",
            category=SandboxViolationCategory.IMPORT,
        )
        assert len(import_events) == 1
        assert import_events[0]["category"] == "import"

    def test_get_events_filter_by_since(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        time.sleep(0.01)
        cutoff = time.time()
        time.sleep(0.01)
        ctxs["p1"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        recent = admin.get_security_events(plugin_id="p1", since=cutoff)
        assert len(recent) == 1
        assert recent[0]["category"] == "network"

    def test_get_events_limit(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        for i in range(10):
            ctxs["p1"].event_logger.log_violation(
                ImportViolation(f"module_{i}", plugin_id="p1")
            )
        events = admin.get_security_events(plugin_id="p1", limit=3)
        assert len(events) == 3

    def test_get_events_nonexistent_plugin(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        events = admin.get_security_events(plugin_id="nonexistent")
        assert events == []

    def test_get_events_all_plugins_aggregated_sorted(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1", "p2"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        time.sleep(0.01)
        ctxs["p2"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p2"))
        events = admin.get_security_events()
        assert events[0]["category"] == "network"
        assert events[1]["category"] == "import"

    def test_get_events_all_plugins_since_filter(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1", "p2"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        ctxs["p2"].event_logger.log_violation(ImportViolation("os", plugin_id="p2"))
        time.sleep(0.01)
        cutoff = time.time()
        time.sleep(0.01)
        ctxs["p1"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        events = admin.get_security_events(since=cutoff)
        assert len(events) == 1
        assert events[0]["category"] == "network"

    def test_events_to_dicts_format(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        events = admin.get_security_events(plugin_id="p1")
        event = events[0]
        assert "timestamp" in event
        assert "category" in event
        assert "detail" in event
        assert "plugin_id" in event
        assert "attempted_action" in event


class TestSandboxAdminAPIGetPolicy:
    def test_get_policy_existing(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        policy_dict = admin.get_policy("p1")
        assert policy_dict is not None
        assert policy_dict["plugin_id"] == "p1"
        assert policy_dict["trust_level"] == "untrusted"
        assert "import_policy" in policy_dict
        assert "network_policy" in policy_dict
        assert "resource_policy" in policy_dict
        assert "filesystem_policy" in policy_dict
        assert "snapshot_at" in policy_dict

    def test_get_policy_nonexistent(self) -> None:
        admin = SandboxAdminAPI(metrics_collector=SandboxMetricsCollector())
        assert admin.get_policy("nonexistent") is None

    def test_get_policy_structure(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        policy_dict = admin.get_policy("p1")
        assert policy_dict is not None
        assert isinstance(policy_dict["import_policy"]["blocked_modules"], list)
        assert isinstance(policy_dict["network_policy"]["allowed_endpoints"], list)
        assert isinstance(policy_dict["resource_policy"]["max_cpu_seconds"], float)
        assert isinstance(policy_dict["resource_policy"]["max_memory_bytes"], int)
        assert isinstance(policy_dict["filesystem_policy"]["read_only_paths"], list)
        assert isinstance(policy_dict["filesystem_policy"]["read_write_paths"], list)

    def test_get_policy_blocked_modules_sorted(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        policy_dict = admin.get_policy("p1")
        assert policy_dict is not None
        blocked = policy_dict["import_policy"]["blocked_modules"]
        assert blocked == sorted(blocked)


class TestSandboxAdminAPIUpdatePolicy:
    def test_update_resource_fields(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        result = admin.update_policy("p1", {"max_cpu_seconds": 120.0})
        assert result is not None
        assert result.updated_fields["max_cpu_seconds"] == 120.0
        assert result.previous_values["max_cpu_seconds"] == 30.0
        assert result.plugin_id == "p1"

    def test_update_multiple_fields(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        result = admin.update_policy("p1", {
            "max_cpu_seconds": 60.0,
            "max_memory_bytes": 1024 * 1024 * 1024,
        })
        assert result is not None
        assert "max_cpu_seconds" in result.updated_fields
        assert "max_memory_bytes" in result.updated_fields

    def test_update_network_fields(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        result = admin.update_policy("p1", {
            "allowed_endpoints": ["api.example.com", "data.example.com"],
            "block_dns": False,
        })
        assert result is not None
        assert "allowed_endpoints" in result.updated_fields
        assert "block_dns" in result.updated_fields
        assert result.updated_fields["block_dns"] is False

    def test_update_blocked_modules(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        new_modules = {"os", "subprocess", "socket", "http"}
        result = admin.update_policy("p1", {"blocked_modules": new_modules})
        assert result is not None
        assert "blocked_modules" in result.updated_fields
        assert result.updated_fields["blocked_modules"] == new_modules

    def test_update_filesystem_paths(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        result = admin.update_policy("p1", {
            "read_only_paths": ["/data"],
            "read_write_paths": ["/var/output"],
        })
        assert result is not None
        assert "read_only_paths" in result.updated_fields
        assert "read_write_paths" in result.updated_fields

    def test_update_unknown_plugin_returns_none(self) -> None:
        admin = SandboxAdminAPI(metrics_collector=SandboxMetricsCollector())
        result = admin.update_policy("nonexistent", {"max_cpu_seconds": 60.0})
        assert result is None

    def test_update_active_sandbox_returns_none(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"]._active = True
        result = admin.update_policy("p1", {"max_cpu_seconds": 60.0})
        assert result is None

    def test_update_creates_history_entry(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        admin.update_policy("p1", {"max_cpu_seconds": 60.0})
        history = admin.get_policy_history("p1")
        assert len(history) == 1
        assert history[0]["updated_fields"]["max_cpu_seconds"] == 60.0

    def test_update_refreshes_snapshot(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        admin.update_policy("p1", {"max_cpu_seconds": 120.0})
        snap = admin._policy_snapshots["p1"]
        assert snap.max_cpu_seconds == 120.0

    def test_update_preserves_previous_values(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        original_cpu = admin._contexts["p1"].policy.resource_policy.max_cpu_seconds
        result = admin.update_policy("p1", {"max_cpu_seconds": 999.0})
        assert result is not None
        assert result.previous_values["max_cpu_seconds"] == original_cpu

    def test_update_empty_updates(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        result = admin.update_policy("p1", {})
        assert result is not None
        assert result.updated_fields == {}

    def test_update_wall_time_seconds(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        result = admin.update_policy("p1", {"wall_time_seconds": 300.0})
        assert result is not None
        assert result.updated_fields["wall_time_seconds"] == 300.0
        assert admin._contexts["p1"].policy.resource_policy.wall_time_seconds == 300.0

    def test_update_max_threads(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        result = admin.update_policy("p1", {"max_threads": 8})
        assert result is not None
        assert result.updated_fields["max_threads"] == 8

    def test_update_max_file_descriptors(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        result = admin.update_policy("p1", {"max_file_descriptors": 128})
        assert result is not None
        assert result.updated_fields["max_file_descriptors"] == 128


class TestSandboxAdminAPIGetPolicyHistory:
    def test_empty_history(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        history = admin.get_policy_history("p1")
        assert history == []

    def test_single_update(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        admin.update_policy("p1", {"max_cpu_seconds": 60.0})
        history = admin.get_policy_history("p1")
        assert len(history) == 1
        assert history[0]["plugin_id"] == "p1"
        assert "applied_at" in history[0]
        assert "updated_fields" in history[0]
        assert "previous_values" in history[0]

    def test_multiple_updates(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        admin.update_policy("p1", {"max_cpu_seconds": 60.0})
        admin.update_policy("p1", {"max_cpu_seconds": 120.0})
        history = admin.get_policy_history("p1")
        assert len(history) == 2
        assert history[0]["updated_fields"]["max_cpu_seconds"] == 60.0
        assert history[1]["updated_fields"]["max_cpu_seconds"] == 120.0

    def test_history_limit(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        for i in range(10):
            admin.update_policy("p1", {"max_cpu_seconds": float(i)})
        history = admin.get_policy_history("p1", limit=3)
        assert len(history) == 3

    def test_history_nonexistent_plugin(self) -> None:
        admin = SandboxAdminAPI(metrics_collector=SandboxMetricsCollector())
        history = admin.get_policy_history("nonexistent")
        assert history == []

    def test_history_different_plugins_independent(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1", "p2"])
        admin.update_policy("p1", {"max_cpu_seconds": 60.0})
        admin.update_policy("p2", {"max_cpu_seconds": 90.0})
        h1 = admin.get_policy_history("p1")
        h2 = admin.get_policy_history("p2")
        assert len(h1) == 1
        assert len(h2) == 1
        assert h1[0]["updated_fields"]["max_cpu_seconds"] == 60.0
        assert h2[0]["updated_fields"]["max_cpu_seconds"] == 90.0


class TestSandboxAdminAPIGetViolationReport:
    def test_empty_report(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        report = admin.get_violation_report(plugin_id="p1")
        assert report.total_violations == 0

    def test_report_with_violations(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        ctxs["p1"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        report = admin.get_violation_report(plugin_id="p1")
        assert report.total_violations == 2
        assert report.plugin_id == "p1"
        assert "import" in report.by_category
        assert "network" in report.by_category

    def test_report_all_plugins(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1", "p2"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        ctxs["p2"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p2"))
        report = admin.get_violation_report()
        assert report.total_violations == 2
        assert report.plugin_id is None

    def test_report_includes_trust_level(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        report = admin.get_violation_report(plugin_id="p1")
        assert report.trust_level is not None

    def test_report_categorizes_by_layer(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        ctxs["p1"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        ctxs["p1"].event_logger.log_violation(
            ResourceExhausted("cpu_time", 30, 35, plugin_id="p1")
        )
        report = admin.get_violation_report(plugin_id="p1")
        assert len(report.by_layer["import"]) == 1
        assert len(report.by_layer["network"]) == 1
        assert len(report.by_layer["resource"]) == 1
        assert len(report.by_layer["filesystem"]) == 0


class TestSandboxAdminAPIListPlugins:
    def test_empty(self) -> None:
        admin = SandboxAdminAPI(metrics_collector=SandboxMetricsCollector())
        assert admin.list_plugins() == []

    def test_single_plugin(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1"])
        plugins = admin.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["plugin_id"] == "p1"
        assert plugins[0]["trust_level"] == "untrusted"
        assert plugins[0]["is_active"] is False
        assert "work_dir" in plugins[0]
        assert plugins[0]["violation_count"] == 0

    def test_multiple_plugins(self) -> None:
        admin, _ = _make_admin_with_contexts(["a", "b", "c"])
        plugins = admin.list_plugins()
        assert len(plugins) == 3

    def test_active_plugin_shows_status(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"]._active = True
        plugins = admin.list_plugins()
        assert plugins[0]["is_active"] is True

    def test_violation_count(self) -> None:
        admin, ctxs = _make_admin_with_contexts(["p1"])
        ctxs["p1"].event_logger.log_violation(ImportViolation("os", plugin_id="p1"))
        ctxs["p1"].event_logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        plugins = admin.list_plugins()
        assert plugins[0]["violation_count"] == 2

    def test_after_unregister(self) -> None:
        admin, _ = _make_admin_with_contexts(["p1", "p2"])
        admin.unregister_context("p1")
        plugins = admin.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["plugin_id"] == "p2"


class TestSandboxAdminAPIMetrics:
    def test_get_plugin_metrics_existing(self) -> None:
        metrics = SandboxMetricsCollector()
        metrics.record_evaluation("p1", 100.0, 3)
        admin = SandboxAdminAPI(metrics_collector=metrics)
        result = admin.get_plugin_metrics("p1")
        assert result is not None
        assert result["total_evaluations"] == 1

    def test_get_plugin_metrics_nonexistent(self) -> None:
        admin = SandboxAdminAPI(metrics_collector=SandboxMetricsCollector())
        assert admin.get_plugin_metrics("nonexistent") is None

    def test_get_all_metrics(self) -> None:
        metrics = SandboxMetricsCollector()
        metrics.record_evaluation("p1", 100.0, 1)
        metrics.record_evaluation("p2", 200.0, 2)
        admin = SandboxAdminAPI(metrics_collector=metrics)
        all_metrics = admin.get_all_metrics()
        assert "p1" in all_metrics
        assert "p2" in all_metrics
        assert all_metrics["p1"]["total_evaluations"] == 1
        assert all_metrics["p2"]["total_evaluations"] == 1

    def test_get_all_metrics_empty(self) -> None:
        admin = SandboxAdminAPI(metrics_collector=SandboxMetricsCollector())
        assert admin.get_all_metrics() == {}


class TestViolationReport:
    def test_from_events_empty(self) -> None:
        report = ViolationReport.from_events(events=[], plugin_id="p1")
        assert report.total_violations == 0
        assert report.plugin_id == "p1"
        assert report.by_category == {}
        for layer_events in report.by_layer.values():
            assert layer_events == []

    def test_from_events_categorizes(self) -> None:
        events = [
            SecurityEvent(
                timestamp=1000.0,
                category=SandboxViolationCategory.IMPORT,
                detail="os blocked",
                plugin_id="p1",
                attempted_action="import os",
                stack_trace=None,
            ),
            SecurityEvent(
                timestamp=1001.0,
                category=SandboxViolationCategory.NETWORK,
                detail="evil.com blocked",
                plugin_id="p1",
                attempted_action="connect:evil.com",
                stack_trace=None,
            ),
        ]
        report = ViolationReport.from_events(events, plugin_id="p1", trust_level="untrusted")
        assert report.total_violations == 2
        assert report.by_category["import"] == 1
        assert report.by_category["network"] == 1
        assert len(report.by_layer["import"]) == 1
        assert len(report.by_layer["network"]) == 1

    def test_from_events_mixed_categories(self) -> None:
        categories = [
            SandboxViolationCategory.IMPORT,
            SandboxViolationCategory.NETWORK,
            SandboxViolationCategory.RESOURCE,
            SandboxViolationCategory.FILESYSTEM,
            SandboxViolationCategory.INTROSPECTION,
        ]
        events = [
            SecurityEvent(
                timestamp=float(i),
                category=cat,
                detail=f"violation {i}",
                plugin_id="p1",
                attempted_action=f"action_{i}",
                stack_trace=None,
            )
            for i, cat in enumerate(categories)
        ]
        report = ViolationReport.from_events(events)
        assert report.total_violations == 5
        for cat in categories:
            assert report.by_category[cat.value] == 1
            assert len(report.by_layer[cat.value]) == 1

    def test_to_dict(self) -> None:
        report = ViolationReport(plugin_id="p1", trust_level="untrusted")
        result = report.to_dict()
        assert result["plugin_id"] == "p1"
        assert result["trust_level"] == "untrusted"
        assert "generated_at" in result
        assert "total_violations" in result
        assert "by_category" in result
        assert "by_layer" in result

    def test_to_json(self) -> None:
        report = ViolationReport(plugin_id="p1")
        json_str = report.to_json()
        parsed = json.loads(json_str)
        assert parsed["plugin_id"] == "p1"

    def test_to_json_custom_indent(self) -> None:
        report = ViolationReport(plugin_id="p1")
        json_str = report.to_json(indent=4)
        assert "    " in json_str

    def test_summary_empty(self) -> None:
        report = ViolationReport(plugin_id="p1", trust_level="untrusted")
        summary = report.summary()
        assert "p1" in summary
        assert "untrusted" in summary
        assert "Total violations: 0" in summary
        assert "By category:" not in summary

    def test_summary_with_violations(self) -> None:
        events = [
            SecurityEvent(
                timestamp=1000.0,
                category=SandboxViolationCategory.IMPORT,
                detail="os blocked",
                plugin_id="p1",
                attempted_action="import os",
                stack_trace=None,
            ),
        ]
        report = ViolationReport.from_events(events, plugin_id="p1", trust_level="untrusted")
        summary = report.summary()
        assert "Total violations: 1" in summary
        assert "By category:" in summary
        assert "import: 1" in summary

    def test_summary_all_plugins(self) -> None:
        report = ViolationReport()
        summary = report.summary()
        assert "all" in summary

    def test_generated_at_auto_set(self) -> None:
        before = time.time()
        report = ViolationReport()
        after = time.time()
        assert before <= report.generated_at <= after


class TestNetworkGuardHelpers:
    def test_is_private_ip_loopback(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("127.0.0.1") is True

    def test_is_private_ip_private_v4(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("10.0.0.1") is True
        assert _is_private_ip("172.16.0.1") is True
        assert _is_private_ip("192.168.1.1") is True

    def test_is_private_ip_link_local(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("169.254.1.1") is True

    def test_is_private_ip_public(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("8.8.8.8") is False
        assert _is_private_ip("1.1.1.1") is False

    def test_is_private_ip_ipv6_loopback(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("::1") is True

    def test_is_private_ip_ipv6_unique_local(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("fc00::1") is True

    def test_is_private_ip_ipv6_link_local(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("fe80::1") is True

    def test_is_private_ip_invalid(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("not-an-ip") is False

    def test_is_private_ip_zero(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("0.0.0.1") is True

    def test_parse_cidr_networks_valid(self) -> None:
        import ipaddress

        from engine.plugins.sandbox.layers.network_guard import _parse_cidr_networks
        networks = _parse_cidr_networks(["10.0.0.0/8", "192.168.0.0/16"])
        assert len(networks) == 2
        assert networks[0] == ipaddress.IPv4Network("10.0.0.0/8")
        assert networks[1] == ipaddress.IPv4Network("192.168.0.0/16")

    def test_parse_cidr_networks_invalid_skipped(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _parse_cidr_networks
        networks = _parse_cidr_networks(["10.0.0.0/8", "not-a-cidr", "192.168.0.0/16"])
        assert len(networks) == 2

    def test_parse_cidr_networks_empty(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _parse_cidr_networks
        assert _parse_cidr_networks([]) == []

    def test_host_allowed_via_endpoint(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy=policy, plugin_id="p1")
        assert guard._is_host_allowed("api.example.com") is True
        assert guard._is_host_allowed("sub.api.example.com") is True

    def test_host_blocked_no_endpoints(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy(allowed_endpoints=[])
        guard = NetworkGuard(policy=policy, plugin_id="p1")
        assert guard._is_host_allowed("api.example.com") is False

    def test_host_allowed_via_cidr(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy(allowed_cidrs=["10.0.0.0/8"])
        guard = NetworkGuard(policy=policy, plugin_id="p1")
        assert guard._is_host_allowed("10.0.0.5") is True

    def test_private_ip_blocked_without_cidr(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy=policy, plugin_id="p1")
        assert guard._is_host_allowed("127.0.0.1") is False
        assert guard._is_host_allowed("192.168.1.1") is False

    def test_port_allowed_no_policy(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy()
        guard = NetworkGuard(policy=policy)
        assert guard._is_port_allowed(443) is True

    def test_port_allowed_in_whitelist(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy(allowed_ports={443, 80})
        guard = NetworkGuard(policy=policy)
        assert guard._is_port_allowed(443) is True

    def test_port_blocked_not_in_whitelist(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy(allowed_ports={443})
        guard = NetworkGuard(policy=policy)
        assert guard._is_port_allowed(22) is False

    def test_port_none_allowed(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy(allowed_ports={443})
        guard = NetworkGuard(policy=policy)
        assert guard._is_port_allowed(None) is True


class TestNetworkGuardInstallUninstall:
    def test_install_sets_installed_flag(self) -> None:
        import socket

        import httpx

        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy()
        guard = NetworkGuard(policy=policy)
        orig_httpx = httpx.AsyncClient.send
        orig_socket = socket.create_connection
        orig_getaddrinfo = socket.getaddrinfo
        try:
            guard.install()
            assert guard._installed is True
            assert httpx.AsyncClient.send is not orig_httpx
            assert socket.create_connection is not orig_socket
            assert socket.getaddrinfo is not orig_getaddrinfo
        finally:
            guard.uninstall()
            assert httpx.AsyncClient.send is orig_httpx
            assert socket.create_connection is orig_socket
            assert socket.getaddrinfo is orig_getaddrinfo

    def test_double_install_noop(self) -> None:
        import httpx

        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy()
        guard = NetworkGuard(policy=policy)
        try:
            guard.install()
            first_send = httpx.AsyncClient.send
            guard.install()
            assert httpx.AsyncClient.send is first_send
        finally:
            guard.uninstall()

    def test_uninstall_without_install(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy()
        guard = NetworkGuard(policy=policy)
        guard.uninstall()
        assert guard._installed is False

    def test_get_violations_returns_copy(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy()
        guard = NetworkGuard(policy=policy)
        v = guard.get_violations()
        assert v == []
        assert v is not guard._violation_log

    def test_clear_violations(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import NetworkGuard
        policy = NetworkPolicy()
        guard = NetworkGuard(policy=policy)
        guard._violation_log.append(NetworkViolation("evil.com"))
        assert len(guard.get_violations()) == 1
        guard.clear_violations()
        assert len(guard.get_violations()) == 0


class TestResourceLimiterParseMemory:
    def test_parse_gb(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        assert ResourceLimiter.parse_memory("1GB") == 1024**3
        assert ResourceLimiter.parse_memory("2 GB") == 2 * 1024**3

    def test_parse_mb(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        assert ResourceLimiter.parse_memory("512MB") == 512 * 1024**2

    def test_parse_kb(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        assert ResourceLimiter.parse_memory("1024KB") == 1024 * 1024

    def test_parse_bytes(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        assert ResourceLimiter.parse_memory("1024B") == 1024

    def test_parse_plain_number(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        assert ResourceLimiter.parse_memory("2048") == 2048

    def test_parse_case_insensitive(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        assert ResourceLimiter.parse_memory("1gb") == 1024**3
        assert ResourceLimiter.parse_memory("512Mb") == 512 * 1024**2

    def test_parse_with_whitespace(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        assert ResourceLimiter.parse_memory("  1GB  ") == 1024**3

    def test_parse_float_gb(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        result = ResourceLimiter.parse_memory("1.5GB")
        assert result == int(1.5 * 1024**3)


class TestResourceLimiterThreadLimit:
    def test_increment_within_limit(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_threads=4)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        for _ in range(4):
            limiter.increment_thread()
        assert limiter._thread_count == 4

    def test_increment_exceeds_limit(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_threads=2)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        limiter.increment_thread()
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.increment_thread()
        assert exc_info.value.resource_type == "threads"
        assert exc_info.value.limit == 2

    def test_decrement_thread(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_threads=4)
        limiter = ResourceLimiter(policy=policy)
        limiter.increment_thread()
        limiter.increment_thread()
        limiter.decrement_thread()
        assert limiter._thread_count == 1

    def test_decrement_below_zero_clamped(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_threads=4)
        limiter = ResourceLimiter(policy=policy)
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_thread_limit_violation_logged(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_threads=1)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        assert len(limiter.get_violations()) == 1


class TestResourceLimiterInstallUninstall:
    def test_install_sets_flag(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy=policy)
        limiter.install()
        assert limiter._installed is True
        limiter.uninstall()
        assert limiter._installed is False

    def test_double_install_noop(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy=policy)
        limiter.install()
        limiter.install()
        assert limiter._installed is True
        limiter.uninstall()

    def test_uninstall_without_install(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy=policy)
        limiter.uninstall()
        assert limiter._installed is False

    def test_get_violations_returns_copy(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy=policy)
        assert limiter.get_violations() == []
        assert limiter.get_violations() is not limiter._violation_log

    def test_clear_violations(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy=policy)
        limiter._violation_log.append(
            ResourceExhausted("cpu_time", 30, 35)
        )
        assert len(limiter.get_violations()) == 1
        limiter.clear_violations()
        assert len(limiter.get_violations()) == 0


class TestResourceLimiterCPUTimer:
    def test_cpu_timer_triggers(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_cpu_seconds=0.05)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        limiter.install()
        time.sleep(0.1)
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.check_cpu_timer()
        assert exc_info.value.resource_type == "cpu_time"
        limiter.uninstall()

    def test_cpu_timer_not_expired(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_cpu_seconds=300)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        limiter.install()
        limiter.check_cpu_timer()
        limiter.uninstall()

    def test_cpu_elapsed_returns_positive(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_cpu_seconds=300)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        assert limiter.cpu_elapsed == 0.0
        limiter.install()
        time.sleep(0.01)
        assert limiter.cpu_elapsed > 0
        limiter.uninstall()

    def test_cpu_elapsed_no_timer(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy=policy)
        assert limiter.cpu_elapsed == 0.0


class TestResourceLimiterWallTimer:
    def test_wall_timer_triggers(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(wall_time_seconds=0.05)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        limiter.install()
        time.sleep(0.1)
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.check_wall_timer()
        assert exc_info.value.resource_type == "wall_time"
        limiter.uninstall()

    def test_wall_timer_not_expired(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(wall_time_seconds=300)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        limiter.install()
        limiter.check_wall_timer()
        limiter.uninstall()

    def test_wall_timer_logs_violation(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(wall_time_seconds=0.05)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        limiter.install()
        time.sleep(0.1)
        with pytest.raises(ResourceExhausted):
            limiter.check_wall_timer()
        assert len(limiter.get_violations()) == 1
        limiter.uninstall()


class TestResourceLimiterCpuViolationLog:
    def test_cpu_violation_logged(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
        policy = ResourcePolicy(max_cpu_seconds=0.05)
        limiter = ResourceLimiter(policy=policy, plugin_id="p1")
        limiter.install()
        time.sleep(0.1)
        with pytest.raises(ResourceExhausted):
            limiter.check_cpu_timer()
        assert len(limiter.get_violations()) == 1
        assert limiter.get_violations()[0].resource_type == "cpu_time"
        limiter.uninstall()
