"""
Comprehensive tests for the sandbox 5-layer security model.

Targets uncovered lines, edge cases, boundary values, cross-layer
interactions, error propagation, and trust-level enforcement.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib
import json
import os
import socket
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from engine.core.signal import Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
    _parse_memory,
)
from engine.plugins.sandbox.core.violation import (
    FilesystemViolation,
    ImportViolation,
    IntrospectionViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolation,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.executor import PluginSandboxExecutor
from engine.plugins.sandbox.layers.filesystem_isolation import FilesystemIsolation
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import (
    _EXPLICITLY_BLOCKED_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.network_guard import NetworkGuard
from engine.plugins.sandbox.layers.resource_limiter import (
    HAS_RESOURCE_MODULE,
    ResourceLimiter,
    _CPUTimer,
)
from engine.plugins.sandbox.monitoring.event_logger import (
    SecurityEvent,
    SecurityEventLogger,
)
from engine.plugins.sandbox.monitoring.metrics import (
    PluginMetrics,
    SandboxMetricsCollector,
)
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport
from engine.plugins.trust_levels import TrustLevel, get_trust_level, get_trust_policy


def _make_manifest(**overrides: Any) -> StrategyManifest:
    defaults = {
        "id": "test-plugin",
        "name": "Test Strategy",
        "version": "1.0.0",
        "author": "tester",
        "trust_level": "untrusted",
    }
    defaults.update(overrides)
    return StrategyManifest(**defaults)


# ─── ImportPolicy unit tests ───────────────────────────────────────────


class TestImportPolicy:
    def test_is_allowed_blocks_blocked_root(self):
        ip = ImportPolicy(blocked_modules={"os", "subprocess"})
        assert not ip.is_allowed("os")
        assert not ip.is_allowed("os.path")

    def test_is_allowed_allows_when_not_blocked(self):
        ip = ImportPolicy(blocked_modules={"os"})
        assert ip.is_allowed("math")
        assert ip.is_allowed("json")

    def test_is_allowed_with_allowlist_blocks_non_member(self):
        ip = ImportPolicy(
            allowed_modules={"math", "json"},
            blocked_modules=set(),
        )
        assert ip.is_allowed("math")
        assert not ip.is_allowed("os")

    def test_is_allowed_empty_allowlist_allows_all(self):
        ip = ImportPolicy(allowed_modules=set(), blocked_modules=set())
        assert ip.is_allowed("anything")

    def test_is_allowed_submodule_root_check(self):
        ip = ImportPolicy(blocked_modules={"os"})
        assert not ip.is_allowed("os.path")
        assert not ip.is_allowed("os.environ")


# ─── NetworkPolicy unit tests ──────────────────────────────────────────


class TestNetworkPolicy:
    def test_is_host_allowed_no_endpoints(self):
        np = NetworkPolicy(allowed_endpoints=[])
        assert not np.is_host_allowed("anything.com")

    def test_is_host_allowed_exact_match(self):
        np = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert np.is_host_allowed("api.example.com")

    def test_is_host_allowed_subdomain_match(self):
        np = NetworkPolicy(allowed_endpoints=["example.com"])
        assert np.is_host_allowed("sub.example.com")
        assert np.is_host_allowed("deep.sub.example.com")

    def test_is_host_allowed_no_partial_match(self):
        np = NetworkPolicy(allowed_endpoints=["example.com"])
        assert not np.is_host_allowed("notexample.com")

    def test_is_host_allowed_multiple_endpoints(self):
        np = NetworkPolicy(allowed_endpoints=["api.a.com", "api.b.com"])
        assert np.is_host_allowed("api.a.com")
        assert np.is_host_allowed("api.b.com")
        assert not np.is_host_allowed("api.c.com")


# ─── _parse_memory unit tests ──────────────────────────────────────────


class TestParseMemory:
    def test_gb(self):
        assert _parse_memory("2GB") == 2 * 1024**3

    def test_mb(self):
        assert _parse_memory("512MB") == 512 * 1024**2

    def test_kb(self):
        assert _parse_memory("256KB") == 256 * 1024

    def test_bytes(self):
        assert _parse_memory("1024B") == 1024

    def test_plain_number(self):
        assert _parse_memory("2048") == 2048

    def test_whitespace_and_case(self):
        assert _parse_memory("  4gb  ") == 4 * 1024**3

    def test_fractional(self):
        assert _parse_memory("1.5GB") == int(1.5 * 1024**3)


# ─── SandboxPolicy.from_manifest ───────────────────────────────────────


class TestSandboxPolicyFromManifest:
    def test_untrusted_no_network(self):
        m = _make_manifest()
        policy = SandboxPolicy.from_manifest(m)
        assert policy.trust_level == "untrusted"
        assert policy.network_policy.allowed_endpoints == []
        assert policy.import_policy.blocked_modules  # non-empty
        assert policy.plugin_id == "test-plugin"

    def test_trusted_full_with_filesystem_write(self):
        m = _make_manifest(
            trust_level="trusted_full",
            permissions=["filesystem_write"],
            artifacts=["/data/model.pkl"],
        )
        policy = SandboxPolicy.from_manifest(m)
        assert policy.trust_level == "trusted_full"
        assert "/data/model.pkl" in policy.filesystem_policy.read_write_paths
        assert "/data/model.pkl" in policy.filesystem_policy.read_only_paths

    def test_trusted_limited_with_filesystem_write(self):
        m = _make_manifest(
            trust_level="trusted_limited",
            permissions=["filesystem_write"],
            artifacts=["/data/file.bin"],
        )
        policy = SandboxPolicy.from_manifest(m)
        assert policy.trust_level == "trusted_limited"
        assert "/data/file.bin" in policy.filesystem_policy.read_write_paths

    def test_untrusted_no_filesystem_write_even_with_permission(self):
        m = _make_manifest(
            trust_level="untrusted",
            permissions=["filesystem_write"],
            artifacts=["/data/file.bin"],
        )
        policy = SandboxPolicy.from_manifest(m)
        assert policy.filesystem_policy.read_write_paths == []

    def test_network_endpoints_from_manifest(self):
        m = _make_manifest(
            network={"allowed_endpoints": ["api.market.com"]},
        )
        policy = SandboxPolicy.from_manifest(m)
        assert policy.network_policy.allowed_endpoints == ["api.market.com"]

    def test_custom_resources(self):
        m = _make_manifest(
            resources={"max_memory": "1GB", "max_cpu_seconds": 60, "gpu": "none"},
        )
        policy = SandboxPolicy.from_manifest(m)
        assert policy.resource_policy.max_cpu_seconds == 60 * 1.0
        assert policy.resource_policy.max_memory_bytes == 1024**3

    def test_manifest_without_id_uses_unknown(self):
        m = _make_manifest()
        delattr(m, "id")
        policy = SandboxPolicy.from_manifest(m)
        assert policy.plugin_id == "unknown"

    def test_manifest_without_artifacts(self):
        m = _make_manifest(trust_level="trusted_full")
        if hasattr(m, "artifacts"):
            m.artifacts = []
        policy = SandboxPolicy.from_manifest(m)
        assert policy.filesystem_policy.read_only_paths == []


# ─── SandboxPolicy.from_trust_level ────────────────────────────────────


class TestSandboxPolicyFromTrustLevel:
    def test_untrusted_defaults(self):
        p = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert p.trust_level == "untrusted"
        assert p.resource_policy.max_cpu_seconds == 30.0
        assert len(p.import_policy.blocked_modules) > 0

    def test_trusted_full_multiplier(self):
        p = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert p.resource_policy.max_cpu_seconds == 30.0 * 4.0
        assert p.resource_policy.max_memory_bytes == int(512 * 1024**2 * 4.0)

    def test_trusted_limited_multiplier(self):
        p = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "test")
        assert p.resource_policy.max_cpu_seconds == 30.0 * 2.0

    def test_custom_params(self):
        p = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            "custom",
            network_endpoints=["api.example.com"],
            max_cpu_seconds=10.0,
            max_memory_bytes=256 * 1024**2,
            read_only_paths=["/data"],
        )
        assert p.network_policy.allowed_endpoints == ["api.example.com"]
        assert p.resource_policy.max_cpu_seconds == 10.0
        assert p.filesystem_policy.read_only_paths == ["/data"]


# ─── SandboxPolicy.trusted_policy ──────────────────────────────────────


class TestTrustedPolicy:
    def test_trusted_policy_fields(self):
        p = SandboxPolicy.trusted_policy("my-plugin")
        assert p.plugin_id == "my-plugin"
        assert p.trust_level == "trusted_full"
        assert p.resource_policy.max_cpu_seconds == 300
        assert "subprocess" in p.import_policy.blocked_modules


# ─── SandboxViolation and subclasses ───────────────────────────────────


class TestViolations:
    def test_import_violation_fields(self):
        v = ImportViolation("os.path", plugin_id="p1")
        assert v.module_name == "os.path"
        assert v.category == SandboxViolationCategory.IMPORT
        assert v.plugin_id == "p1"
        assert "os.path" in v.detail
        assert v.attempted_action == "import os.path"

    def test_network_violation_with_port(self):
        v = NetworkViolation("evil.com", port=443, plugin_id="p2")
        assert v.host == "evil.com"
        assert v.port == 443
        assert "443" in v.detail
        assert v.attempted_action == "connect:evil.com:443"

    def test_network_violation_without_port(self):
        v = NetworkViolation("evil.com")
        assert v.port is None
        assert "evil.com" in v.detail

    def test_filesystem_violation(self):
        v = FilesystemViolation("/etc/passwd", "read", plugin_id="p3")
        assert v.path == "/etc/passwd"
        assert v.operation == "read"
        assert v.category == SandboxViolationCategory.FILESYSTEM

    def test_introspection_violation(self):
        v = IntrospectionViolation("__subclasses__", plugin_id="p4")
        assert v.attribute == "__subclasses__"
        assert v.category == SandboxViolationCategory.INTROSPECTION

    def test_resource_exhausted(self):
        v = ResourceExhausted("memory", 1024, 2048, plugin_id="p5")
        assert v.resource_type == "memory"
        assert v.limit == 1024
        assert v.current == 2048
        assert v.category == SandboxViolationCategory.RESOURCE

    def test_to_dict(self):
        v = ImportViolation("os", plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "import"
        assert d["plugin_id"] == "p1"
        assert d["attempted_action"] == "import os"
        assert "detail" in d

    def test_sandbox_violation_is_exception(self):
        v = SandboxViolation(
            "test",
            category=SandboxViolationCategory.RESOURCE,
            plugin_id="p",
            attempted_action="test_action",
        )
        assert isinstance(v, Exception)
        assert str(v) == "test"


# ─── SandboxViolationCategory ──────────────────────────────────────────


class TestViolationCategory:
    def test_all_categories_exist(self):
        cats = [c.value for c in SandboxViolationCategory]
        assert "import" in cats
        assert "network" in cats
        assert "resource" in cats
        assert "filesystem" in cats
        assert "introspection" in cats


# ─── TrustLevel and helpers ────────────────────────────────────────────


class TestTrustLevels:
    def test_get_trust_level_valid(self):
        m = SimpleNamespace(trust_level="trusted_full")
        assert get_trust_level(m) == TrustLevel.TRUSTED_FULL

    def test_get_trust_level_untrusted_default(self):
        m = SimpleNamespace()
        assert get_trust_level(m) == TrustLevel.UNTRUSTED

    def test_get_trust_level_invalid_string(self):
        m = SimpleNamespace(trust_level="nonexistent")
        assert get_trust_level(m) == TrustLevel.UNTRUSTED

    def test_get_trust_level_none(self):
        m = SimpleNamespace(trust_level=None)
        assert get_trust_level(m) == TrustLevel.UNTRUSTED

    def test_get_trust_policy_full(self):
        policy = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert policy["resource_multiplier"] == 4.0
        assert policy["introspection"] == "basic"

    def test_get_trust_policy_limited(self):
        policy = get_trust_policy(TrustLevel.TRUSTED_LIMITED)
        assert policy["resource_multiplier"] == 2.0

    def test_get_trust_policy_untrusted(self):
        policy = get_trust_policy(TrustLevel.UNTRUSTED)
        assert policy["resource_multiplier"] == 1.0
        assert policy["introspection"] == "strict"

    def test_trust_level_values(self):
        assert TrustLevel.TRUSTED_FULL.value == "trusted_full"
        assert TrustLevel.TRUSTED_LIMITED.value == "trusted_limited"
        assert TrustLevel.UNTRUSTED.value == "untrusted"


# ─── SandboxContext edge cases ─────────────────────────────────────────


class TestSandboxContextEdgeCases:
    def test_activate_raises_on_layer_failure(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        ctx = SandboxContext(policy)
        with patch.object(
            ctx._network_layer, "install", side_effect=RuntimeError("boom")
        ), pytest.raises(RuntimeError, match="boom"):
            ctx.activate()
        assert not ctx.is_active

    def test_double_activate_is_noop(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        ctx = SandboxContext(policy)
        ctx.activate()
        assert ctx.is_active
        ctx.activate()
        assert ctx.is_active
        ctx.deactivate()

    def test_double_deactivate_is_noop(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        ctx = SandboxContext(policy)
        ctx.deactivate()
        assert not ctx.is_active

    def test_context_manager(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        with SandboxContext(policy) as ctx:
            assert ctx.is_active
        assert not ctx.is_active

    def test_cleanup_deactivates_and_cleans(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        ctx = SandboxContext(policy)
        ctx.activate()
        assert ctx.is_active
        ctx.cleanup()
        assert not ctx.is_active

    def test_policy_property(self):
        policy = SandboxPolicy(plugin_id="test-prop")
        ctx = SandboxContext(policy)
        assert ctx.policy is policy
        assert ctx.policy.plugin_id == "test-prop"

    def test_event_logger_property(self):
        policy = SandboxPolicy(plugin_id="test-log")
        ctx = SandboxContext(policy)
        assert isinstance(ctx.event_logger, SecurityEventLogger)


# ─── _CPUTimer unit tests ──────────────────────────────────────────────


class TestCPUTimer:
    def test_expired_property_initially_false(self):
        t = _CPUTimer(10.0)
        assert not t.expired

    def test_check_raises_after_expiry(self):
        t = _CPUTimer(0.01)
        t.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "cpu_time"
        t.stop()

    def test_check_elapsed_exceeds_limit(self):
        t = _CPUTimer(0.01)
        t._start_time = time.monotonic() - 10.0
        with pytest.raises(ResourceExhausted):
            t.check()
        assert t.expired

    def test_stop_cancels_timer(self):
        t = _CPUTimer(100.0)
        t.start()
        t.stop()
        assert t._timer is None

    def test_elapsed_property(self):
        t = _CPUTimer(100.0)
        t.start()
        e = t.elapsed
        assert e >= 0
        t.stop()

    def test_elapsed_before_start(self):
        t = _CPUTimer(10.0)
        assert t.elapsed == 0.0

    def test_on_timeout_sets_expired(self):
        t = _CPUTimer(10.0, plugin_id="p1")
        t._on_timeout()
        assert t.expired

    def test_stop_before_start(self):
        t = _CPUTimer(10.0)
        t.stop()

    def test_check_before_start_no_crash(self):
        t = _CPUTimer(10.0)
        t._start_time = time.monotonic()
        t.check()


# ─── ResourceLimiter unit tests ────────────────────────────────────────


class TestResourceLimiter:
    def test_install_uninstall_cycle(self):
        rp = ResourcePolicy(max_cpu_seconds=60.0)
        rl = ResourceLimiter(rp, plugin_id="test")
        rl.install()
        assert rl._installed
        rl.uninstall()
        assert not rl._installed

    def test_double_install(self):
        rp = ResourcePolicy()
        rl = ResourceLimiter(rp)
        rl.install()
        rl.install()
        assert rl._installed
        rl.uninstall()

    def test_double_uninstall(self):
        rp = ResourcePolicy()
        rl = ResourceLimiter(rp)
        rl.uninstall()

    def test_thread_limit_exceeded(self):
        rp = ResourcePolicy(max_threads=1)
        rl = ResourceLimiter(rp, plugin_id="th-test")
        rl.increment_thread()
        with pytest.raises(ResourceExhausted) as exc_info:
            rl.increment_thread()
        assert exc_info.value.resource_type == "threads"

    def test_decrement_thread_floor(self):
        rp = ResourcePolicy(max_threads=5)
        rl = ResourceLimiter(rp)
        rl.decrement_thread()
        assert rl._thread_count == 0

    def test_cpu_elapsed_without_timer(self):
        rp = ResourcePolicy()
        rl = ResourceLimiter(rp)
        assert rl.cpu_elapsed == 0.0

    def test_cpu_elapsed_with_timer(self):
        rp = ResourcePolicy(max_cpu_seconds=60.0)
        rl = ResourceLimiter(rp)
        rl.install()
        assert rl.cpu_elapsed >= 0
        rl.uninstall()

    def test_violations_clear(self):
        rp = ResourcePolicy(max_threads=0)
        rl = ResourceLimiter(rp, plugin_id="v-clear")
        with pytest.raises(ResourceExhausted):
            rl.increment_thread()
        assert len(rl.get_violations()) == 1
        rl.clear_violations()
        assert len(rl.get_violations()) == 0

    def test_parse_memory_static(self):
        assert ResourceLimiter.parse_memory("1GB") == 1024**3
        assert ResourceLimiter.parse_memory("512MB") == 512 * 1024**2
        assert ResourceLimiter.parse_memory("1024") == 1024

    def test_check_cpu_timer_after_install(self):
        rp = ResourcePolicy(max_cpu_seconds=60.0)
        rl = ResourceLimiter(rp)
        rl.install()
        rl.check_cpu_timer()
        rl.uninstall()

    def test_thread_increment_decrement_cycle(self):
        rp = ResourcePolicy(max_threads=3)
        rl = ResourceLimiter(rp)
        rl.increment_thread()
        rl.increment_thread()
        assert rl._thread_count == 2
        rl.decrement_thread()
        assert rl._thread_count == 1

    @pytest.mark.skipif(not HAS_RESOURCE_MODULE, reason="no resource module")
    def test_apply_resource_limits_actually_sets(self):
        import resource

        rp = ResourcePolicy(max_file_descriptors=128, max_memory_bytes=256 * 1024**2)
        rl = ResourceLimiter(rp)
        original_nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
        rl.install()
        try:
            new_nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert new_nofile[0] <= 128
        finally:
            rl.uninstall()
            restored = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert restored[0] == original_nofile[0]


# ─── FilesystemIsolation edge cases ────────────────────────────────────


class TestFilesystemIsolation:
    def test_fd_access_blocked(self):
        fs = FilesystemIsolation(
            FilesystemPolicy(), plugin_id="fs-fd", work_dir=tempfile.mkdtemp()
        )
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open(0)  # noqa: SIM115
        finally:
            fs.uninstall()
            fs.cleanup()

    def test_read_outside_allowed_paths(self):
        work_dir = tempfile.mkdtemp()
        fs = FilesystemIsolation(
            FilesystemPolicy(read_only_paths=[]),
            plugin_id="fs-read",
            work_dir=work_dir,
        )
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/etc/passwd")  # noqa: SIM115
        finally:
            fs.uninstall()
            fs.cleanup()

    def test_write_in_work_dir_allowed(self):
        work_dir = tempfile.mkdtemp()
        fs = FilesystemIsolation(
            FilesystemPolicy(),
            plugin_id="fs-write-ok",
            work_dir=work_dir,
        )
        fs.install()
        try:
            test_file = os.path.join(work_dir, "test.txt")
            with builtins.open(test_file, "w") as f:
                f.write("hello")
            with builtins.open(test_file) as f:
                assert f.read() == "hello"
        finally:
            fs.uninstall()
            fs.cleanup()

    def test_write_outside_work_dir_blocked(self):
        work_dir = tempfile.mkdtemp()
        fs = FilesystemIsolation(
            FilesystemPolicy(read_only_paths=["/tmp"]),  # noqa: S108
            plugin_id="fs-write-block",
            work_dir=work_dir,
        )
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/tmp/some_test_file.txt", "w")  # noqa: SIM115, S108
        finally:
            fs.uninstall()
            fs.cleanup()

    def test_append_mode_is_write(self):
        work_dir = tempfile.mkdtemp()
        fs = FilesystemIsolation(
            FilesystemPolicy(read_only_paths=["/tmp"]),  # noqa: S108
            plugin_id="fs-append",
            work_dir=work_dir,
        )
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/tmp/test_append.txt", "a")  # noqa: SIM115, S108
        finally:
            fs.uninstall()
            fs.cleanup()

    def test_plus_mode_is_write(self):
        work_dir = tempfile.mkdtemp()
        fs = FilesystemIsolation(
            FilesystemPolicy(read_only_paths=["/tmp"]),  # noqa: S108
            plugin_id="fs-plus",
            work_dir=work_dir,
        )
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/tmp/test_plus.txt", "r+")  # noqa: SIM115, S108
        finally:
            fs.uninstall()
            fs.cleanup()

    def test_cleanup_removes_auto_created_work_dir(self):
        fs = FilesystemIsolation(FilesystemPolicy())
        wd = fs.work_dir
        assert os.path.isdir(wd)
        fs.cleanup()
        assert not os.path.isdir(wd)

    def test_cleanup_owned_work_dir(self):
        fs = FilesystemIsolation(FilesystemPolicy())
        wd = fs.work_dir
        assert os.path.isdir(wd)
        fs.cleanup()
        assert not os.path.isdir(wd)

    def test_cleanup_predefined_work_dir_not_deleted(self):
        work_dir = tempfile.mkdtemp()
        fs = FilesystemIsolation(
            FilesystemPolicy(), work_dir=work_dir
        )
        fs.cleanup()
        assert os.path.isdir(work_dir)
        os.rmdir(work_dir)

    def test_read_only_dir_with_sep(self):
        work_dir = tempfile.mkdtemp()
        ro_dir = tempfile.mkdtemp()
        test_file = os.path.join(ro_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("data")
        fs = FilesystemIsolation(
            FilesystemPolicy(read_only_paths=[ro_dir]),
            plugin_id="fs-rodir",
            work_dir=work_dir,
        )
        fs.install()
        try:
            with builtins.open(test_file) as f:
                assert f.read() == "data"
        finally:
            fs.uninstall()
            fs.cleanup()
            import shutil
            shutil.rmtree(ro_dir, ignore_errors=True)

    def test_double_install_noop(self):
        work_dir = tempfile.mkdtemp()
        fs = FilesystemIsolation(
            FilesystemPolicy(), work_dir=work_dir
        )
        fs.install()
        fs.install()
        fs.uninstall()
        fs.cleanup()

    def test_double_uninstall_noop(self):
        work_dir = tempfile.mkdtemp()
        fs = FilesystemIsolation(
            FilesystemPolicy(), work_dir=work_dir
        )
        fs.uninstall()
        fs.cleanup()

    def test_violations_tracked(self):
        fs = FilesystemIsolation(
            FilesystemPolicy(), plugin_id="fs-viol", work_dir=tempfile.mkdtemp()
        )
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open(0)  # noqa: SIM115
            assert len(fs.get_violations()) == 1
            assert fs.get_violations()[0].path == "<fd>"
        finally:
            fs.uninstall()
            fs.cleanup()

    def test_violations_clear(self):
        fs = FilesystemIsolation(
            FilesystemPolicy(), plugin_id="fs-vclr", work_dir=tempfile.mkdtemp()
        )
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open(0)  # noqa: SIM115
        finally:
            fs.uninstall()
        fs.clear_violations()
        assert len(fs.get_violations()) == 0
        fs.cleanup()


# ─── IntrospectionGuard edge cases ─────────────────────────────────────


class TestIntrospectionGuardEdgeCases:
    def test_restricted_setattr_frame_attrs(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-setattr"
        )
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.setattr(object(), "f_back", None)  # noqa: B010
        finally:
            guard.uninstall()

    def test_restricted_setattr_traceback_attrs(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-setattr-tb"
        )
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.setattr(object(), "__traceback__", None)  # noqa: B010
        finally:
            guard.uninstall()

    def test_restricted_setattr_allowed_attr(self):
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.install()
        try:
            obj = SimpleNamespace()
            builtins.setattr(obj, "normal_attr", 42)  # noqa: B010
            assert obj.normal_attr == 42
        finally:
            guard.uninstall()

    def test_blocked_builtins_eval(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-eval"
        )
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.eval("1+1")  # noqa: S307
        finally:
            guard.uninstall()

    def test_blocked_builtins_exec(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-exec"
        )
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.exec("pass")  # noqa: S102
        finally:
            guard.uninstall()

    def test_blocked_builtins_compile(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-compile"
        )
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.compile("1+1", "<test>", "eval")
        finally:
            guard.uninstall()

    def test_blocked_builtins_breakpoint(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-bp"
        )
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.breakpoint()
        finally:
            guard.uninstall()

    def test_safe_dir_filters_blocked_attrs(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-dir"
        )
        guard.install()
        try:
            result = builtins.dir(str)
            for attr in _EXPLICITLY_BLOCKED_ATTRS:
                assert attr not in result
        finally:
            guard.uninstall()

    def test_restricted_getattr_blocked(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-getattr"
        )
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr(object(), "__class__")  # noqa: B009
        finally:
            guard.uninstall()

    def test_restricted_getattr_allowed(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-getattr-ok"
        )
        guard.install()
        try:
            result = builtins.getattr(SimpleNamespace(x=1), "x")  # noqa: B009
            assert result == 1
        finally:
            guard.uninstall()

    def test_double_install_noop(self):
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.install()
        guard.install()
        guard.uninstall()

    def test_double_uninstall_noop(self):
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.uninstall()

    def test_violations_tracking(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-viol"
        )
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.eval("1")  # noqa: S307
        finally:
            guard.uninstall()
        assert len(guard.get_violations()) == 1
        assert guard.get_violations()[0].attribute == "eval"

    def test_violations_clear(self):
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.clear_violations()
        assert len(guard.get_violations()) == 0

    def test_is_blocked_attr_frame(self):
        guard = IntrospectionGuard(IntrospectionPolicy())
        assert guard._is_blocked_attr("f_back")
        assert guard._is_blocked_attr("tb_frame")

    def test_is_blocked_attr_explicit(self):
        guard = IntrospectionGuard(IntrospectionPolicy())
        for attr in _EXPLICITLY_BLOCKED_ATTRS:
            assert guard._is_blocked_attr(attr)

    def test_is_blocked_attr_normal(self):
        guard = IntrospectionGuard(IntrospectionPolicy())
        assert not guard._is_blocked_attr("name")
        assert not guard._is_blocked_attr("value")

    def test_subclasses_blocked_on_restricted_object(self):
        guard = IntrospectionGuard(
            IntrospectionPolicy(), plugin_id="ig-sub"
        )
        guard.install()
        try:
            with pytest.raises(RuntimeError):
                object.__subclasses__()
        finally:
            guard.uninstall()


# ─── NetworkGuard edge cases ──────────────────────────────────────────


class TestNetworkGuardEdgeCases:
    def test_allowed_host_passthrough_socket(self):
        np = NetworkPolicy(allowed_endpoints=["127.0.0.1"])
        ng = NetworkGuard(np, plugin_id="ng-pass")
        mock_conn = MagicMock()
        with patch.object(
            socket, "create_connection", return_value=mock_conn
        ) as mock_orig:
            ng._original_socket_create_connection = mock_orig
            result = ng._restricted_create_connection(("127.0.0.1", 80))
            assert result is mock_conn
            mock_orig.assert_called_once_with(("127.0.0.1", 80))

    def test_blocked_host_socket(self):
        np = NetworkPolicy(allowed_endpoints=["trusted.com"])
        ng = NetworkGuard(np, plugin_id="ng-block")
        ng.install()
        try:
            with pytest.raises(PermissionError):
                ng._restricted_create_connection(("evil.com", 443))
        finally:
            ng.uninstall()

    def test_dns_blocked(self):
        np = NetworkPolicy(allowed_endpoints=[], block_dns=True)
        ng = NetworkGuard(np, plugin_id="ng-dns")
        ng.install()
        try:
            with pytest.raises(PermissionError):
                socket.getaddrinfo("evil.com", 80)
        finally:
            ng.uninstall()

    def test_dns_allowed_for_whitelisted(self):
        np = NetworkPolicy(
            allowed_endpoints=["localhost"], block_dns=True
        )
        ng = NetworkGuard(np, plugin_id="ng-dns-ok")
        ng.install()
        try:
            result = socket.getaddrinfo("localhost", 80)
            assert len(result) > 0
        finally:
            ng.uninstall()

    def test_cidr_allowed(self):
        np = NetworkPolicy(
            allowed_endpoints=[], allowed_cidrs=["127.0.0.0/8"]
        )
        ng = NetworkGuard(np, plugin_id="ng-cidr")
        assert ng._is_host_in_cidr("127.0.0.1")
        assert not ng._is_host_in_cidr("192.168.1.1")

    def test_cidr_invalid_host(self):
        np = NetworkPolicy(
            allowed_endpoints=[], allowed_cidrs=["10.0.0.0/8"]
        )
        ng = NetworkGuard(np)
        assert not ng._is_host_in_cidr("not-an-ip")

    def test_invalid_cidr_ignored(self):
        np = NetworkPolicy(
            allowed_endpoints=[], allowed_cidrs=["not-a-cidr"]
        )
        ng = NetworkGuard(np)
        assert len(ng._cidr_networks) == 0

    def test_httpx_send_blocked(self):
        np = NetworkPolicy(allowed_endpoints=[])
        ng = NetworkGuard(np, plugin_id="ng-httpx")
        ng.install()
        try:
            async def _test():
                client = httpx.AsyncClient()
                req = httpx.Request("GET", "https://evil.com/api")
                with pytest.raises(PermissionError):
                    await httpx.AsyncClient.send(client, req)

            asyncio.get_event_loop().run_until_complete(_test())
        finally:
            ng.uninstall()

    def test_double_install_noop(self):
        np = NetworkPolicy()
        ng = NetworkGuard(np)
        ng.install()
        ng.install()
        ng.uninstall()

    def test_double_uninstall_noop(self):
        np = NetworkPolicy()
        ng = NetworkGuard(np)
        ng.uninstall()

    def test_violations_tracked(self):
        np = NetworkPolicy(allowed_endpoints=[])
        ng = NetworkGuard(np, plugin_id="ng-viol")
        ng.install()
        try:
            with pytest.raises(PermissionError):
                socket.getaddrinfo("evil.com", 80)
        finally:
            ng.uninstall()
        assert len(ng.get_violations()) == 1

    def test_violations_clear(self):
        ng = NetworkGuard(NetworkPolicy())
        ng.clear_violations()
        assert len(ng.get_violations()) == 0


# ─── RestrictedImporter (sandbox layers) edge cases ────────────────────


class TestRestrictedImporterEdgeCases:
    def test_importlib_import_module_blocked(self):
        ri = RestrictedImporter(
            blocked={"os"}, plugin_id="ri-importlib"
        )
        ri.install()
        try:
            with pytest.raises(ImportError):
                importlib.import_module("os")
        finally:
            ri.uninstall()

    def test_importlib_import_module_allowed(self):
        ri = RestrictedImporter(
            blocked=set(), plugin_id="ri-importlib-ok"
        )
        ri.install()
        try:
            mod = importlib.import_module("json")
            assert mod is not None
        finally:
            ri.uninstall()

    def test_find_spec_raises_for_blocked(self):
        ri = RestrictedImporter(blocked={"fake_blocked_module"})
        with pytest.raises(ImportError):
            ri.find_spec("fake_blocked_module")

    def test_find_spec_returns_none_for_allowed(self):
        ri = RestrictedImporter(blocked=set())
        result = ri.find_spec("json")
        assert result is None

    def test_is_module_blocked_with_allowlist(self):
        ri = RestrictedImporter(
            blocked=set(), allowed={"json", "math"}
        )
        assert not ri._is_module_blocked("json")
        assert ri._is_module_blocked("os")

    def test_is_module_blocked_submodule(self):
        ri = RestrictedImporter(blocked={"os"})
        assert ri._is_module_blocked("os.path")

    def test_violations_tracked(self):
        ri = RestrictedImporter(
            blocked={"nonexistent_test_module"}, plugin_id="ri-viol"
        )
        ri.install()
        try:
            with pytest.raises(ImportError):
                builtins.__import__("nonexistent_test_module")
        finally:
            ri.uninstall()
        assert len(ri.get_violations()) >= 1

    def test_violations_clear(self):
        ri = RestrictedImporter(blocked={"os"})
        ri.clear_violations()
        assert len(ri.get_violations()) == 0

    def test_relative_import_not_blocked(self):
        ri = RestrictedImporter(blocked={"os"})
        assert not ri._is_module_blocked("oss")


# ─── SecurityEventLogger edge cases ────────────────────────────────────


class TestSecurityEventLogger:
    def test_log_violation(self):
        logger = SecurityEventLogger(plugin_id="p1")
        v = ImportViolation("os", plugin_id="p1")
        logger.log_violation(v)
        assert logger.event_count == 1

    def test_log_event(self):
        logger = SecurityEventLogger(plugin_id="p2")
        logger.log_event(
            SandboxViolationCategory.NETWORK,
            "test detail",
            attempted_action="test_action",
        )
        assert logger.event_count == 1

    def test_get_events_with_category_filter(self):
        logger = SecurityEventLogger(plugin_id="p3")
        logger.log_event(SandboxViolationCategory.IMPORT, "imp")
        logger.log_event(SandboxViolationCategory.NETWORK, "net")
        events = logger.get_events(category=SandboxViolationCategory.IMPORT)
        assert len(events) == 1
        assert events[0].detail == "imp"

    def test_get_events_limit(self):
        logger = SecurityEventLogger(plugin_id="p4")
        for i in range(10):
            logger.log_event(SandboxViolationCategory.IMPORT, f"evt-{i}")
        events = logger.get_events(limit=3)
        assert len(events) == 3

    def test_get_events_since(self):
        logger = SecurityEventLogger(plugin_id="p5")
        logger.log_event(SandboxViolationCategory.IMPORT, "old")
        after = time.time()
        logger.log_event(SandboxViolationCategory.IMPORT, "new")
        events = logger.get_events_since(after)
        assert len(events) == 1
        assert events[0].detail == "new"

    def test_to_dicts(self):
        logger = SecurityEventLogger(plugin_id="p6")
        logger.log_event(SandboxViolationCategory.FILESYSTEM, "fs-detail")
        dicts = logger.to_dicts()
        assert len(dicts) == 1
        assert dicts[0]["category"] == "filesystem"

    def test_clear(self):
        logger = SecurityEventLogger(plugin_id="p7")
        logger.log_event(SandboxViolationCategory.IMPORT, "x")
        logger.clear()
        assert logger.event_count == 0

    def test_violation_uses_own_plugin_id_when_violation_has_none(self):
        logger = SecurityEventLogger(plugin_id="logger-pid")
        v = ImportViolation("os")
        assert v.plugin_id is None
        logger.log_violation(v)
        events = logger.get_events()
        assert events[0].plugin_id == "logger-pid"


# ─── ViolationReport tests ─────────────────────────────────────────────


class TestViolationReport:
    def test_from_events(self):
        events = [
            SecurityEvent(
                timestamp=time.time(),
                category=SandboxViolationCategory.IMPORT,
                detail="blocked os",
                plugin_id="p1",
                attempted_action="import os",
                stack_trace=None,
            ),
            SecurityEvent(
                timestamp=time.time(),
                category=SandboxViolationCategory.NETWORK,
                detail="blocked evil.com",
                plugin_id="p1",
                attempted_action="connect:evil.com",
                stack_trace=None,
            ),
        ]
        report = ViolationReport.from_events(events, plugin_id="p1")
        assert report.total_violations == 2
        assert report.by_category.get("import") == 1
        assert report.by_category.get("network") == 1
        assert len(report.by_layer["import"]) == 1

    def test_to_dict(self):
        report = ViolationReport(plugin_id="test")
        d = report.to_dict()
        assert d["plugin_id"] == "test"
        assert d["total_violations"] == 0
        assert isinstance(d["by_category"], dict)
        assert isinstance(d["by_layer"], dict)

    def test_to_json(self):
        report = ViolationReport(plugin_id="test")
        j = report.to_json()
        parsed = json.loads(j)
        assert parsed["plugin_id"] == "test"

    def test_summary(self):
        events = [
            SecurityEvent(
                timestamp=time.time(),
                category=SandboxViolationCategory.IMPORT,
                detail="d1",
                plugin_id="p1",
                attempted_action="a1",
                stack_trace=None,
            ),
            SecurityEvent(
                timestamp=time.time(),
                category=SandboxViolationCategory.IMPORT,
                detail="d2",
                plugin_id="p1",
                attempted_action="a2",
                stack_trace=None,
            ),
        ]
        report = ViolationReport.from_events(events, plugin_id="p1")
        s = report.summary()
        assert "Total violations: 2" in s
        assert "import: 2" in s

    def test_summary_empty(self):
        report = ViolationReport(plugin_id="empty")
        s = report.summary()
        assert "Total violations: 0" in s
        assert "By category:" not in s

    def test_empty_by_layer(self):
        report = ViolationReport()
        assert report.by_layer["import"] == []
        assert report.by_layer["network"] == []
        assert report.by_layer["resource"] == []
        assert report.by_layer["filesystem"] == []
        assert report.by_layer["introspection"] == []


# ─── SandboxMetricsCollector tests ─────────────────────────────────────


class TestSandboxMetricsCollector:
    def test_record_evaluation(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p1", 100.0, 5)
        m = mc.get_plugin_metrics("p1")
        assert m is not None
        assert m["total_evaluations"] == 1
        assert m["total_signals_emitted"] == 5

    def test_record_evaluation_with_error(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p2", 50.0, 0, error="timeout")
        m = mc.get_plugin_metrics("p2")
        assert m["errors"] == 1
        assert m["last_error"] == "timeout"

    def test_avg_evaluation_ms(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p3", 100.0, 3)
        mc.record_evaluation("p3", 200.0, 1)
        m = mc.get_plugin_metrics("p3")
        assert m["avg_evaluation_ms"] == 150.0

    def test_get_all_metrics(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("a", 10.0, 1)
        mc.record_evaluation("b", 20.0, 2)
        all_m = mc.get_all_metrics()
        assert "a" in all_m
        assert "b" in all_m

    def test_reset_specific_plugin(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p", 10.0, 1)
        mc.record_evaluation("q", 10.0, 1)
        mc.reset("p")
        assert mc.get_plugin_metrics("p") is None
        assert mc.get_plugin_metrics("q") is not None

    def test_reset_all(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p", 10.0, 1)
        mc.reset()
        assert mc.get_plugin_metrics("p") is None

    def test_record_violation(self):
        mc = SandboxMetricsCollector()
        mc.record_violation("p1")
        mc.record_violation("p1")
        m = mc.get_plugin_metrics("p1")
        assert m["security_violations"] == 2

    def test_get_plugin_metrics_nonexistent(self):
        mc = SandboxMetricsCollector()
        assert mc.get_plugin_metrics("nonexistent") is None


# ─── PluginMetrics tests ───────────────────────────────────────────────


class TestPluginMetrics:
    def test_to_dict(self):
        pm = PluginMetrics(plugin_id="test-pm")
        pm.total_evaluations = 5
        pm.total_cpu_time_ms = 123.456
        d = pm.to_dict()
        assert d["plugin_id"] == "test-pm"
        assert d["total_evaluations"] == 5
        assert d["total_cpu_time_ms"] == 123.46


# ─── PluginSandboxExecutor edge cases ──────────────────────────────────


class TestPluginSandboxExecutorEdgeCases:
    def test_from_factory_creates_placeholder(self):
        policy = SandboxPolicy(plugin_id="exec-test")

        def factory():
            return SimpleNamespace(name="real", version="1.0", on_bar=lambda s, p: [])

        executor = PluginSandboxExecutor.from_factory(factory, policy)
        assert executor.strategy.name == "real"

    def test_get_health(self):
        policy = SandboxPolicy(
            plugin_id="health-test",
            trust_level="untrusted",
        )
        executor = PluginSandboxExecutor(
            strategy=SimpleNamespace(name="s1", version="0.1"),
            policy=policy,
        )
        health = executor.get_health()
        assert health["strategy_name"] == "s1"
        assert health["plugin_id"] == "health-test"
        assert health["trust_level"] == "untrusted"

    def test_cleanup(self):
        policy = SandboxPolicy(plugin_id="cleanup-test")
        executor = PluginSandboxExecutor(
            strategy=SimpleNamespace(name="s1", version="0.1"),
            policy=policy,
        )
        executor.cleanup()

    def test_convert_signals_valid(self):
        policy = SandboxPolicy(plugin_id="sig-test")
        executor = PluginSandboxExecutor(
            strategy=SimpleNamespace(name="s1", version="0.1"),
            policy=policy,
        )
        sig = Signal(strategy_id="s1", symbol="AAPL", side="buy", quantity=10)
        result = executor._convert_signals([sig])
        assert len(result) == 1
        assert result[0].symbol == "AAPL"

    def test_convert_signals_invalid_skipped(self):
        policy = SandboxPolicy(plugin_id="sig-skip")
        executor = PluginSandboxExecutor(
            strategy=SimpleNamespace(name="s1", version="0.1"),
            policy=policy,
        )
        result = executor._convert_signals(["not_a_signal", 42])
        assert len(result) == 0

    def test_convert_signals_fills_strategy_id(self):
        policy = SandboxPolicy(plugin_id="sig-fill")
        executor = PluginSandboxExecutor(
            strategy=SimpleNamespace(name="mystrat", version="0.1"),
            policy=policy,
        )
        sig = Signal(strategy_id="", symbol="AAPL", side="buy", quantity=10)
        result = executor._convert_signals([sig])
        assert result[0].strategy_id == "mystrat"

    def test_convert_signals_preserves_existing_strategy_id(self):
        policy = SandboxPolicy(plugin_id="sig-preserve")
        executor = PluginSandboxExecutor(
            strategy=SimpleNamespace(name="mystrat", version="0.1"),
            policy=policy,
        )
        sig = Signal(strategy_id="other", symbol="AAPL", side="buy", quantity=10)
        result = executor._convert_signals([sig])
        assert result[0].strategy_id == "other"


# ─── StrategySandbox (from _sandbox.py) tests ─────────────────────────


class TestStrategySandbox:
    def test_init_no_network(self):
        m = _make_manifest()
        sb = StrategySandbox(
            SimpleNamespace(name="s", version="1.0", on_bar=lambda s, p: []),
            m,
        )
        assert sb._http_client is None

    def test_init_with_network(self):
        m = _make_manifest(
            network={"allowed_endpoints": ["api.example.com"]},
        )
        sb = StrategySandbox(
            SimpleNamespace(name="s", version="1.0", on_bar=lambda s, p: []),
            m,
        )
        assert sb._http_client is not None

    def test_get_health_initial(self):
        m = _make_manifest()
        sb = StrategySandbox(
            SimpleNamespace(name="s", version="1.0", on_bar=lambda s, p: []),
            m,
        )
        h = sb.get_health()
        assert h["strategy_name"] == "s"
        assert h["evaluations"] == 0
        assert h["errors"] == 0

    def test_work_dir(self):
        m = _make_manifest()
        sb = StrategySandbox(
            SimpleNamespace(name="s", version="1.0", on_bar=lambda s, p: []),
            m,
        )
        assert sb._work_dir is not None
        sb.cleanup()

    def test_parse_memory_static(self):
        assert StrategySandbox._parse_memory("2GB") == 2 * 1024**3
        assert StrategySandbox._parse_memory("512MB") == 512 * 1024**2

    def test_update_metrics(self):
        m = _make_manifest()
        sb = StrategySandbox(
            SimpleNamespace(name="s", version="1.0", on_bar=lambda s, p: []),
            m,
        )
        sb._update_metrics(100.0, 3)
        assert sb.metrics.total_evaluations == 1
        assert sb.metrics.total_signals_emitted == 3
        assert sb.metrics.avg_evaluation_ms == 100.0

        sb._update_metrics(200.0, 2)
        assert sb.metrics.total_evaluations == 2
        assert sb.metrics.avg_evaluation_ms == 150.0

    def test_convert_signals_with_invalid(self):
        m = _make_manifest()
        sb = StrategySandbox(
            SimpleNamespace(name="s", version="1.0", on_bar=lambda s, p: []),
            m,
        )
        sig = Signal(strategy_id="s", symbol="AAPL", side="buy", quantity=10)
        result = sb._convert_signals([sig, "bad", 42])
        assert len(result) == 1
        assert result[0].symbol == "AAPL"

    def test_convert_signals_fills_strategy_id(self):
        m = _make_manifest()
        sb = StrategySandbox(
            SimpleNamespace(name="mystrat", version="1.0", on_bar=lambda s, p: []),
            m,
        )
        sig = Signal(strategy_id="", symbol="AAPL", side="buy", quantity=10)
        result = sb._convert_signals([sig])
        assert result[0].strategy_id == "mystrat"

    def test_cleanup(self):
        m = _make_manifest()
        sb = StrategySandbox(
            SimpleNamespace(name="s", version="1.0", on_bar=lambda s, p: []),
            m,
        )
        sb.cleanup()


# ─── Cross-layer integration tests ─────────────────────────────────────


class TestCrossLayerIntegration:
    def test_violations_collected_from_all_layers(self):
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "cross-viol")
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            with pytest.raises(PermissionError):
                builtins.eval("1+1")  # noqa: S307
            with pytest.raises(PermissionError):
                builtins.getattr(object(), "__class__")  # noqa: B009
        finally:
            ctx.deactivate()
        assert ctx.event_logger.event_count >= 2

    def test_full_lifecycle_with_manifest(self):
        m = _make_manifest(trust_level="untrusted")
        policy = SandboxPolicy.from_manifest(m)
        assert policy.trust_level == "untrusted"
        assert len(policy.import_policy.blocked_modules) > 0

        ctx = SandboxContext(policy)
        ctx.activate()
        assert ctx.is_active
        ctx.deactivate()
        assert not ctx.is_active

    def test_policy_from_manifest_all_trust_levels(self):
        for tl in ["untrusted", "trusted_limited", "trusted_full"]:
            m = _make_manifest(trust_level=tl)
            policy = SandboxPolicy.from_manifest(m)
            assert policy.trust_level == tl


# ─── __init__.py lazy exports ──────────────────────────────────────────


class TestLazyExports:
    def test_known_export(self):
        from engine.plugins.sandbox import SandboxPolicy

        assert SandboxPolicy is not None

    def test_unknown_attribute_raises(self):
        import engine.plugins.sandbox as sandbox_mod

        with pytest.raises(AttributeError, match="has no attribute"):
            sandbox_mod.__getattr__("nonexistent_attr_xyz")


# ─── Boundary/edge value tests ─────────────────────────────────────────


class TestBoundaryValues:
    def test_parse_memory_zero(self):
        assert _parse_memory("0MB") == 0

    def test_parse_memory_very_large(self):
        val = _parse_memory("1024GB")
        assert val == 1024 * 1024**3

    def test_resource_policy_defaults(self):
        rp = ResourcePolicy()
        assert rp.max_cpu_seconds == 30.0
        assert rp.max_memory_bytes == 512 * 1024 * 1024
        assert rp.max_file_descriptors == 64
        assert rp.max_threads == 1
        assert rp.wall_time_seconds == 60.0

    def test_filesystem_policy_defaults(self):
        fp = FilesystemPolicy()
        assert fp.read_only_paths == []
        assert fp.read_write_paths == []
        assert fp.virtual_root is None
        assert fp.block_symlinks is True
        assert fp.block_absolute_paths is True

    def test_introspection_policy_defaults(self):
        ip = IntrospectionPolicy()
        assert "eval" in ip.blocked_builtins
        assert "__subclasses__" in ip.blocked_attributes
        assert ip.blocked_dunder_access is True
        assert ip.block_gc is True
        assert ip.block_inspect is True
        assert ip.block_frame_access is True

    def test_network_policy_defaults(self):
        np = NetworkPolicy()
        assert np.allowed_endpoints == []
        assert np.allowed_cidrs == []
        assert np.allowed_ports == set()
        assert np.block_dns is True
        assert np.allowed_dns_servers == []

    def test_import_policy_defaults(self):
        ip = ImportPolicy()
        assert ip.allowed_modules == set()
        assert ip.blocked_modules == set()
        assert ip.blocked_categories == {}

    def test_cpu_timer_zero_seconds(self):
        t = _CPUTimer(0.0)
        t.start()
        with pytest.raises(ResourceExhausted):
            t.check()
        t.stop()

    def test_cpu_timer_negative_seconds(self):
        t = _CPUTimer(-1.0)
        t._start_time = time.monotonic()
        with pytest.raises(ResourceExhausted):
            t.check()
