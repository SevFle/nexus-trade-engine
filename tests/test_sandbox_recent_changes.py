"""Comprehensive tests for recently changed sandbox code.

Covers:
- ResourceLimiter (timers, thread counting, memory parsing, violation tracking)
- NetworkGuard (host/CIDR/port filtering, socket/httpx patching)
- IntrospectionGuard (blocked attrs, builtins, dir filtering)
- SandboxContext (activation, trust validation, violation collection)
- SandboxAdminAPI (CRUD, policy updates, violation reports, metrics)
- ViolationReport (generation, serialization)
"""

from __future__ import annotations

import builtins
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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
    ImportViolation,
    IntrospectionViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers import (
    IntrospectionGuard,
    NetworkGuard,
    ResourceLimiter,
)
from engine.plugins.sandbox.monitoring.admin_api import (
    PolicySnapshot,
    PolicyUpdate,
    SandboxAdminAPI,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport


def _make_policy(**overrides: Any) -> SandboxPolicy:
    defaults = {
        "plugin_id": "test-plugin",
        "trust_level": "untrusted",
        "import_policy": ImportPolicy(
            blocked_modules={"os", "subprocess", "sys", "ctypes", "signal",
                             "socket", "http", "urllib", "shutil", "pathlib"},
        ),
        "network_policy": NetworkPolicy(
            allowed_endpoints=["api.example.com"],
            allowed_cidrs=["10.0.0.0/8"],
            allowed_ports={443},
        ),
        "resource_policy": ResourcePolicy(
            max_cpu_seconds=5.0,
            max_memory_bytes=256 * 1024 * 1024,
            max_file_descriptors=32,
            max_threads=2,
            wall_time_seconds=10.0,
        ),
        "filesystem_policy": FilesystemPolicy(
            read_only_paths=["/var/sandbox/readonly"],
            read_write_paths=[],
        ),
        "introspection_policy": IntrospectionPolicy(),
    }
    defaults.update(overrides)
    return SandboxPolicy(**defaults)


# ---------------------------------------------------------------------------
# ResourceLimiter unit tests
# ---------------------------------------------------------------------------
class TestCPUTimer:
    def test_timer_starts_and_checks_within_limit(self):
        from engine.plugins.sandbox.layers.resource_limiter import _CPUTimer
        t = _CPUTimer(10.0, plugin_id="p1")
        t.start()
        t.check()
        assert not t.expired
        t.stop()

    def test_timer_expires_after_timeout(self):
        from engine.plugins.sandbox.layers.resource_limiter import _CPUTimer
        t = _CPUTimer(0.01, plugin_id="p1")
        t.start()
        time.sleep(0.05)
        assert t.expired
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "cpu_time"
        assert exc_info.value.plugin_id == "p1"
        t.stop()

    def test_timer_not_expired_when_stopped(self):
        from engine.plugins.sandbox.layers.resource_limiter import _CPUTimer
        t = _CPUTimer(0.01, plugin_id="p1")
        t.start()
        t.stop()
        time.sleep(0.05)
        assert not t.expired

    def test_elapsed_returns_positive(self):
        from engine.plugins.sandbox.layers.resource_limiter import _CPUTimer
        t = _CPUTimer(60.0)
        t.start()
        time.sleep(0.05)
        assert t.elapsed > 0
        t.stop()

    def test_expired_property(self):
        from engine.plugins.sandbox.layers.resource_limiter import _CPUTimer
        t = _CPUTimer(60.0)
        assert not t.expired
        t.start()
        assert not t.expired
        t.stop()


class TestWallTimer:
    def test_wall_timer_within_limit(self):
        from engine.plugins.sandbox.layers.resource_limiter import _WallTimer
        t = _WallTimer(10.0, plugin_id="p1")
        t.start()
        t.check()
        assert not t.expired
        t.stop()

    def test_wall_timer_expires(self):
        from engine.plugins.sandbox.layers.resource_limiter import _WallTimer
        t = _WallTimer(0.01, plugin_id="p1")
        t.start()
        time.sleep(0.05)
        assert t.expired
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "wall_time"
        t.stop()


class TestResourceLimiter:
    def test_install_uninstall_cycle(self):
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="p1")
        limiter.install()
        assert limiter._installed
        limiter.uninstall()
        assert not limiter._installed

    def test_double_install_is_noop(self):
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.install()
        limiter.uninstall()

    def test_double_uninstall_is_noop(self):
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter.uninstall()

    def test_check_cpu_timer_raises_on_expiry(self):
        policy = ResourcePolicy(max_cpu_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="p1")
        limiter.install()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted):
            limiter.check_cpu_timer()
        assert len(limiter.get_violations()) == 1
        limiter.uninstall()

    def test_check_wall_timer_raises_on_expiry(self):
        policy = ResourcePolicy(wall_time_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="p1")
        limiter.install()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted):
            limiter.check_wall_timer()
        assert len(limiter.get_violations()) == 1
        limiter.uninstall()

    def test_cpu_elapsed_returns_zero_when_not_installed(self):
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        assert limiter.cpu_elapsed == 0.0

    def test_thread_limit_enforced(self):
        policy = ResourcePolicy(max_threads=1)
        limiter = ResourceLimiter(policy, plugin_id="p1")
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.increment_thread()
        assert exc_info.value.resource_type == "threads"
        assert len(limiter.get_violations()) == 1

    def test_decrement_thread_floor(self):
        policy = ResourcePolicy(max_threads=5)
        limiter = ResourceLimiter(policy)
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_violation_tracking(self):
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="p1")
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        assert len(limiter.get_violations()) == 1
        limiter.clear_violations()
        assert limiter.get_violations() == []

    @pytest.mark.parametrize(
        ("input_str", "expected"),
        [
            ("1GB", 1024 ** 3),
            ("512MB", 512 * 1024 ** 2),
            ("256KB", 256 * 1024),
            ("1024B", 1024),
            ("1024", 1024),
            ("  2gb  ", 2 * 1024 ** 3),
            ("1.5GB", int(1.5 * 1024 ** 3)),
        ],
    )
    def test_parse_memory(self, input_str: str, expected: int):
        assert ResourceLimiter.parse_memory(input_str) == expected


# ---------------------------------------------------------------------------
# NetworkGuard unit tests
# ---------------------------------------------------------------------------
class TestNetworkGuardHostValidation:
    def test_allowed_endpoint(self):
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy, plugin_id="p1")
        assert guard._is_host_allowed("api.example.com")

    def test_subdomain_of_allowed(self):
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("sub.example.com")

    def test_blocked_endpoint(self):
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy)
        assert not guard._is_host_allowed("evil.com")

    def test_cidr_allowed(self):
        policy = NetworkPolicy(allowed_cidrs=["10.0.0.0/8"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("10.1.2.3")

    def test_private_ip_blocked_without_cidr(self):
        policy = NetworkPolicy(allowed_endpoints=[], allowed_cidrs=[])
        guard = NetworkGuard(policy)
        assert not guard._is_host_allowed("127.0.0.1")
        assert not guard._is_host_allowed("192.168.1.1")
        assert not guard._is_host_allowed("10.0.0.1")

    def test_port_allowed_when_no_restriction(self):
        policy = NetworkPolicy(allowed_ports=set())
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(80)
        assert guard._is_port_allowed(443)

    def test_port_allowed_in_whitelist(self):
        policy = NetworkPolicy(allowed_ports={443, 80})
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(443)
        assert not guard._is_port_allowed(8080)

    def test_port_none_is_allowed(self):
        policy = NetworkPolicy(allowed_ports={443})
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(None)


class TestNetworkGuardInstall:
    def test_install_patches_and_uninstall_restores(self):
        import socket

        import httpx

        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy)
        orig_socket = socket.create_connection
        orig_httpx = httpx.AsyncClient.send
        guard.install()
        assert socket.create_connection is not orig_socket
        assert httpx.AsyncClient.send is not orig_httpx
        guard.uninstall()
        assert socket.create_connection is orig_socket
        assert httpx.AsyncClient.send is orig_httpx

    def test_install_idempotent(self):
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        guard.install()
        guard.install()
        guard.uninstall()

    def test_uninstall_idempotent(self):
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        guard.uninstall()

    def test_socket_connection_blocked(self):
        import socket

        policy = NetworkPolicy(allowed_endpoints=[], allowed_cidrs=[])
        guard = NetworkGuard(policy, plugin_id="p1")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                socket.create_connection(("evil.com", 80))
            assert len(guard.get_violations()) == 1
            assert guard.get_violations()[0].host == "evil.com"
        finally:
            guard.uninstall()

    def test_socket_connection_allowed(self):
        import socket

        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy, plugin_id="p1")
        guard.install()
        try:
            with patch.object(guard, "_original_socket_create_connection", return_value=MagicMock()):
                result = socket.create_connection(("api.example.com", 443))
                assert result is not None
                assert guard.get_violations() == []
        finally:
            guard.uninstall()

    def test_violation_tracking(self):
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        guard.get_violations()
        guard.clear_violations()
        assert guard.get_violations() == []


class TestPrivateIPDetection:
    def test_loopback(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("127.0.0.1")

    def test_private_10(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("10.0.0.1")

    def test_private_172(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("172.16.0.1")

    def test_private_192(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("192.168.1.1")

    def test_link_local(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("169.254.1.1")

    def test_ipv6_loopback(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("::1")

    def test_ipv6_unique_local(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("fc00::1")

    def test_public_ip(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert not _is_private_ip("8.8.8.8")

    def test_invalid_host(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert not _is_private_ip("not-an-ip")

    def test_zero_network(self):
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("0.0.0.1")


class TestParseCIDR:
    def test_valid_cidrs(self):
        from engine.plugins.sandbox.layers.network_guard import _parse_cidr_networks
        nets = _parse_cidr_networks(["10.0.0.0/8", "192.168.0.0/16"])
        assert len(nets) == 2

    def test_invalid_cidrs_skipped(self):
        from engine.plugins.sandbox.layers.network_guard import _parse_cidr_networks
        nets = _parse_cidr_networks(["not-a-cidr", "10.0.0.0/8"])
        assert len(nets) == 1

    def test_empty_list(self):
        from engine.plugins.sandbox.layers.network_guard import _parse_cidr_networks
        assert _parse_cidr_networks([]) == []


# ---------------------------------------------------------------------------
# IntrospectionGuard unit tests
# ---------------------------------------------------------------------------
class TestIntrospectionGuard:
    def test_blocked_attribute_detection(self):
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="p1")
        assert guard._is_blocked_attr("__subclasses__")
        assert guard._is_blocked_attr("__globals__")
        assert guard._is_blocked_attr("__code__")

    def test_custom_blocked_attribute(self):
        policy = IntrospectionPolicy(blocked_attributes={"__mysecret__"})
        guard = IntrospectionGuard(policy)
        assert guard._is_blocked_attr("__mysecret__")

    def test_frame_attrs_blocked(self):
        policy = IntrospectionPolicy(block_frame_access=True)
        guard = IntrospectionGuard(policy)
        assert guard._is_blocked_attr("tb_frame")
        assert guard._is_blocked_attr("f_globals")

    def test_frame_attrs_not_blocked_when_disabled(self):
        policy = IntrospectionPolicy(block_frame_access=False)
        guard = IntrospectionGuard(policy)
        assert not guard._is_blocked_attr("tb_frame")

    def test_normal_attr_not_blocked(self):
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        assert not guard._is_blocked_attr("name")
        assert not guard._is_blocked_attr("value")

    def test_install_patches_builtins(self):
        orig_getattr = builtins.getattr
        orig_setattr = builtins.setattr
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.install()
        try:
            assert builtins.getattr is not orig_getattr
            assert builtins.setattr is not orig_setattr
        finally:
            guard.uninstall()
            assert builtins.getattr is orig_getattr
            assert builtins.setattr is orig_setattr

    def test_install_uninstall_idempotent(self):
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.install()
        guard.install()
        guard.uninstall()
        guard.uninstall()

    def test_blocked_getattr_raises(self):
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="p1")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr(str, "__subclasses__")  # noqa: B009
            assert len(guard.get_violations()) == 1
            assert guard.get_violations()[0].attribute == "__subclasses__"
        finally:
            guard.uninstall()

    def test_blocked_builtin_raises(self):
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="p1")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                eval("1+1")  # noqa: S307
            assert len(guard.get_violations()) >= 1
        finally:
            guard.uninstall()

    def test_clear_violations(self):
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy)
        guard.clear_violations()
        assert guard.get_violations() == []


# ---------------------------------------------------------------------------
# SandboxContext tests
# ---------------------------------------------------------------------------
class TestSandboxContext:
    def test_context_activation_deactivation(self):
        policy = _make_policy()
        ctx = SandboxContext(policy)
        assert not ctx.is_active
        ctx.activate()
        assert ctx.is_active
        ctx.deactivate()
        assert not ctx.is_active

    def test_double_activate_is_noop(self):
        policy = _make_policy()
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.activate()
        assert ctx.is_active
        ctx.deactivate()

    def test_double_deactivate_is_noop(self):
        policy = _make_policy()
        ctx = SandboxContext(policy)
        ctx.deactivate()

    def test_context_manager_protocol(self):
        policy = _make_policy()
        with SandboxContext(policy) as ctx:
            assert ctx.is_active
        assert not ctx.is_active

    def test_trust_level_resolved(self):
        from engine.plugins.trust_levels import TrustLevel
        policy = _make_policy(trust_level="untrusted")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED

    def test_trust_level_invalid_falls_back(self):
        from engine.plugins.trust_levels import TrustLevel
        policy = _make_policy(trust_level="invalid_level")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED

    def test_validate_trust_level_untrusted_with_enough_blocks(self):
        policy = _make_policy(
            trust_level="untrusted",
            import_policy=ImportPolicy(
                blocked_modules={f"mod{i}" for i in range(15)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=[]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_validate_trust_level_untrusted_too_few_blocks(self):
        policy = _make_policy(
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os"}),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_validate_trust_level_untrusted_cpu_too_high(self):
        policy = _make_policy(
            trust_level="untrusted",
            import_policy=ImportPolicy(
                blocked_modules={f"mod{i}" for i in range(15)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_validate_trust_level_untrusted_rw_paths(self):
        policy = _make_policy(
            trust_level="untrusted",
            import_policy=ImportPolicy(
                blocked_modules={f"mod{i}" for i in range(15)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/var/sandbox/write"]),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_validate_trust_level_limited_valid(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"mod{i}" for i in range(10)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_validate_trust_level_limited_too_few_blocks(self):
        policy = _make_policy(
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={"os"}),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_validate_trust_level_trusted_full_always_valid(self):
        policy = _make_policy(
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_activate_with_failed_installation_rolls_back(self):
        policy = _make_policy()
        ctx = SandboxContext(policy)
        original_install = ctx._network_layer.install

        def bad_install():
            raise RuntimeError("install failed")

        ctx._network_layer.install = bad_install
        with pytest.raises(RuntimeError, match="install failed"):
            ctx.activate()
        assert not ctx.is_active
        ctx._network_layer.install = original_install

    def test_cleanup_deactivates_and_cleans(self):
        policy = _make_policy()
        ctx = SandboxContext(policy)
        ctx.activate()
        assert ctx.is_active
        ctx.cleanup()
        assert not ctx.is_active

    def test_event_logger_exposed(self):
        policy = _make_policy()
        ctx = SandboxContext(policy)
        assert isinstance(ctx.event_logger, SecurityEventLogger)

    def test_policy_property(self):
        policy = _make_policy()
        ctx = SandboxContext(policy)
        assert ctx.policy is policy

    def test_work_dir_property(self):
        policy = _make_policy()
        ctx = SandboxContext(policy)
        assert isinstance(ctx.work_dir, str)
        assert len(ctx.work_dir) > 0


# ---------------------------------------------------------------------------
# SandboxAdminAPI tests
# ---------------------------------------------------------------------------
class TestPolicySnapshot:
    def test_from_policy(self):
        policy = _make_policy()
        snap = PolicySnapshot.from_policy(policy)
        assert snap.plugin_id == "test-plugin"
        assert snap.trust_level == "untrusted"
        assert isinstance(snap.blocked_modules, set)
        assert isinstance(snap.allowed_endpoints, list)
        assert snap.max_cpu_seconds > 0
        assert snap.max_memory_bytes > 0

    def test_snapshot_has_timestamp(self):
        policy = _make_policy()
        snap = PolicySnapshot.from_policy(policy)
        assert snap.snapshot_at > 0


class TestPolicyUpdate:
    def test_default_fields(self):
        u = PolicyUpdate(plugin_id="p1", updated_fields={"a": 1})
        assert u.plugin_id == "p1"
        assert u.applied_at > 0
        assert u.previous_values == {}


class TestSandboxAdminAPI:
    def _make_admin(self) -> tuple[SandboxAdminAPI, SandboxMetricsCollector]:
        mc = SandboxMetricsCollector()
        return SandboxAdminAPI(mc), mc

    def test_register_and_unregister_context(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        plugins = admin.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["plugin_id"] == "test-plugin"
        assert plugins[0]["trust_level"] == "untrusted"
        admin.unregister_context("test-plugin")
        assert admin.list_plugins() == []

    def test_unregister_nonexistent_is_noop(self):
        admin, _ = self._make_admin()
        admin.unregister_context("nonexistent")

    def test_get_policy_existing(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        result = admin.get_policy("test-plugin")
        assert result is not None
        assert result["plugin_id"] == "test-plugin"
        assert "import_policy" in result
        assert "network_policy" in result
        assert "resource_policy" in result
        assert "filesystem_policy" in result

    def test_get_policy_nonexistent(self):
        admin, _ = self._make_admin()
        assert admin.get_policy("nope") is None

    def test_update_policy_inactive_context(self):
        admin, _ = self._make_admin()
        policy = _make_policy(resource_policy=ResourcePolicy(max_cpu_seconds=30))
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        update = admin.update_policy("test-plugin", {"max_cpu_seconds": 60})
        assert update is not None
        assert update.updated_fields["max_cpu_seconds"] == 60
        assert update.previous_values["max_cpu_seconds"] == 30
        assert policy.resource_policy.max_cpu_seconds == 60

    def test_update_policy_active_context_rejected(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        ctx.activate()
        result = admin.update_policy("test-plugin", {"max_cpu_seconds": 99})
        assert result is None
        ctx.deactivate()

    def test_update_policy_unknown_plugin(self):
        admin, _ = self._make_admin()
        assert admin.update_policy("nope", {"max_cpu_seconds": 99}) is None

    def test_update_policy_multiple_fields(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        update = admin.update_policy("test-plugin", {
            "max_cpu_seconds": 100,
            "max_threads": 8,
            "block_dns": False,
        })
        assert update is not None
        assert "max_cpu_seconds" in update.updated_fields
        assert "max_threads" in update.updated_fields
        assert "block_dns" in update.updated_fields
        assert policy.resource_policy.max_threads == 8
        assert policy.network_policy.block_dns is False

    def test_update_policy_endpoints(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        update = admin.update_policy(
            "test-plugin",
            {"allowed_endpoints": ["new.example.com"]},
        )
        assert update is not None
        assert "allowed_endpoints" in update.updated_fields
        assert policy.network_policy.allowed_endpoints == ["new.example.com"]

    def test_update_policy_blocked_modules(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        update = admin.update_policy(
            "test-plugin",
            {"blocked_modules": {"os", "sys"}},
        )
        assert update is not None
        assert policy.import_policy.blocked_modules == {"os", "sys"}

    def test_update_policy_filesystem_paths(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        update = admin.update_policy(
            "test-plugin",
            {
                "read_only_paths": ["/data"],
                "read_write_paths": ["/var/sandbox/writable"],
            },
        )
        assert update is not None
        assert policy.filesystem_policy.read_only_paths == ["/data"]
        assert policy.filesystem_policy.read_write_paths == ["/var/sandbox/writable"]

    def test_get_policy_history(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        admin.update_policy("test-plugin", {"max_cpu_seconds": 10})
        admin.update_policy("test-plugin", {"max_cpu_seconds": 20})
        history = admin.get_policy_history("test-plugin")
        assert len(history) == 2
        assert history[0]["updated_fields"]["max_cpu_seconds"] == 10
        assert history[1]["updated_fields"]["max_cpu_seconds"] == 20

    def test_get_policy_history_limit(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        for i in range(10):
            admin.update_policy("test-plugin", {"max_cpu_seconds": float(i)})
        history = admin.get_policy_history("test-plugin", limit=3)
        assert len(history) == 3

    def test_get_policy_history_empty(self):
        admin, _ = self._make_admin()
        assert admin.get_policy_history("nope") == []

    def test_get_security_events_single_plugin(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        events = admin.get_security_events(plugin_id="test-plugin")
        assert isinstance(events, list)

    def test_get_security_events_all_plugins(self):
        admin, _ = self._make_admin()
        policy1 = _make_policy()
        ctx1 = SandboxContext(policy1)
        admin.register_context(ctx1)
        events = admin.get_security_events()
        assert isinstance(events, list)

    def test_get_security_events_with_since(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        events = admin.get_security_events(since=time.time() - 100)
        assert isinstance(events, list)

    def test_get_security_events_unknown_plugin(self):
        admin, _ = self._make_admin()
        events = admin.get_security_events(plugin_id="nope")
        assert events == []

    def test_get_plugin_metrics(self):
        admin, mc = self._make_admin()
        mc.record_evaluation("test-plugin", 100.0, 5)
        result = admin.get_plugin_metrics("test-plugin")
        assert result is not None
        assert result["total_evaluations"] == 1
        assert result["total_signals_emitted"] == 5

    def test_get_plugin_metrics_nonexistent(self):
        admin, _ = self._make_admin()
        assert admin.get_plugin_metrics("nope") is None

    def test_get_all_metrics(self):
        admin, mc = self._make_admin()
        mc.record_evaluation("p1", 100.0, 3)
        mc.record_evaluation("p2", 200.0, 1)
        result = admin.get_all_metrics()
        assert "p1" in result
        assert "p2" in result

    def test_get_violation_report(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        report = admin.get_violation_report(plugin_id="test-plugin")
        assert isinstance(report, ViolationReport)
        assert report.plugin_id == "test-plugin"

    def test_get_violation_report_all(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        report = admin.get_violation_report()
        assert isinstance(report, ViolationReport)
        assert report.plugin_id is None

    def test_list_plugins(self):
        admin, _ = self._make_admin()
        policy = _make_policy()
        ctx = SandboxContext(policy)
        admin.register_context(ctx)
        plugins = admin.list_plugins()
        assert len(plugins) == 1
        p = plugins[0]
        assert "plugin_id" in p
        assert "trust_level" in p
        assert "is_active" in p
        assert "work_dir" in p
        assert "violation_count" in p

    def test_events_to_dicts(self):
        result = SandboxAdminAPI._events_to_dicts([])
        assert result == []


# ---------------------------------------------------------------------------
# ViolationReport tests
# ---------------------------------------------------------------------------
class TestViolationReport:
    def test_empty_report(self):
        report = ViolationReport(plugin_id="p1", trust_level="untrusted")
        assert report.total_violations == 0
        assert report.by_category == {}

    def test_from_events(self):
        logger = SecurityEventLogger(plugin_id="p1")
        violation = NetworkViolation("evil.com", port=80, plugin_id="p1")
        logger.log_violation(violation)
        events = logger.get_events()
        report = ViolationReport.from_events(events, plugin_id="p1", trust_level="untrusted")
        assert report.total_violations == 1
        assert "network" in report.by_category
        assert report.by_category["network"] == 1

    def test_to_dict(self):
        report = ViolationReport(plugin_id="p1")
        d = report.to_dict()
        assert "plugin_id" in d
        assert "total_violations" in d
        assert "by_category" in d
        assert "by_layer" in d

    def test_to_json(self):
        report = ViolationReport()
        j = report.to_json()
        assert isinstance(j, str)
        assert "total_violations" in j

    def test_summary(self):
        report = ViolationReport(plugin_id="p1", trust_level="untrusted")
        s = report.summary()
        assert "p1" in s
        assert "untrusted" in s

    def test_summary_with_categories(self):
        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        logger.log_violation(ImportViolation("os", plugin_id="p1"))
        events = logger.get_events()
        report = ViolationReport.from_events(events, plugin_id="p1")
        s = report.summary()
        assert "network" in s
        assert "import" in s


# ---------------------------------------------------------------------------
# Violation classes tests
# ---------------------------------------------------------------------------
class TestViolations:
    def test_import_violation(self):
        v = ImportViolation("os", plugin_id="p1")
        assert v.module_name == "os"
        assert v.category == SandboxViolationCategory.IMPORT
        assert v.plugin_id == "p1"
        assert "os" in v.detail

    def test_network_violation(self):
        v = NetworkViolation("evil.com", port=80, plugin_id="p1")
        assert v.host == "evil.com"
        assert v.port == 80
        assert v.category == SandboxViolationCategory.NETWORK
        assert "evil.com" in v.detail

    def test_network_violation_no_port(self):
        v = NetworkViolation("evil.com")
        assert v.port is None
        assert "evil.com" in v.detail

    def test_resource_exhausted(self):
        v = ResourceExhausted("cpu_time", 10, 15, plugin_id="p1")
        assert v.resource_type == "cpu_time"
        assert v.limit == 10
        assert v.current == 15
        assert v.category == SandboxViolationCategory.RESOURCE

    def test_introspection_violation(self):
        v = IntrospectionViolation("__subclasses__", plugin_id="p1")
        assert v.attribute == "__subclasses__"
        assert v.category == SandboxViolationCategory.INTROSPECTION

    def test_to_dict(self):
        v = ImportViolation("os", plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "import"
        assert d["plugin_id"] == "p1"
        assert d["attempted_action"] == "import os"


# ---------------------------------------------------------------------------
# SecurityEventLogger tests
# ---------------------------------------------------------------------------
class TestSecurityEventLogger:
    def test_log_violation(self):
        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        assert logger.event_count == 1
        events = logger.get_events()
        assert events[0].category == SandboxViolationCategory.NETWORK
        assert events[0].stack_trace is not None

    def test_log_event(self):
        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_event(
            category=SandboxViolationCategory.INTROSPECTION,
            detail="test event",
            attempted_action="test_action",
        )
        assert logger.event_count == 1
        events = logger.get_events()
        assert events[0].detail == "test event"
        assert events[0].attempted_action == "test_action"

    def test_get_events_by_category(self):
        logger = SecurityEventLogger()
        logger.log_violation(NetworkViolation("evil.com"))
        logger.log_violation(ImportViolation("os"))
        net_events = logger.get_events(category=SandboxViolationCategory.NETWORK)
        assert len(net_events) == 1

    def test_get_events_since(self):
        logger = SecurityEventLogger()
        before = time.time()
        logger.log_violation(NetworkViolation("evil.com"))
        after = time.time()
        events = logger.get_events_since(before)
        assert len(events) >= 1
        events = logger.get_events_since(after + 1000)
        assert len(events) == 0

    def test_clear(self):
        logger = SecurityEventLogger()
        logger.log_violation(NetworkViolation("evil.com"))
        logger.clear()
        assert logger.event_count == 0

    def test_to_dicts(self):
        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        result = logger.to_dicts()
        assert len(result) == 1
        assert result[0]["category"] == "network"
        assert result[0]["plugin_id"] == "p1"

    def test_get_events_limit(self):
        logger = SecurityEventLogger()
        for i in range(10):
            logger.log_violation(NetworkViolation(f"host{i}.com"))
        events = logger.get_events(limit=3)
        assert len(events) == 3


# ---------------------------------------------------------------------------
# SandboxMetricsCollector tests
# ---------------------------------------------------------------------------
class TestSandboxMetricsCollector:
    def test_record_evaluation(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p1", 100.0, 5)
        metrics = mc.get_plugin_metrics("p1")
        assert metrics is not None
        assert metrics["total_evaluations"] == 1
        assert metrics["total_signals_emitted"] == 5
        assert metrics["avg_evaluation_ms"] == 100.0

    def test_record_evaluation_with_error(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p1", 50.0, 0, error="crash")
        metrics = mc.get_plugin_metrics("p1")
        assert metrics["errors"] == 1
        assert metrics["last_error"] == "crash"

    def test_record_violation(self):
        mc = SandboxMetricsCollector()
        mc.record_violation("p1")
        metrics = mc.get_plugin_metrics("p1")
        assert metrics["security_violations"] == 1

    def test_get_all_metrics(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p1", 100.0, 3)
        mc.record_evaluation("p2", 200.0, 1)
        all_m = mc.get_all_metrics()
        assert len(all_m) == 2

    def test_get_plugin_metrics_nonexistent(self):
        mc = SandboxMetricsCollector()
        assert mc.get_plugin_metrics("nope") is None

    def test_reset_specific(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p1", 100.0, 3)
        mc.reset("p1")
        assert mc.get_plugin_metrics("p1") is None

    def test_reset_all(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p1", 100.0, 3)
        mc.record_evaluation("p2", 200.0, 1)
        mc.reset()
        assert mc.get_all_metrics() == {}

    def test_avg_evaluation_updates(self):
        mc = SandboxMetricsCollector()
        mc.record_evaluation("p1", 100.0, 1)
        mc.record_evaluation("p1", 200.0, 1)
        metrics = mc.get_plugin_metrics("p1")
        assert metrics["avg_evaluation_ms"] == 150.0


# ---------------------------------------------------------------------------
# SandboxPolicy.from_trust_level tests
# ---------------------------------------------------------------------------
class TestSandboxPolicyTrustLevels:
    def test_from_trust_level_untrusted(self):
        from engine.plugins.trust_levels import TrustLevel
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p1")
        assert policy.trust_level == "untrusted"
        assert policy.resource_policy.max_cpu_seconds == 30.0
        assert "os" in policy.import_policy.blocked_modules

    def test_from_trust_level_limited(self):
        from engine.plugins.trust_levels import TrustLevel
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "p1")
        assert policy.trust_level == "trusted_limited"
        assert policy.resource_policy.max_cpu_seconds == 60.0

    def test_from_trust_level_full(self):
        from engine.plugins.trust_levels import TrustLevel
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "p1")
        assert policy.trust_level == "trusted_full"
        assert policy.resource_policy.max_cpu_seconds == 120.0

    def test_from_trust_level_custom_cpu(self):
        from engine.plugins.trust_levels import TrustLevel
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "p1", max_cpu_seconds=60.0,
        )
        assert policy.resource_policy.max_cpu_seconds == 60.0

    def test_from_trust_level_with_network(self):
        from engine.plugins.trust_levels import TrustLevel
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "p1",
            network_endpoints=["api.example.com"],
        )
        assert "api.example.com" in policy.network_policy.allowed_endpoints

    def test_trusted_policy(self):
        policy = SandboxPolicy.trusted_policy("p1")
        assert policy.trust_level == "trusted"
        assert "subprocess" in policy.import_policy.blocked_modules


# ---------------------------------------------------------------------------
# ImportPolicy tests
# ---------------------------------------------------------------------------
class TestImportPolicy:
    def test_blocked_module(self):
        policy = ImportPolicy(blocked_modules={"os"})
        assert not policy.is_allowed("os")
        assert not policy.is_allowed("os.path")

    def test_allowed_module(self):
        policy = ImportPolicy(blocked_modules={"os"}, allowed_modules={"json"})
        assert policy.is_allowed("json")

    def test_no_allowlist_blocks_non_blocked(self):
        policy = ImportPolicy(blocked_modules={"os"}, allowed_modules=set())
        assert policy.is_allowed("json")

    def test_empty_allowlist_blocks(self):
        policy = ImportPolicy(blocked_modules=set(), allowed_modules={"json"})
        assert not policy.is_allowed("os")


# ---------------------------------------------------------------------------
# NetworkPolicy tests
# ---------------------------------------------------------------------------
class TestNetworkPolicy:
    def test_host_allowed_exact(self):
        p = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert p.is_host_allowed("api.example.com")

    def test_host_allowed_subdomain(self):
        p = NetworkPolicy(allowed_endpoints=["example.com"])
        assert p.is_host_allowed("sub.example.com")

    def test_host_not_allowed(self):
        p = NetworkPolicy(allowed_endpoints=["example.com"])
        assert not p.is_host_allowed("evil.com")

    def test_empty_endpoints_blocks_all(self):
        p = NetworkPolicy(allowed_endpoints=[])
        assert not p.is_host_allowed("any.com")
