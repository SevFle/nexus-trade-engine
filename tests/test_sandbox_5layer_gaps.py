"""
Comprehensive tests covering gaps across all 5 sandbox security layers.

Layers:
  1. Import restriction      (RestrictedImporter)
  2. Introspection guard     (IntrospectionGuard, _RestrictedObject)
  3. Network guard           (NetworkGuard)
  4. Resource limiter        (ResourceLimiter, _WallTimer)
  5. Filesystem isolation    (FilesystemIsolation)

Plus integration coverage for SandboxContext orchestration and
SandboxPolicy.from_trust_level().
"""

from __future__ import annotations

import builtins
import os
import time
from typing import Any

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
    FilesystemViolation,
    ImportViolation,
    IntrospectionViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers.filesystem_isolation import FilesystemIsolation
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import (
    IntrospectionGuard,
    _RestrictedObject,
)
from engine.plugins.sandbox.layers.network_guard import NetworkGuard
from engine.plugins.sandbox.layers.resource_limiter import (
    ResourceLimiter,
    _WallTimer,
)

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def introspection_policy() -> IntrospectionPolicy:
    return IntrospectionPolicy()


@pytest.fixture
def permissive_introspection_policy() -> IntrospectionPolicy:
    return IntrospectionPolicy(
        blocked_builtins=set(),
        blocked_attributes={"__subclasses__"},
        block_frame_access=False,
    )


@pytest.fixture
def network_policy() -> NetworkPolicy:
    return NetworkPolicy(
        allowed_endpoints=["api.example.com"],
        allowed_ports={443},
        block_dns=True,
    )


@pytest.fixture
def resource_policy() -> ResourcePolicy:
    return ResourcePolicy(
        max_cpu_seconds=5,
        max_memory_bytes=1024 * 1024,
        max_file_descriptors=64,
        max_threads=2,
        wall_time_seconds=2.0,
    )


@pytest.fixture
def filesystem_policy() -> FileSystemPolicy_type:
    return FilesystemPolicy(
        read_only_paths=[],
        read_write_paths=[],
        block_symlinks=True,
        block_absolute_paths=True,
    )


FileSystemPolicy_type = FilesystemPolicy


@pytest.fixture
def import_policy() -> ImportPolicy:
    return ImportPolicy(
        blocked_modules={"os", "subprocess", "sys", "ctypes"},
        allowed_modules=set(),
    )


# ── Layer 1: RestrictedImporter unit tests ────────────────────────────


class TestRestrictedImporterGaps:
    def test_violation_logged_on_blocked_import(self) -> None:
        importer = RestrictedImporter(blocked={"fake_blocked_mod_xyz"})
        with pytest.raises(ImportError):
            importer.find_spec("fake_blocked_mod_xyz")
        violations = importer.get_violations()
        assert len(violations) == 1
        assert violations[0].module_name == "fake_blocked_mod_xyz"

    def test_clear_violations_resets_log(self) -> None:
        importer = RestrictedImporter(blocked={"fake_mod_a"})
        with pytest.raises(ImportError):
            importer.find_spec("fake_mod_a")
        assert len(importer.get_violations()) == 1
        importer.clear_violations()
        assert importer.get_violations() == []

    def test_allowed_whitelist_blocks_undeclared(self) -> None:
        importer = RestrictedImporter(blocked=set(), allowed={"json", "math"})
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("re")

    def test_allowed_whitelist_permits_declared(self) -> None:
        importer = RestrictedImporter(blocked=set(), allowed={"json", "math"})
        result = importer.find_spec("json")
        assert result is None

    def test_plugin_id_propagated_to_violation(self) -> None:
        importer = RestrictedImporter(blocked={"evil_mod"}, plugin_id="plugin-42")
        with pytest.raises(ImportError):
            importer.find_spec("evil_mod")
        assert importer.get_violations()[0].plugin_id == "plugin-42"

    def test_install_uninstall_cycle(self) -> None:
        original_import = builtins.__import__
        importer = RestrictedImporter(blocked={"os"})
        importer.install()
        assert builtins.__import__ is not original_import
        importer.uninstall()
        assert builtins.__import__ is original_import

    def test_double_install_idempotent(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        importer.install()
        first = builtins.__import__
        importer.install()
        assert builtins.__import__ is first
        importer.uninstall()

    def test_double_uninstall_safe(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        importer.install()
        importer.uninstall()
        importer.uninstall()


# ── Layer 2: IntrospectionGuard unit tests ────────────────────────────


class TestRestrictedObject:
    def test_subclasses_raises_runtime_error(self) -> None:
        with pytest.raises(RuntimeError, match="not allowed"):
            _RestrictedObject.__subclasses__()

    def test_is_class(self) -> None:
        assert isinstance(_RestrictedObject, type)


class TestIntrospectionGuardIsBlockedAttr:
    def test_blocked_attrs_detected(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        for attr in ("__subclasses__", "__bases__", "__mro__", "__globals__", "__dict__"):
            assert guard._is_blocked_attr(attr) is True, f"{attr} should be blocked"

    def test_explicitly_blocked_attrs(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        for attr in ("__reduce__", "__reduce_ex__", "__getstate__", "__setstate__"):
            assert guard._is_blocked_attr(attr) is True, f"{attr} should be blocked"

    def test_frame_attrs_blocked_when_enabled(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        for attr in ("tb_frame", "f_back", "f_globals", "f_locals"):
            assert guard._is_blocked_attr(attr) is True, f"{attr} should be blocked"

    def test_frame_attrs_allowed_when_disabled(self, permissive_introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(permissive_introspection_policy)
        for attr in ("tb_frame", "f_back", "f_globals", "f_locals"):
            assert guard._is_blocked_attr(attr) is False, f"{attr} should be allowed"

    def test_normal_attr_not_blocked(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        assert guard._is_blocked_attr("name") is False
        assert guard._is_blocked_attr("value") is False
        assert guard._is_blocked_attr("__repr__") is False

    def test_custom_blocked_attributes(self) -> None:
        policy = IntrospectionPolicy(blocked_attributes={"__secret__"})
        guard = IntrospectionGuard(policy)
        assert guard._is_blocked_attr("__secret__") is True
        assert guard._is_blocked_attr("normal_attr") is False

    def test_return_type_is_always_bool(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        assert isinstance(guard._is_blocked_attr("__subclasses__"), bool)
        assert isinstance(guard._is_blocked_attr("name"), bool)
        permissive = IntrospectionPolicy(block_frame_access=False)
        guard2 = IntrospectionGuard(permissive)
        assert isinstance(guard2._is_blocked_attr("tb_frame"), bool)
        assert isinstance(guard2._is_blocked_attr("name"), bool)


class TestIntrospectionGuardRestrictedGetattr:
    def test_blocked_attr_raises_permission_error(
        self, introspection_policy: IntrospectionPolicy
    ) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard._original_getattr = getattr
        with pytest.raises(PermissionError, match="not accessible"):
            guard._restricted_getattr(object, "__globals__")

    def test_allowed_attr_passes_through(
        self, introspection_policy: IntrospectionPolicy
    ) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard._original_getattr = getattr
        result = guard._restricted_getattr(str, "__name__")
        assert result == "str"

    def test_violation_logged_on_block(
        self, introspection_policy: IntrospectionPolicy
    ) -> None:
        guard = IntrospectionGuard(introspection_policy, plugin_id="p1")
        guard._original_getattr = getattr
        with pytest.raises(PermissionError):
            guard._restricted_getattr(object, "__code__")
        violations = guard.get_violations()
        assert len(violations) == 1
        assert violations[0].attribute == "__code__"
        assert violations[0].plugin_id == "p1"


class TestIntrospectionGuardRestrictedSetattr:
    def test_blocked_setattr_raises(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard._original_setattr = setattr
        with pytest.raises(PermissionError):
            guard._restricted_setattr(object, "__globals__", None)

    def test_allowed_setattr_passes(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard._original_setattr = setattr
        obj = type("Obj", (), {"x": 0})()
        guard._restricted_setattr(obj, "x", 42)
        assert obj.x == 42


class TestIntrospectionGuardRestrictedDir:
    def test_dir_filters_blocked_attrs(
        self, introspection_policy: IntrospectionPolicy
    ) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard._original_dir = dir
        result = guard._restricted_dir(object)
        for blocked in ("__subclasses__", "__globals__", "__bases__", "__dict__"):
            assert blocked not in result

    def test_dir_keeps_normal_attrs(
        self, introspection_policy: IntrospectionPolicy
    ) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard._original_dir = dir
        result = guard._restricted_dir(str)
        assert "__len__" in result
        assert "upper" in result


class TestIntrospectionGuardInstallUninstall:
    def test_install_replaces_builtins(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        orig_getattr = builtins.getattr
        orig_setattr = builtins.setattr
        orig_dir = builtins.dir
        try:
            guard.install()
            assert builtins.getattr is not orig_getattr
            assert builtins.setattr is not orig_setattr
            assert builtins.dir is not orig_dir
            assert builtins.object is _RestrictedObject
        finally:
            guard.uninstall()

    def test_uninstall_restores_builtins(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        orig_getattr = builtins.getattr
        orig_setattr = builtins.setattr
        orig_dir = builtins.dir
        orig_object = builtins.object
        guard.install()
        guard.uninstall()
        assert builtins.getattr is orig_getattr
        assert builtins.setattr is orig_setattr
        assert builtins.dir is orig_dir
        assert builtins.object is orig_object

    def test_double_install_noop(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard.install()
        guard.install()
        guard.uninstall()
        assert guard._installed is False

    def test_double_uninstall_safe(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard.install()
        guard.uninstall()
        guard.uninstall()

    def test_blocked_builtins_replaced(self) -> None:
        policy = IntrospectionPolicy(blocked_builtins={"eval"}, blocked_attributes=set())
        guard = IntrospectionGuard(policy)
        orig_eval = builtins.eval
        try:
            guard.install()
            assert builtins.eval is not orig_eval
            with pytest.raises(PermissionError, match="not available"):
                builtins.eval("1+1")  # noqa: S307
        finally:
            guard.uninstall()

    def test_get_violations_returns_copy(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard._original_getattr = getattr
        with pytest.raises(PermissionError):
            guard._restricted_getattr(object, "__globals__")
        v1 = guard.get_violations()
        v2 = guard.get_violations()
        assert v1 is not v2
        assert v1 == v2

    def test_clear_violations(self, introspection_policy: IntrospectionPolicy) -> None:
        guard = IntrospectionGuard(introspection_policy)
        guard._original_getattr = getattr
        with pytest.raises(PermissionError):
            guard._restricted_getattr(object, "__globals__")
        assert len(guard.get_violations()) == 1
        guard.clear_violations()
        assert len(guard.get_violations()) == 0


# ── Layer 3: NetworkGuard unit tests ──────────────────────────────────


class TestNetworkGuardIsPrivateIp:
    def test_loopback_is_private(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("127.0.0.1") is True

    def test_private_class_a(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("10.0.0.1") is True

    def test_private_class_b(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("172.16.0.1") is True

    def test_private_class_c(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("192.168.1.1") is True

    def test_public_ip_not_private(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("8.8.8.8") is False

    def test_link_local(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("169.254.1.1") is True

    def test_invalid_host_returns_false(self) -> None:
        from engine.plugins.sandbox.layers.network_guard import _is_private_ip
        assert _is_private_ip("not-an-ip") is False


class TestNetworkGuardPortAllowance:
    def test_no_port_restriction(self) -> None:
        policy = NetworkPolicy(allowed_ports=set())
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(80) is True
        assert guard._is_port_allowed(443) is True

    def test_specific_ports(self) -> None:
        policy = NetworkPolicy(allowed_ports={443})
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(443) is True
        assert guard._is_port_allowed(80) is False

    def test_none_port_allowed(self) -> None:
        policy = NetworkPolicy(allowed_ports={443})
        guard = NetworkGuard(policy)
        assert guard._is_port_allowed(None) is True


class TestNetworkGuardCidr:
    def test_host_in_cidr(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["10.0.0.0/8"])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("10.1.2.3") is True

    def test_host_not_in_cidr(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["10.0.0.0/8"])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("192.168.1.1") is False

    def test_empty_cidrs(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=[])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("10.0.0.1") is False

    def test_invalid_cidr_ignored(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["not-a-cidr"])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("10.0.0.1") is False


class TestNetworkGuardHostAllowed:
    def test_allowed_endpoint_passes(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("api.example.com") is True

    def test_subdomain_of_allowed_passes(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("api.example.com") is True

    def test_unknown_host_blocked(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("evil.com") is False

    def test_private_ip_without_allowance_blocked(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("127.0.0.1") is False

    def test_private_ip_in_cidr_allowed(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=[],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("10.1.2.3") is True


class TestNetworkGuardInstallUninstall:
    def test_install_uninstall_cycle(self) -> None:
        import socket

        import httpx

        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy, plugin_id="test")
        orig_send = httpx.AsyncClient.send
        orig_socket = socket.create_connection
        orig_getaddrinfo = socket.getaddrinfo
        try:
            guard.install()
            assert guard._installed is True
        finally:
            guard.uninstall()
            assert guard._installed is False
            assert httpx.AsyncClient.send is orig_send
            assert socket.create_connection is orig_socket
            assert socket.getaddrinfo is orig_getaddrinfo

    def test_double_install_noop(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy)
        guard.install()
        guard.install()
        guard.uninstall()

    def test_double_uninstall_safe(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy)
        guard.install()
        guard.uninstall()
        guard.uninstall()

    def test_violation_tracking(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        guard = NetworkGuard(policy, plugin_id="p1")
        violations = guard.get_violations()
        assert violations == []
        guard.clear_violations()


# ── Layer 4: ResourceLimiter unit tests ───────────────────────────────


class TestWallTimer:
    def test_not_expired_initially(self) -> None:
        timer = _WallTimer(limit=10.0)
        assert timer.expired is False

    def test_elapsed_zero_before_start(self) -> None:
        timer = _WallTimer(limit=10.0)
        assert timer.elapsed == 0.0

    def test_start_sets_active(self) -> None:
        timer = _WallTimer(limit=10.0)
        timer.start()
        assert timer._active is True
        assert timer._start_time is not None
        timer.stop()

    def test_stop_resets_state(self) -> None:
        timer = _WallTimer(limit=10.0)
        timer.start()
        timer.stop()
        assert timer._active is False
        assert timer._start_time is None

    def test_expired_after_limit(self) -> None:
        timer = _WallTimer(limit=0.0)
        timer.start()
        assert timer.expired is True
        timer.stop()

    def test_check_raises_when_expired(self) -> None:
        timer = _WallTimer(limit=0.0, plugin_id="test-p")
        timer.start()
        with pytest.raises(ResourceExhausted, match="wall_time") as exc_info:
            timer.check()
        assert exc_info.value.plugin_id == "test-p"
        timer.stop()

    def test_elapsed_increases(self) -> None:
        timer = _WallTimer(limit=10.0)
        timer.start()
        e1 = timer.elapsed
        time.sleep(0.01)
        e2 = timer.elapsed
        assert e2 >= e1
        timer.stop()


class TestResourceLimiterInstallUninstall:
    def test_install_sets_installed(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        limiter.install()
        assert limiter._installed is True
        limiter.uninstall()

    def test_uninstall_clears_wall_timer(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        limiter.install()
        assert limiter._wall_timer is not None
        limiter.uninstall()
        assert limiter._wall_timer is None
        assert limiter._installed is False

    def test_double_install_noop(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        limiter.install()
        limiter.install()
        limiter.uninstall()

    def test_double_uninstall_safe(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        limiter.install()
        limiter.uninstall()
        limiter.uninstall()


class TestResourceLimiterThreadLimit:
    def test_within_limit(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        limiter.increment_thread()
        assert limiter._thread_count == 1
        limiter.increment_thread()
        assert limiter._thread_count == 2

    def test_exceeds_limit_raises(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        limiter.increment_thread()
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted, match="threads"):
            limiter.increment_thread()

    def test_decrement_thread_clamps_zero(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_thread_violation_logged(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy, plugin_id="p1")
        limiter.increment_thread()
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        violations = limiter.get_violations()
        assert len(violations) == 1
        assert violations[0].plugin_id == "p1"


class TestResourceLimiterParseMemory:
    def test_gb(self) -> None:
        assert ResourceLimiter.parse_memory("2GB") == 2 * 1024**3

    def test_mb(self) -> None:
        assert ResourceLimiter.parse_memory("512MB") == 512 * 1024**2

    def test_kb(self) -> None:
        assert ResourceLimiter.parse_memory("256KB") == 256 * 1024

    def test_bytes(self) -> None:
        assert ResourceLimiter.parse_memory("1024B") == 1024

    def test_plain_number(self) -> None:
        assert ResourceLimiter.parse_memory("1048576") == 1_048_576

    def test_case_insensitive(self) -> None:
        assert ResourceLimiter.parse_memory("512mb") == 512 * 1024**2

    def test_with_whitespace(self) -> None:
        assert ResourceLimiter.parse_memory("  1GB  ") == 1024**3


class TestResourceLimiterViolations:
    def test_get_violations_returns_copy(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        v1 = limiter.get_violations()
        v2 = limiter.get_violations()
        assert v1 is not v2

    def test_clear_violations(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        limiter.increment_thread()
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        assert len(limiter.get_violations()) == 1
        limiter.clear_violations()
        assert len(limiter.get_violations()) == 0

    def test_cpu_elapsed_when_not_installed(self, resource_policy: ResourcePolicy) -> None:
        limiter = ResourceLimiter(resource_policy)
        assert limiter.cpu_elapsed == 0.0


# ── Layer 5: FilesystemIsolation unit tests ───────────────────────────


class TestFilesystemIsolationPathAllowed:
    def test_work_dir_allowed(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        try:
            resolved = os.path.realpath(fs.work_dir)
            assert fs._is_path_allowed(resolved) is True
        finally:
            fs.cleanup()

    def test_outside_work_dir_blocked(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        try:
            assert fs._is_path_allowed("/etc/passwd") is False
        finally:
            fs.cleanup()

    def test_read_only_path_allowed(self, tmp_path: Any) -> None:
        ro = tmp_path / "readonly"
        ro.mkdir()
        policy = FilesystemPolicy(read_only_paths=[str(ro)])
        fs = FilesystemIsolation(policy)
        try:
            assert fs._is_path_allowed(str(ro)) is True
        finally:
            fs.cleanup()

    def test_read_write_path_allowed(self, tmp_path: Any) -> None:
        rw = tmp_path / "readwrite"
        rw.mkdir()
        policy = FilesystemPolicy(read_write_paths=[str(rw)])
        fs = FilesystemIsolation(policy)
        try:
            assert fs._is_path_allowed(str(rw)) is True
        finally:
            fs.cleanup()


class TestFilesystemIsolationWriteAllowed:
    def test_file_in_work_dir_write_allowed(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        try:
            file_path = os.path.join(fs.work_dir, "output.txt")
            resolved = os.path.realpath(file_path)
            assert fs._is_write_allowed(resolved) is True
        finally:
            fs.cleanup()

    def test_read_only_path_write_denied(self, tmp_path: Any) -> None:
        ro = tmp_path / "readonly"
        ro.mkdir()
        policy = FilesystemPolicy(read_only_paths=[str(ro)])
        fs = FilesystemIsolation(policy)
        try:
            file_path = os.path.join(str(ro), "data.txt")
            resolved = os.path.realpath(file_path)
            assert fs._is_write_allowed(resolved) is False
        finally:
            fs.cleanup()

    def test_file_in_read_write_path_allowed(self, tmp_path: Any) -> None:
        rw = tmp_path / "rw"
        rw.mkdir()
        policy = FilesystemPolicy(read_write_paths=[str(rw)])
        fs = FilesystemIsolation(policy)
        try:
            file_path = os.path.join(str(rw), "output.txt")
            resolved = os.path.realpath(file_path)
            assert fs._is_write_allowed(resolved) is True
        finally:
            fs.cleanup()


class TestFilesystemIsolationRestrictedOpen:
    def test_fd_access_blocked(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        fs._original_open = open
        try:
            with pytest.raises(PermissionError, match="fd_access"):
                fs._restricted_open(0)
        finally:
            fs.cleanup()

    def test_outside_path_blocked(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        fs._original_open = open
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                fs._restricted_open("/etc/passwd")
        finally:
            fs.cleanup()

    def test_write_to_readonly_path_blocked(self, tmp_path: Any) -> None:
        ro = tmp_path / "readonly"
        ro.mkdir()
        ro_file = ro / "data.txt"
        ro_file.write_text("read-only content")
        policy = FilesystemPolicy(read_only_paths=[str(ro)])
        fs = FilesystemIsolation(policy)
        fs._original_open = open
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                fs._restricted_open(str(ro_file), "w")
        finally:
            fs.cleanup()

    def test_read_allowed_in_readonly_path(self, tmp_path: Any) -> None:
        ro = tmp_path / "readonly"
        ro.mkdir()
        ro_file = ro / "data.txt"
        ro_file.write_text("read-only content")
        policy = FilesystemPolicy(read_only_paths=[str(ro)])
        fs = FilesystemIsolation(policy)
        fs._original_open = open
        try:
            result = fs._restricted_open(str(ro_file), "r")
            content = result.read()
            result.close()
            assert content == "read-only content"
        finally:
            fs.cleanup()


class TestFilesystemIsolationInstallUninstall:
    def test_install_replaces_open(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        orig_open = builtins.open
        try:
            fs.install()
            assert builtins.open is not orig_open
        finally:
            fs.uninstall()
            assert builtins.open is orig_open
            fs.cleanup()

    def test_double_install_noop(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        fs.install()
        fs.install()
        fs.uninstall()
        fs.cleanup()

    def test_double_uninstall_safe(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        fs.install()
        fs.uninstall()
        fs.uninstall()
        fs.cleanup()


class TestFilesystemIsolationCleanup:
    def test_cleanup_removes_owned_work_dir(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy)
        work_dir = fs.work_dir
        assert os.path.isdir(work_dir)
        fs.cleanup()
        assert not os.path.isdir(work_dir)

    def test_cleanup_preserves_provided_work_dir(self, tmp_path: Any) -> None:
        provided = str(tmp_path / "custom_work")
        os.makedirs(provided, exist_ok=True)
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy, work_dir=provided)
        fs.cleanup()
        assert os.path.isdir(provided)

    def test_violation_tracking(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy, plugin_id="p1")
        fs._original_open = open
        with pytest.raises(PermissionError):
            fs._restricted_open("/etc/passwd")
        violations = fs.get_violations()
        assert len(violations) == 1
        assert isinstance(violations[0], FilesystemViolation)
        assert violations[0].plugin_id == "p1"
        fs.clear_violations()
        assert fs.get_violations() == []
        fs.cleanup()


# ── SandboxPolicy.from_trust_level ────────────────────────────────────


class TestSandboxPolicyFromTrustLevel:
    def test_untrusted_default(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="p1")
        assert policy.trust_level == "untrusted"
        assert "os" in policy.import_policy.blocked_modules
        assert len(policy.import_policy.blocked_modules) >= 10

    def test_trusted_limited(self) -> None:
        policy = SandboxPolicy.from_trust_level("trusted_limited", plugin_id="p2")
        assert policy.trust_level == "trusted_limited"
        assert "os" in policy.import_policy.blocked_modules
        assert policy.resource_policy.max_cpu_seconds == 120

    def test_trusted_full(self) -> None:
        policy = SandboxPolicy.from_trust_level("trusted_full", plugin_id="p3")
        assert policy.trust_level == "trusted"
        assert "subprocess" in policy.import_policy.blocked_modules
        assert policy.resource_policy.max_cpu_seconds == 300

    def test_invalid_trust_level_defaults_untrusted(self) -> None:
        policy = SandboxPolicy.from_trust_level("invalid_level")
        assert policy.trust_level == "untrusted"


# ── SandboxContext integration tests ──────────────────────────────────


class TestSandboxContextIntegration:
    def test_activate_deactivate_cycle(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        assert ctx.is_active is False
        ctx.activate()
        assert ctx.is_active is True
        ctx.deactivate()
        assert ctx.is_active is False
        ctx.cleanup()

    def test_double_activate_idempotent(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.activate()
        assert ctx.is_active is True
        ctx.deactivate()
        ctx.cleanup()

    def test_double_deactivate_safe(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.deactivate()
        ctx.deactivate()
        assert ctx.is_active is False
        ctx.cleanup()

    def test_context_manager(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        with ctx:
            assert ctx.is_active is True
        assert ctx.is_active is False
        ctx.cleanup()

    def test_work_dir_property(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        assert ctx.work_dir is not None
        assert os.path.isdir(ctx.work_dir)
        ctx.cleanup()

    def test_event_logger_has_no_events_initially(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        assert ctx.event_logger.event_count == 0
        ctx.cleanup()

    def test_trust_level_property(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        from engine.plugins.trust_levels import TrustLevel
        assert ctx.trust_level == TrustLevel.UNTRUSTED
        ctx.cleanup()

    def test_validate_trust_level_untrusted(self) -> None:
        policy = SandboxPolicy.from_trust_level("untrusted", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True
        ctx.cleanup()

    def test_validate_trust_level_trusted_limited(self) -> None:
        policy = SandboxPolicy.from_trust_level("trusted_limited", plugin_id="ctx-test")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True
        ctx.cleanup()


class TestSandboxViolationCategories:
    def test_import_violation_category(self) -> None:
        v = ImportViolation("os", plugin_id="p1")
        assert v.category == SandboxViolationCategory.IMPORT
        assert v.to_dict()["category"] == "import"

    def test_network_violation_category(self) -> None:
        v = NetworkViolation("evil.com", port=80, plugin_id="p2")
        assert v.category == SandboxViolationCategory.NETWORK
        d = v.to_dict()
        assert d["category"] == "network"
        assert d["plugin_id"] == "p2"

    def test_filesystem_violation_category(self) -> None:
        v = FilesystemViolation("/etc/passwd", "read", plugin_id="p3")
        assert v.category == SandboxViolationCategory.FILESYSTEM
        assert v.to_dict()["attempted_action"] == "read:/etc/passwd"

    def test_introspection_violation_category(self) -> None:
        v = IntrospectionViolation("__globals__", plugin_id="p4")
        assert v.category == SandboxViolationCategory.INTROSPECTION
        assert v.to_dict()["category"] == "introspection"

    def test_resource_exhausted_category(self) -> None:
        v = ResourceExhausted("memory", 1024, 2048, plugin_id="p5")
        assert v.category == SandboxViolationCategory.RESOURCE
        d = v.to_dict()
        assert d["detail"] == "Resource limit exceeded: memory (limit=1024, current=2048)"

    def test_violation_without_plugin_id(self) -> None:
        v = ImportViolation("os")
        assert v.plugin_id is None
        assert v.to_dict()["plugin_id"] is None
