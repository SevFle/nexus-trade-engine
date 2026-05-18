"""
Comprehensive tests covering issue #510 gaps: trust levels, StrategySandbox (new),
SandboxContext trust validation, policy edge cases, layer integration.

Targets code NOT covered by existing test files.
"""

from __future__ import annotations

import builtins
import contextlib
import os
import socket
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

from engine.plugins.sandbox._sandbox import SandboxMetrics
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.integration import SandboxIntegration
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
    _parse_memory,
)
from engine.plugins.sandbox.core.state import SandboxTLS, get_default_tls
from engine.plugins.sandbox.core.violation import (
    FilesystemViolation,
    ImportViolation,
    IntrospectionViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolation,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers.filesystem_isolation import (
    _BLOCKED_SYSTEM_PREFIXES,
    FilesystemIsolation,
)
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import (
    _BLOCKED_BUILTINS_DEFAULT,
    _EXPLICITLY_BLOCKED_ATTRS,
    _FRAME_ATTRS,
    _SAFE_DIR_ATTRS,
    _TRACEBACK_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.network_guard import NetworkGuard
from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.trust_levels import (
    TrustLevel,
    get_trust_level,
    get_trust_policy,
)


class TestTrustLevelEnum:
    def test_all_values(self) -> None:
        assert TrustLevel.TRUSTED_FULL.value == "trusted_full"
        assert TrustLevel.TRUSTED_LIMITED.value == "trusted_limited"
        assert TrustLevel.UNTRUSTED.value == "untrusted"

    def test_member_count(self) -> None:
        assert len(list(TrustLevel)) == 3

    def test_from_value(self) -> None:
        assert TrustLevel("trusted_full") is TrustLevel.TRUSTED_FULL
        assert TrustLevel("trusted_limited") is TrustLevel.TRUSTED_LIMITED
        assert TrustLevel("untrusted") is TrustLevel.UNTRUSTED

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            TrustLevel("invalid")


class TestGetTrustLevel:
    def test_manifest_with_trusted_full(self) -> None:
        manifest = SimpleNamespace(trust_level="trusted_full")
        assert get_trust_level(manifest) is TrustLevel.TRUSTED_FULL

    def test_manifest_with_trusted_limited(self) -> None:
        manifest = SimpleNamespace(trust_level="trusted_limited")
        assert get_trust_level(manifest) is TrustLevel.TRUSTED_LIMITED

    def test_manifest_with_untrusted(self) -> None:
        manifest = SimpleNamespace(trust_level="untrusted")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_without_trust_level(self) -> None:
        manifest = SimpleNamespace()
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_none_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level=None)
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_empty_string_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level="")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_with_invalid_trust_level(self) -> None:
        manifest = SimpleNamespace(trust_level="super_admin")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_manifest_without_attr(self) -> None:
        manifest = object()
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED


class TestGetTrustPolicy:
    def test_trusted_full_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert policy["import_restriction"] == "relaxed"
        assert policy["resource_multiplier"] == 4.0
        assert policy["introspection"] == "basic"

    def test_trusted_limited_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_LIMITED)
        assert policy["import_restriction"] == "standard"
        assert policy["resource_multiplier"] == 2.0
        assert policy["introspection"] == "standard"

    def test_untrusted_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.UNTRUSTED)
        assert policy["import_restriction"] == "strict"
        assert policy["resource_multiplier"] == 1.0
        assert policy["introspection"] == "strict"

    def test_all_policies_have_required_keys(self) -> None:
        required_keys = {
            "import_restriction",
            "network",
            "resource_multiplier",
            "filesystem",
            "introspection",
        }
        for level in TrustLevel:
            policy = get_trust_policy(level)
            assert required_keys.issubset(policy.keys()), f"Missing keys for {level}"


class TestSandboxMetrics:
    def test_all_defaults(self) -> None:
        m = SandboxMetrics()
        assert m.total_evaluations == 0
        assert m.total_signals_emitted == 0
        assert m.total_cpu_time_ms == 0.0
        assert m.avg_evaluation_ms == 0.0
        assert m.peak_memory_mb == 0.0
        assert m.errors == 0
        assert m.last_error is None
        assert m.api_calls == 0


class TestSandboxPolicyFromTrustLevel:
    def test_untrusted_with_defaults(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, plugin_id="test"
        )
        assert policy.plugin_id == "test"
        assert policy.trust_level == "untrusted"
        assert len(policy.import_policy.blocked_modules) > 0
        assert policy.resource_policy.max_cpu_seconds == 30.0

    def test_trusted_limited(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_LIMITED, plugin_id="limited"
        )
        assert policy.trust_level == "trusted_limited"
        assert policy.resource_policy.max_cpu_seconds == 60.0

    def test_trusted_full(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_FULL, plugin_id="full"
        )
        assert policy.trust_level == "trusted_full"
        assert policy.resource_policy.max_cpu_seconds == 120.0
        assert policy.resource_policy.max_memory_bytes == 4 * 512 * 1024 * 1024

    def test_with_network_endpoints(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            plugin_id="test",
            network_endpoints=["api.example.com"],
        )
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]

    def test_with_custom_cpu_seconds(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            plugin_id="test",
            max_cpu_seconds=60,
        )
        assert policy.resource_policy.max_cpu_seconds == 60.0

    def test_with_read_only_paths(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            plugin_id="test",
            read_only_paths=["/data"],
        )
        assert policy.filesystem_policy.read_only_paths == ["/data"]

    def test_resource_multiplier_untrusted(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, max_cpu_seconds=30
        )
        assert policy.resource_policy.max_cpu_seconds == 30.0

    def test_resource_multiplier_limited(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_LIMITED, max_cpu_seconds=30
        )
        assert policy.resource_policy.max_cpu_seconds == 60.0

    def test_resource_multiplier_full(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_FULL, max_cpu_seconds=30
        )
        assert policy.resource_policy.max_cpu_seconds == 120.0


class TestSandboxPolicyFromManifestEdgeCases:
    def test_manifest_with_permissions_for_untrusted(self) -> None:
        manifest = SimpleNamespace(
            id="untrusted_perm",
            trust_level="untrusted",
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
            artifacts=["/data"],
            permissions=["filesystem_write"],
            has_permission=lambda p: p == "filesystem_write",
            network=SimpleNamespace(allowed_endpoints=[]),
            requires_network=lambda: False,
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.filesystem_policy.read_write_paths == []

    def test_manifest_with_permissions_for_trusted_full(self) -> None:
        manifest = SimpleNamespace(
            id="full_perm",
            trust_level="trusted_full",
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
            artifacts=["/data"],
            permissions=["filesystem_write"],
            has_permission=lambda p: p == "filesystem_write",
            network=SimpleNamespace(allowed_endpoints=[]),
            requires_network=lambda: False,
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert "/data" in policy.filesystem_policy.read_write_paths


class TestSandboxContextTrustValidation:
    def test_untrusted_with_sufficient_blocked_modules(self) -> None:
        blocked = {f"mod_{i}" for i in range(15)}
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=blocked),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=[]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_untrusted_with_insufficient_blocked_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_with_cpu_exceeding_limit(self) -> None:
        blocked = {f"mod_{i}" for i in range(15)}
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=blocked),
            resource_policy=ResourcePolicy(max_cpu_seconds=120),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_with_rw_paths(self) -> None:
        blocked = {f"mod_{i}" for i in range(15)}
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=blocked),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/var"]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_limited_with_sufficient_blocked_modules(self) -> None:
        blocked = {f"mod_{i}" for i in range(10)}
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=blocked),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_limited_with_insufficient_blocked_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_limited_with_cpu_exceeding_limit(self) -> None:
        blocked = {f"mod_{i}" for i in range(10)}
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=blocked),
            resource_policy=ResourcePolicy(max_cpu_seconds=200),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_limited_with_rw_paths_allowed(self) -> None:
        blocked = {f"mod_{i}" for i in range(10)}
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=blocked),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/var"]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_trusted_full_always_valid(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules=set()),
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/"]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_invalid_trust_level_resolves_to_untrusted(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="super_admin",
        )
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.UNTRUSTED

    def test_activate_logs_event_on_invalid_trust(self) -> None:
        blocked = {f"mod_{i}" for i in range(15)}
        policy = SandboxPolicy(
            plugin_id="test_invalid_trust",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=blocked),
            resource_policy=ResourcePolicy(max_cpu_seconds=120),
        )
        ctx = SandboxContext(policy)
        try:
            with pytest.raises(SandboxViolation):
                ctx.activate()
            events = ctx.event_logger.get_events(
                category=SandboxViolationCategory.RESOURCE
            )
            assert any("validation failed" in e.detail for e in events)
        finally:
            ctx.deactivate()
            ctx.cleanup()


class TestFilesystemIsolationPathValidation:
    def test_blocked_system_prefixes(self) -> None:
        assert "/proc" in _BLOCKED_SYSTEM_PREFIXES
        assert "/sys" in _BLOCKED_SYSTEM_PREFIXES
        assert "/dev" in _BLOCKED_SYSTEM_PREFIXES

    def test_path_traversal_detected(self, tmp_path: Any) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="path_traversal"):
            fs._restricted_open("/../../../etc/passwd", "r")
        violations = fs.get_violations()
        assert any(v.operation == "path_traversal" for v in violations)
        fs.cleanup()

    def test_system_path_blocked_proc(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="system_path"):
            fs._restricted_open("/proc/self/cmdline", "r")
        fs.cleanup()

    def test_system_path_blocked_sys(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="system_path"):
            fs._restricted_open("/sys/kernel/version", "r")
        fs.cleanup()

    def test_system_path_blocked_dev(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="system_path"):
            fs._restricted_open("/dev/null", "r")
        fs.cleanup()

    def test_symlink_blocked_when_policy_enabled(self, tmp_path: Any) -> None:
        target = tmp_path / "target.txt"
        target.write_text("data")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        policy = FilesystemPolicy(
            read_only_paths=[str(tmp_path)],
            block_symlinks=True,
        )
        fs = FilesystemIsolation(policy)
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="symlink"):
            fs._restricted_open(str(link), "r")
        fs.cleanup()

    def test_symlink_allowed_when_policy_disabled(self, tmp_path: Any) -> None:
        target = tmp_path / "target.txt"
        target.write_text("data")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        policy = FilesystemPolicy(
            read_only_paths=[str(tmp_path)],
            block_symlinks=False,
        )
        fs = FilesystemIsolation(policy)
        try:
            fs._original_open = builtins.open
            resolved = fs._validate_path(str(link))
            assert resolved is not None
        finally:
            fs.cleanup()

    def test_path_traversal_logs_violation_with_plugin_id(self, tmp_path: Any) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="traversal_test")
        fs._original_open = builtins.open
        with pytest.raises(PermissionError):
            fs._restricted_open("../../etc/shadow", "r")
        violations = fs.get_violations()
        assert len(violations) == 1
        assert violations[0].plugin_id == "traversal_test"
        assert violations[0].operation == "path_traversal"
        fs.cleanup()

    def test_fd_access_logs_violation_with_plugin_id(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="fd_test")
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="fd_access"):
            fs._restricted_open(42, "r")
        violations = fs.get_violations()
        assert violations[0].plugin_id == "fd_test"
        fs.cleanup()


class TestIntrospectionGuardSetattr:
    def test_restricted_setattr_blocks_frame_attrs(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p1")
        guard._original_setattr = builtins.setattr
        with pytest.raises(PermissionError, match="tb_frame"):
            guard._restricted_setattr(object(), "tb_frame", None)
        violations = guard.get_violations()
        assert len(violations) == 1
        assert violations[0].attribute == "tb_frame"

    def test_restricted_setattr_blocks_traceback_attrs(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p1")
        guard._original_setattr = builtins.setattr
        with pytest.raises(PermissionError, match="__traceback__"):
            guard._restricted_setattr(object(), "__traceback__", None)

    def test_restricted_setattr_allows_normal(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard._original_setattr = builtins.setattr

        class _Obj:
            x = 0

        obj = _Obj()
        guard._restricted_setattr(obj, "x", 42)
        assert obj.x == 42


class TestIntrospectionGuardSafeDir:
    def test_safe_dir_filters_blocked_attrs(self) -> None:
        from engine.plugins.sandbox.layers.introspection_guard import _make_safe_dir

        guard = IntrospectionGuard(IntrospectionPolicy())
        original_dir = dir
        safe_dir = _make_safe_dir(original_dir, guard)
        result = safe_dir(str)
        for attr in _SAFE_DIR_ATTRS:
            assert attr not in result

    def test_safe_dir_preserves_normal_attrs(self) -> None:
        from engine.plugins.sandbox.layers.introspection_guard import _make_safe_dir

        guard = IntrospectionGuard(IntrospectionPolicy())
        original_dir = dir
        safe_dir = _make_safe_dir(original_dir, guard)
        result = safe_dir(str)
        assert "upper" in result
        assert "lower" in result
        assert "strip" in result


class TestIntrospectionGuardBlockedBuiltinSets:
    def test_explicitly_blocked_attrs_comprehensive(self) -> None:
        expected = frozenset({
            "__subclasses__",
            "__bases__",
            "__mro__",
            "__globals__",
            "__closure__",
            "__code__",
            "__dict__",
            "__class__",
            "__init_subclass__",
            "__instancecheck__",
            "__subclasscheck__",
            "__reduce__",
            "__reduce_ex__",
            "__getstate__",
            "__setstate__",
            "__builtins__",
            "__func__",
            "__self__",
            "__module__",
            "__weakref__",
        })
        assert expected == _EXPLICITLY_BLOCKED_ATTRS

    def test_frame_attrs_comprehensive(self) -> None:
        expected = {
            "tb_frame",
            "tb_lineno",
            "tb_next",
            "f_back",
            "f_builtins",
            "f_code",
            "f_globals",
            "f_locals",
            "f_trace",
        }
        assert expected == _FRAME_ATTRS

    def test_traceback_attrs_comprehensive(self) -> None:
        expected = {
            "__traceback__",
            "__context__",
            "__cause__",
            "tb_frame",
        }
        assert expected == _TRACEBACK_ATTRS

    def test_blocked_builtins_default_set(self) -> None:
        expected = {"eval", "exec", "compile", "breakpoint", "vars", "globals", "locals"}
        assert expected == _BLOCKED_BUILTINS_DEFAULT

    def test_safe_dir_attrs_is_superset(self) -> None:
        assert _EXPLICITLY_BLOCKED_ATTRS.issubset(_SAFE_DIR_ATTRS)
        assert _FRAME_ATTRS.issubset(_SAFE_DIR_ATTRS)
        assert _TRACEBACK_ATTRS.issubset(_SAFE_DIR_ATTRS)


class TestNetworkGuardCombinedHostLogic:
    def test_private_ip_blocked_unless_in_cidr(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=[],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("10.0.0.1") is True
        assert guard._is_host_allowed("192.168.1.1") is False
        assert guard._is_host_allowed("127.0.0.1") is False

    def test_endpoint_overrides_private_check(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["localhost"],
        )
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("localhost") is True

    def test_all_checks_fail_for_random_host(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("evil.attacker.com") is False

    def test_is_port_allowed_with_no_ports_configured(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(80) is True
        assert guard._is_port_allowed(443) is True

    def test_is_port_allowed_with_configured_ports(self) -> None:
        policy = NetworkPolicy(allowed_ports={443})
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(443) is True
        assert guard._is_port_allowed(80) is False

    def test_is_port_allowed_none(self) -> None:
        policy = NetworkPolicy(allowed_ports={443})
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(None) is True


class TestSandboxContextContextManager:
    def test_context_manager_activate_deactivate(self) -> None:
        blocked = {f"mod_{i}" for i in range(15)}
        policy = SandboxPolicy(
            plugin_id="ctx_mgr_test",
            import_policy=ImportPolicy(blocked_modules=blocked),
        )
        ctx = SandboxContext(policy)
        with ctx:
            assert ctx.is_active is True
        assert ctx.is_active is False
        ctx.cleanup()

    def test_context_manager_cleanup(self) -> None:
        blocked = {f"mod_{i}" for i in range(15)}
        policy = SandboxPolicy(
            plugin_id="ctx_cleanup",
            import_policy=ImportPolicy(blocked_modules=blocked),
        )
        ctx = SandboxContext(policy)
        work_dir = ctx.work_dir
        ctx.activate()
        ctx.cleanup()
        assert ctx.is_active is False
        assert not os.path.isdir(work_dir)


class TestSandboxContextViolationCollection:
    def test_violations_collected_from_all_layers(self) -> None:
        blocked = {f"mod_{i}" for i in range(15)} | {"os"}
        policy = SandboxPolicy(
            plugin_id="violation_collect",
            import_policy=ImportPolicy(blocked_modules=blocked),
        )
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            builtins.__import__("os")
        except ImportError:
            pass
        finally:
            ctx.deactivate()
        events = ctx.event_logger.get_events(category=SandboxViolationCategory.IMPORT)
        assert len(events) >= 1

    def test_violations_reported_to_metrics_collector(self) -> None:
        metrics = SandboxMetricsCollector()
        blocked = {f"mod_{i}" for i in range(15)} | {"os"}
        policy = SandboxPolicy(
            plugin_id="metrics_violation",
            import_policy=ImportPolicy(blocked_modules=blocked),
        )
        ctx = SandboxContext(policy, metrics_collector=metrics)
        ctx.activate()
        try:
            builtins.__import__("os")
        except ImportError:
            pass
        finally:
            ctx.deactivate()
        m = metrics.get_plugin_metrics("metrics_violation")
        assert m is not None
        assert m["security_violations"] >= 1
        ctx.cleanup()


class TestRestrictedImporterModule:
    def test_importlib_import_module_blocked(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p1")
        original_import = builtins.__import__
        try:
            importer.install()
            with pytest.raises(ImportError, match="blocked"):
                __import__("importlib").import_module("os")
        finally:
            importer.uninstall()
            assert builtins.__import__ is original_import

    def test_importlib_import_module_allows_safe(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p1")
        original_import = builtins.__import__
        try:
            importer.install()
            mod = __import__("importlib").import_module("json")
            assert mod is not None
        finally:
            importer.uninstall()
            assert builtins.__import__ is original_import


class TestSandboxViolationBase:
    def test_to_dict_all_fields(self) -> None:
        v = SandboxViolation(
            "test detail",
            category=SandboxViolationCategory.IMPORT,
            plugin_id="p1",
            attempted_action="import os",
        )
        d = v.to_dict()
        assert d["category"] == "import"
        assert d["detail"] == "test detail"
        assert d["plugin_id"] == "p1"
        assert d["attempted_action"] == "import os"

    def test_is_exception_subclass(self) -> None:
        assert issubclass(SandboxViolation, Exception)

    def test_all_violation_subclasses(self) -> None:
        assert issubclass(ImportViolation, SandboxViolation)
        assert issubclass(NetworkViolation, SandboxViolation)
        assert issubclass(FilesystemViolation, SandboxViolation)
        assert issubclass(IntrospectionViolation, SandboxViolation)
        assert issubclass(ResourceExhausted, SandboxViolation)


class TestNetworkViolationWithPort:
    def test_without_port(self) -> None:
        v = NetworkViolation("evil.com")
        assert "port" not in str(v)

    def test_with_port(self) -> None:
        v = NetworkViolation("evil.com", port=8080)
        assert "8080" in str(v)
        assert v.port == 8080

    def test_attempted_action_with_port(self) -> None:
        v = NetworkViolation("evil.com", port=443)
        assert v.attempted_action == "connect:evil.com:443"

    def test_attempted_action_without_port(self) -> None:
        v = NetworkViolation("evil.com")
        assert v.attempted_action == "connect:evil.com:None"


class TestParseMemoryEdgeCases:
    def test_empty_string(self) -> None:
        with pytest.raises(ValueError):
            _parse_memory("")

    def test_very_large_gb(self) -> None:
        result = _parse_memory("100GB")
        assert result == 100 * 1024**3

    def test_fractional_mb(self) -> None:
        result = _parse_memory("0.5MB")
        assert result == int(0.5 * 1024**2)

    def test_zero(self) -> None:
        assert _parse_memory("0") == 0

    def test_just_unit_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_memory("B")


class TestSandboxTLSSnapshot:
    def test_snapshot_with_bound_context(self) -> None:
        tls = SandboxTLS()
        policy = SandboxPolicy(plugin_id="snap_test")
        ctx = SandboxContext(policy)
        tls.bind(ctx)
        try:
            snap = tls.snapshot()
            assert snap["plugin_id"] == "snap_test"
            assert snap["trust_level"] == "untrusted"
            assert snap["is_active"] is False
        finally:
            tls.unbind()
            ctx.cleanup()

    def test_get_default_tls_singleton(self) -> None:
        tls1 = get_default_tls()
        tls2 = get_default_tls()
        assert tls1 is tls2


class TestSandboxIntegrationCreatePolicyAllLevels:
    def test_create_policy_all_trust_levels(self) -> None:
        for level in ("untrusted", "trusted_limited", "trusted_full"):
            policy = SandboxIntegration.create_policy(
                plugin_id=f"test_{level}",
                trust_level=level,
            )
            assert policy.plugin_id == f"test_{level}"

    def test_create_policy_with_all_overrides(self) -> None:
        policy = SandboxIntegration.create_policy(
            plugin_id="all_overrides",
            trust_level="untrusted",
            allowed_endpoints=["api.example.com"],
            max_cpu_seconds=120,
            max_memory_bytes=1024 * 1024 * 1024,
            blocked_modules={"os", "subprocess"},
        )
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]
        assert policy.resource_policy.max_cpu_seconds == 120
        assert policy.resource_policy.max_memory_bytes == 1024 * 1024 * 1024
        assert "os" in policy.import_policy.blocked_modules

    def test_create_policy_unknown_overrides_ignored(self) -> None:
        policy = SandboxIntegration.create_policy(
            plugin_id="test",
            trust_level="untrusted",
            unknown_field="value",
        )
        assert policy.plugin_id == "test"


class TestSandboxContextProperties:
    def test_policy_property(self) -> None:
        policy = SandboxPolicy(plugin_id="prop_test")
        ctx = SandboxContext(policy)
        assert ctx.policy is policy
        ctx.cleanup()

    def test_trust_level_property_untrusted(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="untrusted")
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.UNTRUSTED
        ctx.cleanup()

    def test_trust_level_property_limited(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="trusted_limited")
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.TRUSTED_LIMITED
        ctx.cleanup()

    def test_trust_level_property_full(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="trusted_full")
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.TRUSTED_FULL
        ctx.cleanup()

    def test_work_dir_is_temp_directory(self) -> None:
        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        assert ctx.work_dir.startswith(tempfile.gettempdir())
        ctx.cleanup()


class TestNetworkGuardSocketRestrictions:
    def test_restricted_create_connection_blocks_disallowed(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")
        guard._original_socket_create_connection = socket.create_connection
        with pytest.raises(PermissionError):
            guard._restricted_create_connection(("evil.com", 443))

    def test_restricted_create_connection_allows_whitelisted(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")
        guard._original_socket_create_connection = socket.create_connection
        violations_before = len(guard.get_violations())
        with contextlib.suppress(OSError, ConnectionRefusedError):
            guard._restricted_create_connection(("safe.com", 443))
        assert len(guard.get_violations()) == violations_before

    def test_restricted_getaddrinfo_blocks_dns(self) -> None:
        policy = NetworkPolicy(block_dns=True)
        guard = NetworkGuard(policy, plugin_id="p1")
        guard._original_getaddrinfo = socket.getaddrinfo
        with pytest.raises(PermissionError, match="DNS lookup"):
            guard._restricted_getaddrinfo("evil.com", 80)


class TestFilesystemIsolationWriteModes:
    def test_write_mode_blocked_for_read_only(self, tmp_path: Any) -> None:
        policy = FilesystemPolicy(read_only_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="write"):
            fs._restricted_open(str(tmp_path / "out.txt"), "w")
        fs.cleanup()

    def test_append_mode_blocked_for_read_only(self, tmp_path: Any) -> None:
        policy = FilesystemPolicy(read_only_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="write"):
            fs._restricted_open(str(tmp_path / "out.txt"), "a")
        fs.cleanup()

    def test_plus_mode_blocked_for_read_only(self, tmp_path: Any) -> None:
        policy = FilesystemPolicy(read_only_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="write"):
            fs._restricted_open(str(tmp_path / "out.txt"), "r+")
        fs.cleanup()

    def test_write_allowed_in_rw_path(self, tmp_path: Any) -> None:
        policy = FilesystemPolicy(read_write_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        try:
            fs._original_open = builtins.open
            test_file = str(tmp_path / "output.txt")
            result = fs._restricted_open(test_file, "w")
            result.close()
        finally:
            fs.cleanup()

    def test_write_allowed_in_work_dir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs._original_open = builtins.open
            test_file = os.path.join(fs.work_dir, "output.txt")
            result = fs._restricted_open(test_file, "w")
            result.close()
        finally:
            fs.cleanup()


class TestResourceLimiterInstallUninstallLifecycle:
    def test_cpu_timer_starts_on_install(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_cpu_seconds=300))
        limiter.install()
        assert limiter._cpu_timer is not None
        assert limiter._cpu_timer._start_time > 0
        limiter.uninstall()
        assert limiter._cpu_timer is None

    def test_wall_timer_starts_on_install(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(wall_time_seconds=300))
        limiter.install()
        assert limiter._wall_timer is not None
        limiter.uninstall()
        assert limiter._wall_timer is None

    def test_timers_stopped_on_uninstall(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.install()
        cpu_timer = limiter._cpu_timer
        wall_timer = limiter._wall_timer
        limiter.uninstall()
        assert cpu_timer._timer is None
        assert wall_timer._timer is None


class TestIntrospectionGuardInstallUninstall:
    def test_install_blocks_dir_output(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            result = dir(str)
            assert "__subclasses__" not in result
            assert "__globals__" not in result
            assert "upper" in result
        finally:
            guard.uninstall()

    def test_install_blocks_setattr_for_frame(self) -> None:
        original_setattr = builtins.setattr
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError):
                builtins.setattr(object(), "tb_frame", None)  # noqa: B010
        finally:
            guard.uninstall()
            assert builtins.setattr is original_setattr

    def test_install_restores_dir_on_uninstall(self) -> None:
        original_dir = builtins.__dict__.get("dir")
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.install()
        assert builtins.__dict__["dir"] is not original_dir
        guard.uninstall()
        assert builtins.__dict__["dir"] is original_dir
