"""
Comprehensive tests for the 5-layer sandbox security model.

Covers cross-layer interactions, trust-level enforcement, security event
aggregation, edge cases, and attack scenarios that span multiple layers.

  Layers:
  1. Import restrictions - blocked stdlib modules
  2. Network whitelist - endpoint/CIDR/DNS enforcement
  3. Resource limits - CPU, memory, FD, thread limits
  4. Filesystem isolation - path-based read/write restrictions
  5. Introspection blocking - frame/gc/inspect/dunder abuse prevention
"""

from __future__ import annotations

import asyncio
import builtins
import io
import os
import socket
import tempfile
from types import SimpleNamespace
from typing import Any, ClassVar

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
from engine.plugins.sandbox.layers.filesystem_isolation import FilesystemIsolation
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import (
    _EXPLICITLY_BLOCKED_ATTRS,
    _FRAME_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.network_guard import NetworkGuard
from engine.plugins.sandbox.layers.resource_limiter import (
    HAS_RESOURCE_MODULE,
    ResourceLimiter,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import (
    PluginMetrics,
    SandboxMetricsCollector,
)
from engine.plugins.trust_levels import TrustLevel

# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def untrusted_policy() -> SandboxPolicy:
    return SandboxPolicy(
        plugin_id="test_untrusted",
        trust_level="untrusted",
        import_policy=ImportPolicy(
            blocked_modules={"os", "subprocess", "sys", "socket", "ctypes", "gc", "inspect"},
        ),
        network_policy=NetworkPolicy(allowed_endpoints=["api.trusted.com"]),
        resource_policy=ResourcePolicy(
            max_cpu_seconds=5,
            max_memory_bytes=256 * 1024 * 1024,
            max_file_descriptors=32,
            max_threads=1,
        ),
        filesystem_policy=FilesystemPolicy(),
        introspection_policy=IntrospectionPolicy(),
    )


@pytest.fixture
def trusted_policy() -> SandboxPolicy:
    return SandboxPolicy.trusted_policy("test_trusted")


@pytest.fixture
def strict_policy() -> SandboxPolicy:
    return SandboxPolicy(
        plugin_id="strict",
        trust_level="untrusted",
        import_policy=ImportPolicy(
            blocked_modules={"os", "subprocess", "sys", "socket"},
            allowed_modules={"json", "math", "datetime"},
        ),
        network_policy=NetworkPolicy(),
        resource_policy=ResourcePolicy(max_cpu_seconds=1, max_memory_bytes=64 * 1024 * 1024),
        filesystem_policy=FilesystemPolicy(block_symlinks=True, block_absolute_paths=True),
        introspection_policy=IntrospectionPolicy(
            blocked_builtins={"eval", "exec", "compile", "breakpoint", "credits", "license"},
            blocked_attributes={"__subclasses__", "__globals__", "__bases__"},
            block_gc=True,
            block_inspect=True,
            block_frame_access=True,
        ),
    )


def _make_manifest(**overrides: Any) -> StrategyManifest:
    defaults: dict[str, Any] = {
        "id": "test",
        "name": "test",
        "version": "1.0.0",
        "resources": {"max_cpu_seconds": 2},
    }
    defaults.update(overrides)
    return StrategyManifest(**defaults)


# ─── Layer 1: Import Restrictions ──────────────────────────────────────


class TestImportLayerForbiddenStdlib:
    BLOCKED_STDLIB: ClassVar[list[str]] = [
        "os", "os.path", "os.environ",
        "subprocess", "subprocess.run",
        "sys", "sys.modules",
        "socket", "socket.socket",
        "ctypes", "ctypes.cdll",
        "gc", "gc.get_objects",
        "inspect", "inspect.stack",
        "importlib", "importlib.import_module",
        "pickle", "pickle.dumps",
        "shelve", "marshal",
        "threading", "multiprocessing",
        "signal", "atexit",
        "code", "codeop", "ast", "dis",
        "runpy", "pkgutil",
        "pdb", "bdb",
    ]

    @pytest.mark.parametrize("module_name", BLOCKED_STDLIB)
    def test_blocked_stdlib_module_rejected(self, module_name: str) -> None:
        blocked_roots = {"os", "subprocess", "sys", "socket", "ctypes", "gc",
                         "inspect", "importlib", "pickle", "shelve", "marshal",
                         "threading", "multiprocessing", "signal", "atexit",
                         "code", "codeop", "ast", "dis", "runpy", "pkgutil",
                         "pdb", "bdb"}
        importer = RestrictedImporter(blocked=blocked_roots)
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec(module_name)

    def test_allowlist_blocks_everything_not_listed(self) -> None:
        importer = RestrictedImporter(blocked=set(), allowed={"json"})
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("math")

    def test_allowlist_permits_listed_module(self) -> None:
        importer = RestrictedImporter(blocked=set(), allowed={"json"})
        assert importer.find_spec("json") is None

    def test_blocked_takes_priority_over_allowed(self) -> None:
        importer = RestrictedImporter(blocked={"json"}, allowed={"json"})
        with pytest.raises(ImportError):
            importer.find_spec("json")

    def test_relative_import_level_nonzero_skips_check(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        original_import = builtins.__import__
        called = False

        def fake_import(name, g=None, local_vars=None, fromlist=(), level=0):
            nonlocal called
            called = True
            return original_import(name, g, local_vars, fromlist, level)

        try:
            builtins.__import__ = importer._restricted_import
            importer._original_import = fake_import
            builtins.__import__("json", {"__name__": "__main__"}, {}, (), 1)
        except ImportError:
            pass
        finally:
            builtins.__import__ = original_import
        assert called
        assert len(importer.get_violations()) == 0

    def test_import_violation_includes_plugin_id(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="my_plugin")
        with pytest.raises(ImportError):
            importer.find_spec("os")
        v = importer.get_violations()
        assert v[0].plugin_id == "my_plugin"

    def test_import_violation_to_dict(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p1")
        with pytest.raises(ImportError):
            importer.find_spec("os")
        d = importer.get_violations()[0].to_dict()
        assert d["category"] == "import"
        assert "os" in d["detail"]
        assert d["attempted_action"] == "import os"

    def test_multiple_blocked_imports_accumulate_violations(self) -> None:
        importer = RestrictedImporter(blocked={"os", "sys", "subprocess"})
        for mod in ["os", "sys", "subprocess"]:
            with pytest.raises(ImportError):
                importer.find_spec(mod)
        assert len(importer.get_violations()) == 3
        names = {v.module_name for v in importer.get_violations()}
        assert names == {"os", "sys", "subprocess"}

    def test_install_uninstall_cycle_restores_clean_state(self) -> None:
        original = builtins.__import__
        importer = RestrictedImporter(blocked={"os"})
        importer.install()
        assert builtins.__import__ is not original
        with pytest.raises(ImportError):
            builtins.__import__("os")
        importer.uninstall()
        assert builtins.__import__ is original
        import json
        assert json is not None


# ─── Layer 2: Network Whitelist ────────────────────────────────────────


class TestNetworkLayerWhitelist:
    def test_exact_host_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("api.example.com") is True

    def test_subdomain_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("cdn.example.com") is True
        assert guard._is_host_allowed("deep.cdn.example.com") is True

    def test_partial_name_not_confused(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("notexample.com") is False
        assert guard._is_host_allowed("example.com.evil.org") is False

    def test_empty_whitelist_blocks_all(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("any.host") is False

    def test_ipv4_cidr_allowed(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["10.0.0.0/8", "192.168.0.0/16"])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("10.1.2.3") is True
        assert guard._is_host_in_cidr("192.168.1.1") is True
        assert guard._is_host_in_cidr("172.16.0.1") is False

    def test_ipv6_cidr_allowed(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["::1/128", "fd00::/8"])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("::1") is True
        assert guard._is_host_in_cidr("fd00::1") is True
        assert guard._is_host_in_cidr("8.8.8.8") is False

    def test_endpoint_and_cidr_combined(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["api.internal"],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("api.internal") is True
        assert guard._is_host_allowed("10.0.0.50") is True
        assert guard._is_host_allowed("evil.com") is False

    def test_dns_blocked_by_default(self) -> None:
        policy = NetworkPolicy(block_dns=True)
        guard = NetworkGuard(policy, plugin_id="p1")
        guard._original_getaddrinfo = socket.getaddrinfo
        with pytest.raises(PermissionError, match="DNS lookup"):
            guard._restricted_getaddrinfo("evil.com", 443)

    def test_dns_allowed_for_whitelisted(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["localhost"],
            block_dns=True,
        )
        guard = NetworkGuard(policy)
        guard._original_getaddrinfo = socket.getaddrinfo
        result = guard._restricted_getaddrinfo("localhost", 0)
        assert result is not None

    def test_dns_allowed_when_block_dns_false(self) -> None:
        policy = NetworkPolicy(block_dns=False)
        guard = NetworkGuard(policy)
        guard._original_getaddrinfo = socket.getaddrinfo
        result = guard._restricted_getaddrinfo("localhost", 0)
        assert result is not None

    async def test_httpx_send_blocks_non_whitelisted(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")

        async def fake_send(client: Any, request: Any, **kw: Any) -> Any:
            return "response"

        restricted = guard._make_restricted_send(fake_send)
        req = httpx.Request("GET", "https://evil.com/api")
        with pytest.raises(PermissionError, match="not allowed"):
            await restricted(None, req)

    async def test_httpx_send_allows_whitelisted(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy)

        async def fake_send(client: Any, request: Any, **kw: Any) -> str:
            return "ok"

        restricted = guard._make_restricted_send(fake_send)
        req = httpx.Request("GET", "https://safe.com/api")
        result = await restricted(None, req)
        assert result == "ok"

    def test_network_violation_logged_with_port(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")
        with pytest.raises(PermissionError):
            guard._restricted_create_connection(("evil.com", 443))
        v = guard.get_violations()
        assert len(v) == 1
        assert v[0].host == "evil.com"
        assert v[0].port == 443
        assert v[0].plugin_id == "p1"

    def test_install_uninstall_restores_all_monkeypatches(self) -> None:
        guard = NetworkGuard(NetworkPolicy())
        orig_httpx = httpx.AsyncClient.send
        orig_socket = socket.create_connection
        orig_dns = socket.getaddrinfo
        try:
            guard.install()
            assert httpx.AsyncClient.send is not orig_httpx
            assert socket.create_connection is not orig_socket
            assert socket.getaddrinfo is not orig_dns
        finally:
            guard.uninstall()
        assert httpx.AsyncClient.send is orig_httpx
        assert socket.create_connection is orig_socket
        assert socket.getaddrinfo is orig_dns


# ─── Layer 3: Resource Limits ──────────────────────────────────────────


class TestResourceLayerLimits:
    def test_thread_limit_at_boundary(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=2), plugin_id="p1")
        limiter.increment_thread()
        limiter.increment_thread()
        assert limiter._thread_count == 2
        with pytest.raises(ResourceExhausted, match="threads"):
            limiter.increment_thread()

    def test_thread_count_does_not_go_negative(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=2))
        limiter.decrement_thread()
        limiter.decrement_thread()
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_zero_thread_limit_immediate_block(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=0), plugin_id="p1")
        with pytest.raises(ResourceExhausted):
            limiter.check_thread_limit()
        v = limiter.get_violations()
        assert v[0].resource_type == "threads"
        assert v[0].limit == 0
        assert v[0].current == 0

    def test_thread_lifecycle_increment_decrement(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=5))
        for _ in range(5):
            limiter.increment_thread()
        assert limiter._thread_count == 5
        for _ in range(3):
            limiter.decrement_thread()
        assert limiter._thread_count == 2
        limiter.increment_thread()
        assert limiter._thread_count == 3

    @pytest.mark.skipif(not HAS_RESOURCE_MODULE, reason="no resource module")
    def test_memory_limit_applied(self) -> None:
        import resource

        policy = ResourcePolicy(max_memory_bytes=128 * 1024 * 1024)
        limiter = ResourceLimiter(policy)
        try:
            limiter.install()
            soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
            assert soft <= 128 * 1024 * 1024
        finally:
            limiter.uninstall()

    @pytest.mark.skipif(not HAS_RESOURCE_MODULE, reason="no resource module")
    def test_fd_limit_applied(self) -> None:
        import resource

        policy = ResourcePolicy(max_file_descriptors=16)
        limiter = ResourceLimiter(policy)
        try:
            limiter.install()
            soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert soft <= 16
        finally:
            limiter.uninstall()

    @pytest.mark.skipif(not HAS_RESOURCE_MODULE, reason="no resource module")
    def test_limits_restored_after_uninstall(self) -> None:
        import resource

        orig_soft, _orig_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        policy = ResourcePolicy(max_file_descriptors=min(16, orig_soft))
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.uninstall()
        restored_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert restored_soft == orig_soft

    def test_parse_memory_units(self) -> None:
        assert ResourceLimiter.parse_memory("1GB") == 1024**3
        assert ResourceLimiter.parse_memory("512MB") == 512 * 1024**2
        assert ResourceLimiter.parse_memory("1024KB") == 1024 * 1024
        assert ResourceLimiter.parse_memory("4096B") == 4096
        assert ResourceLimiter.parse_memory("2097152") == 2097152

    def test_violation_returns_copy(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=0))
        with pytest.raises(ResourceExhausted):
            limiter.check_thread_limit()
        v1 = limiter.get_violations()
        v2 = limiter.get_violations()
        assert v1 is not v2
        assert v1 == v2

    def test_clear_violations_resets(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=0))
        with pytest.raises(ResourceExhausted):
            limiter.check_thread_limit()
        assert len(limiter.get_violations()) == 1
        limiter.clear_violations()
        assert len(limiter.get_violations()) == 0


# ─── Layer 4: Filesystem Isolation ─────────────────────────────────────


class TestFilesystemLayerIsolation:
    def test_work_dir_created_and_cleanup(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        work_dir = fs.work_dir
        assert os.path.isdir(work_dir)
        fs.cleanup()
        assert not os.path.isdir(work_dir)

    def test_custom_work_dir_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fs = FilesystemIsolation(FilesystemPolicy(), work_dir=td)
            fs.cleanup()
            assert os.path.isdir(td)

    def test_read_allowed_in_work_dir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fpath = os.path.join(fs.work_dir, "data.txt")
            with builtins.open(fpath, "w") as f:
                f.write("hello")
            fs._original_open = builtins.open
            result = fs._restricted_open(fpath, "r")
            content = result.read()
            result.close()
            assert content == "hello"
        finally:
            fs.cleanup()

    def test_write_allowed_in_work_dir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fpath = os.path.join(fs.work_dir, "output.txt")
            fs._original_open = builtins.open
            result = fs._restricted_open(fpath, "w")
            result.write("data")
            result.close()
            assert os.path.exists(fpath)
        finally:
            fs.cleanup()

    def test_read_blocked_outside_sandbox(self, tmp_path: Any) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive")
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="not allowed"):
            fs._restricted_open(str(secret), "r")
        fs.cleanup()

    def test_write_blocked_outside_sandbox(self, tmp_path: Any) -> None:
        target = tmp_path / "output.txt"
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="write"):
            fs._restricted_open(str(target), "w")
        fs.cleanup()

    def test_read_only_path_allows_read(self, tmp_path: Any) -> None:
        data_file = tmp_path / "data.bin"
        data_file.write_bytes(b"\x00\x01\x02")
        policy = FilesystemPolicy(read_only_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        fs._original_open = builtins.open
        result = fs._restricted_open(str(data_file), "r")
        result.close()
        fs.cleanup()

    def test_read_only_path_blocks_write(self, tmp_path: Any) -> None:
        data_file = tmp_path / "data.bin"
        data_file.write_text("original")
        policy = FilesystemPolicy(read_only_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="write"):
            fs._restricted_open(str(data_file), "w")
        fs.cleanup()

    def test_read_write_path_allows_write(self, tmp_path: Any) -> None:
        target = tmp_path / "output.txt"
        policy = FilesystemPolicy(read_write_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        fs._original_open = builtins.open
        result = fs._restricted_open(str(target), "w")
        result.close()
        fs.cleanup()

    def test_fd_access_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="fd_access"):
            fs._restricted_open(0, "r")
        fs.cleanup()

    def test_append_mode_detected_as_write(self, tmp_path: Any) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError):
            fs._restricted_open(str(tmp_path / "file"), "a")
        fs.cleanup()

    def test_plus_mode_detected_as_write(self, tmp_path: Any) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError):
            fs._restricted_open(str(tmp_path / "file"), "r+")
        fs.cleanup()

    def test_symlink_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        target = tmp_path / "secret.txt"
        target.write_text("sensitive")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError):
            fs._restricted_open(str(link), "r")
        fs.cleanup()

    def test_path_inside_work_dir_subdirectory(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            subdir = os.path.join(fs.work_dir, "sub", "deep")
            os.makedirs(subdir, exist_ok=True)
            assert fs._is_path_allowed(os.path.realpath(subdir)) is True
        finally:
            fs.cleanup()

    def test_etc_passwd_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            assert fs._is_path_allowed("/etc/passwd") is False
        finally:
            fs.cleanup()

    def test_install_uninstall_restores_open(self) -> None:
        orig_builtins = builtins.open
        orig_io = io.open
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            assert builtins.open is not orig_builtins
            assert io.open is not orig_io
        finally:
            fs.cleanup()
        assert builtins.open is orig_builtins
        assert io.open is orig_io


# ─── Layer 5: Introspection Blocking ──────────────────────────────────


class TestIntrospectionLayerBlocking:
    def test_blocked_dunder_attributes(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        for attr in _EXPLICITLY_BLOCKED_ATTRS:
            assert guard._is_blocked_attr(attr) is True, f"{attr} should be blocked"

    def test_frame_attributes_blocked(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        for attr in _FRAME_ATTRS:
            assert guard._is_blocked_attr(attr) is True, f"{attr} should be blocked"

    def test_normal_attributes_allowed(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        for attr in ["name", "upper", "__init__", "__str__", "__repr__", "value"]:
            assert guard._is_blocked_attr(attr) is False, f"{attr} should be allowed"

    def test_policy_custom_blocked_attributes(self) -> None:
        policy = IntrospectionPolicy(blocked_attributes={"__custom__"})
        guard = IntrospectionGuard(policy)
        assert guard._is_blocked_attr("__custom__") is True
        assert guard._is_blocked_attr("normal_attr") is False

    def test_restricted_getattr_blocks_dunder(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p1")
        guard._original_getattr = builtins.getattr
        with pytest.raises(PermissionError, match="not accessible"):
            guard._restricted_getattr(int, "__subclasses__")
        assert len(guard.get_violations()) == 1

    def test_restricted_getattr_allows_normal(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard._original_getattr = builtins.getattr
        result = guard._restricted_getattr("hello", "upper")
        assert callable(result)

    def test_restricted_getattr_with_default(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard._original_getattr = builtins.getattr
        result = guard._restricted_getattr("hello", "nonexistent", "fallback")
        assert result == "fallback"

    def test_install_blocks_eval(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.eval("1+1")  # noqa: S307
        finally:
            guard.uninstall()

    def test_install_blocks_exec(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.exec("x=1")  # noqa: S102
        finally:
            guard.uninstall()

    def test_install_blocks_compile(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.compile("1+1", "<test>", "eval")
        finally:
            guard.uninstall()

    def test_install_blocks_breakpoint(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.breakpoint()  # type: ignore[attr-defined]
        finally:
            guard.uninstall()

    def test_install_replaces_object_and_getattr(self) -> None:
        orig_object = builtins.object
        orig_getattr = builtins.getattr
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            assert builtins.object is not orig_object
            assert builtins.getattr is not orig_getattr
        finally:
            guard.uninstall()

    def test_uninstall_restores_all_builtins(self) -> None:
        originals = {
            "object": builtins.object,
            "getattr": builtins.getattr,
            "eval": builtins.eval,
            "exec": builtins.exec,
            "compile": builtins.compile,
            "breakpoint": builtins.breakpoint,  # type: ignore[attr-defined]
        }
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.install()
        guard.uninstall()
        for name, orig in originals.items():
            assert getattr(builtins, name) is orig

    def test_blocked_builtin_logs_violation_with_plugin_id(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p1")
        blocked_fn = guard._make_blocked_builtin("eval")
        with pytest.raises(PermissionError):
            blocked_fn()
        v = guard.get_violations()
        assert len(v) == 1
        assert v[0].attribute == "eval"
        assert v[0].plugin_id == "p1"

    def test_policy_specific_builtins_blocked(self) -> None:
        policy = IntrospectionPolicy(blocked_builtins={"credits", "license"})
        guard = IntrospectionGuard(policy)
        try:
            guard.install()
            with pytest.raises(PermissionError):
                builtins.credits()  # type: ignore[attr-defined]
            with pytest.raises(PermissionError):
                builtins.license()  # type: ignore[attr-defined]
        finally:
            guard.uninstall()

    def test_default_and_policy_builtins_union_blocked(self) -> None:
        policy = IntrospectionPolicy(blocked_builtins={"credits"})
        guard = IntrospectionGuard(policy)
        try:
            guard.install()
            with pytest.raises(PermissionError):
                builtins.eval("1")  # noqa: S307
            with pytest.raises(PermissionError):
                builtins.credits()  # type: ignore[attr-defined]
        finally:
            guard.uninstall()

    def test_multiple_violations_accumulate(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p1")
        guard._original_getattr = builtins.getattr
        for attr in ["__subclasses__", "__globals__", "__bases__"]:
            with pytest.raises(PermissionError):
                guard._restricted_getattr(int, attr)
        assert len(guard.get_violations()) == 3


# ─── Layer Composition ─────────────────────────────────────────────────


class TestLayerComposition:
    def test_all_five_layers_install_and_uninstall(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "comp")
        ctx = SandboxContext(policy)
        orig_import = builtins.__import__
        orig_open = builtins.open
        orig_getattr = builtins.getattr
        orig_object = builtins.object
        try:
            ctx.activate()
            assert ctx.is_active is True
            assert builtins.__import__ is not orig_import
            assert builtins.open is not orig_open
            assert builtins.getattr is not orig_getattr
            assert builtins.object is not orig_object
        finally:
            ctx.deactivate()
        assert builtins.__import__ is orig_import
        assert builtins.open is orig_open
        assert builtins.getattr is orig_getattr
        assert builtins.object is orig_object

    def test_context_manager_activates_and_deactivates(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "ctx_mgr")
        ctx = SandboxContext(policy)
        orig_import = builtins.__import__
        with ctx:
            assert ctx.is_active is True
            assert builtins.__import__ is not orig_import
        assert ctx.is_active is False
        assert builtins.__import__ is orig_import
        ctx.cleanup()

    def test_violations_from_all_layers_collected_on_deactivate(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "multi_violation")
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            with pytest.raises(ImportError):
                builtins.__import__("os")
            with pytest.raises(PermissionError):
                getattr(int, "__subclasses__")  # noqa: B009
        finally:
            ctx.deactivate()

        import_events = ctx.event_logger.get_events(category=SandboxViolationCategory.IMPORT)
        intro_events = ctx.event_logger.get_events(category=SandboxViolationCategory.INTROSPECTION)
        assert len(import_events) >= 1
        assert len(intro_events) >= 1

    def test_work_dir_accessible_during_activation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "workdir_test")
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            assert ctx.work_dir is not None
            assert os.path.isdir(ctx.work_dir)
        finally:
            ctx.deactivate()
        ctx.cleanup()

    def test_cleanup_removes_work_dir_after_activation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "cleanup_test")
        ctx = SandboxContext(policy)
        work_dir = ctx.work_dir
        ctx.activate()
        ctx.cleanup()
        assert not os.path.isdir(work_dir)

    def test_activate_idempotent(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "idempotent")
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.deactivate()
            ctx.cleanup()

    def test_deactivate_idempotent(self) -> None:
        policy = SandboxPolicy(plugin_id="deact_idem")
        ctx = SandboxContext(policy)
        ctx.deactivate()
        ctx.deactivate()
        assert ctx.is_active is False
        ctx.cleanup()

    def test_event_logger_tracks_plugin_id(self) -> None:
        policy = SandboxPolicy(plugin_id="tracked_plugin")
        ctx = SandboxContext(policy)
        assert ctx.event_logger._plugin_id == "tracked_plugin"
        ctx.cleanup()

    async def test_import_and_filesystem_violation_in_same_eval(self) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        policy = SandboxPolicy(
            plugin_id="dual_violation",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
        )

        class DualViolationStrat:
            name = "dual"
            version = "1.0"

            def on_bar(self, state, portfolio):
                import contextlib
                with contextlib.suppress(ImportError):
                    import os  # noqa: F401
                try:
                    with open("/etc/passwd") as f:
                        f.read()
                except (PermissionError, OSError):
                    pass
                return []

        executor = PluginSandboxExecutor(DualViolationStrat(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()


# ─── Trust Levels ──────────────────────────────────────────────────────


class TestTrustLevels:
    def test_untrusted_defaults(self, untrusted_policy: SandboxPolicy) -> None:
        assert untrusted_policy.trust_level == "untrusted"
        assert untrusted_policy.resource_policy.max_cpu_seconds == 5
        assert untrusted_policy.resource_policy.max_memory_bytes == 256 * 1024 * 1024
        assert "eval" in untrusted_policy.introspection_policy.blocked_builtins
        assert "exec" in untrusted_policy.introspection_policy.blocked_builtins
        assert len(untrusted_policy.import_policy.blocked_modules) > 0

    def test_trusted_relaxed_resources(self, trusted_policy: SandboxPolicy) -> None:
        assert trusted_policy.trust_level == "trusted_full"
        assert trusted_policy.resource_policy.max_cpu_seconds == 300
        assert trusted_policy.resource_policy.max_memory_bytes == 2 * 1024**3

    def test_trusted_fewer_blocked_imports(self, trusted_policy: SandboxPolicy) -> None:
        assert "subprocess" in trusted_policy.import_policy.blocked_modules
        assert "ctypes" in trusted_policy.import_policy.blocked_modules
        assert "os" not in trusted_policy.import_policy.blocked_modules
        assert "json" not in trusted_policy.import_policy.blocked_modules

    def test_trusted_fewer_blocked_builtins(self, trusted_policy: SandboxPolicy) -> None:
        assert "exec" in trusted_policy.introspection_policy.blocked_builtins
        assert "compile" in trusted_policy.introspection_policy.blocked_builtins
        assert "eval" not in trusted_policy.introspection_policy.blocked_builtins

    def test_trusted_fewer_blocked_attributes(self, trusted_policy: SandboxPolicy) -> None:
        untrusted = SandboxPolicy()
        assert len(trusted_policy.introspection_policy.blocked_attributes) < len(
            untrusted.introspection_policy.blocked_attributes
        )

    def test_strict_policy_allows_only_explicit_modules(self, strict_policy: SandboxPolicy) -> None:
        importer = RestrictedImporter(
            blocked=strict_policy.import_policy.blocked_modules,
            allowed=strict_policy.import_policy.allowed_modules,
        )
        assert importer.find_spec("json") is None
        with pytest.raises(ImportError):
            importer.find_spec("urllib")

    def test_strict_policy_no_network(self, strict_policy: SandboxPolicy) -> None:
        guard = NetworkGuard(strict_policy.network_policy)
        assert guard._is_host_allowed("any.host") is False

    def test_importer_respects_trusted_blocked_set(self, trusted_policy: SandboxPolicy) -> None:
        importer = RestrictedImporter(
            blocked=trusted_policy.import_policy.blocked_modules,
        )
        with pytest.raises(ImportError):
            importer.find_spec("subprocess")
        assert importer.find_spec("json") is None

    def test_resource_limits_differ_between_trust_levels(
        self, untrusted_policy: SandboxPolicy, trusted_policy: SandboxPolicy
    ) -> None:
        assert trusted_policy.resource_policy.max_cpu_seconds > untrusted_policy.resource_policy.max_cpu_seconds
        assert trusted_policy.resource_policy.max_memory_bytes > untrusted_policy.resource_policy.max_memory_bytes

    def test_from_manifest_creates_untrusted_by_default(self) -> None:
        manifest = SimpleNamespace(
            id="manifest_test",
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.trust_level == "untrusted"
        assert "os" in policy.import_policy.blocked_modules

    def test_trusted_policy_factory(self) -> None:
        policy = SandboxPolicy.trusted_policy("my_plugin")
        assert policy.plugin_id == "my_plugin"
        assert policy.trust_level == "trusted_full"

    async def test_trusted_plugin_can_use_more_resources(self) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        trusted = SandboxPolicy.trusted_policy("trusted_e2e")
        untrusted = SandboxPolicy(
            plugin_id="untrusted_e2e",
            resource_policy=ResourcePolicy(max_cpu_seconds=1),
        )

        class SlowishStrat:
            name = "slowish"
            version = "1.0"

            def on_bar(self, state, portfolio):
                import time
                time.sleep(0.05)
                return []

        trusted_executor = PluginSandboxExecutor(SlowishStrat(), trusted)
        untrusted_executor = PluginSandboxExecutor(SlowishStrat(), untrusted)
        try:
            trusted_signals = await trusted_executor.safe_evaluate(None, None, None)
            assert trusted_signals == []
            untrusted_signals = await untrusted_executor.safe_evaluate(None, None, None)
            assert untrusted_signals == []
        finally:
            trusted_executor.cleanup()
            untrusted_executor.cleanup()


# ─── Security Event Logging ────────────────────────────────────────────


class TestSecurityEventLogging:
    def test_event_logger_records_violation(self) -> None:
        logger = SecurityEventLogger(plugin_id="p1")
        v = ImportViolation("os", plugin_id="p1")
        logger.log_violation(v)
        assert logger.event_count == 1
        events = logger.get_events()
        assert len(events) == 1
        assert events[0].category == SandboxViolationCategory.IMPORT
        assert events[0].plugin_id == "p1"

    def test_event_logger_filters_by_category(self) -> None:
        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_violation(ImportViolation("os", plugin_id="p1"))
        logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))
        logger.log_violation(FilesystemViolation("/etc/passwd", "read", plugin_id="p1"))
        import_events = logger.get_events(category=SandboxViolationCategory.IMPORT)
        assert len(import_events) == 1
        assert import_events[0].category == SandboxViolationCategory.IMPORT

    def test_event_logger_limit(self) -> None:
        logger = SecurityEventLogger()
        for i in range(20):
            logger.log_violation(ImportViolation(f"mod_{i}"))
        events = logger.get_events(limit=5)
        assert len(events) == 5

    def test_event_logger_clear(self) -> None:
        logger = SecurityEventLogger()
        logger.log_violation(ImportViolation("os"))
        assert logger.event_count == 1
        logger.clear()
        assert logger.event_count == 0

    def test_event_logger_to_dicts(self) -> None:
        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_violation(ImportViolation("os", plugin_id="p1"))
        dicts = logger.to_dicts()
        assert len(dicts) == 1
        assert dicts[0]["category"] == "import"
        assert dicts[0]["plugin_id"] == "p1"

    def test_event_logger_get_events_since(self) -> None:
        import time

        logger = SecurityEventLogger()
        before = time.time()
        time.sleep(0.01)
        logger.log_violation(ImportViolation("os"))
        recent = logger.get_events_since(before)
        assert len(recent) == 1

    def test_all_five_violation_categories_logged(self) -> None:
        logger = SecurityEventLogger(plugin_id="five_layers")
        logger.log_violation(ImportViolation("os", plugin_id="p"))
        logger.log_violation(NetworkViolation("evil.com", plugin_id="p"))
        logger.log_violation(ResourceExhausted("memory", 512, 600, plugin_id="p"))
        logger.log_violation(FilesystemViolation("/etc/passwd", "read", plugin_id="p"))
        logger.log_violation(IntrospectionViolation("__globals__", plugin_id="p"))

        for cat in SandboxViolationCategory:
            events = logger.get_events(category=cat)
            assert len(events) >= 1, f"Expected events for {cat.value}"

    def test_context_collects_violations_from_all_layers(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "event_test")
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            with pytest.raises(ImportError):
                builtins.__import__("os")
            with pytest.raises(PermissionError):
                getattr(int, "__subclasses__")  # noqa: B009
        finally:
            ctx.deactivate()
        assert ctx.event_logger.event_count >= 2
        ctx.cleanup()


# ─── Metrics Collector ─────────────────────────────────────────────────


class TestMetricsCollector:
    def test_record_evaluation(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 3)
        metrics = collector.get_plugin_metrics("p1")
        assert metrics is not None
        assert metrics["total_evaluations"] == 1
        assert metrics["total_signals_emitted"] == 3
        assert metrics["avg_evaluation_ms"] == 100.0

    def test_record_evaluation_with_error(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 50.0, 0, error="timeout")
        metrics = collector.get_plugin_metrics("p1")
        assert metrics["errors"] == 1
        assert metrics["last_error"] == "timeout"

    def test_record_violation(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_violation("p1")
        metrics = collector.get_plugin_metrics("p1")
        assert metrics["security_violations"] == 1

    def test_get_all_metrics(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 10.0, 1)
        collector.record_evaluation("p2", 20.0, 2)
        all_metrics = collector.get_all_metrics()
        assert "p1" in all_metrics
        assert "p2" in all_metrics

    def test_reset_specific_plugin(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 10.0, 1)
        collector.record_evaluation("p2", 20.0, 2)
        collector.reset("p1")
        assert collector.get_plugin_metrics("p1") is None
        assert collector.get_plugin_metrics("p2") is not None

    def test_reset_all(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 10.0, 1)
        collector.reset()
        assert collector.get_plugin_metrics("p1") is None

    def test_metrics_accumulate(self) -> None:
        collector = SandboxMetricsCollector()
        collector.record_evaluation("p1", 100.0, 1)
        collector.record_evaluation("p1", 200.0, 2)
        metrics = collector.get_plugin_metrics("p1")
        assert metrics["total_evaluations"] == 2
        assert metrics["total_signals_emitted"] == 3
        assert metrics["avg_evaluation_ms"] == 150.0

    def test_plugin_metrics_to_dict(self) -> None:
        pm = PluginMetrics(plugin_id="test")
        d = pm.to_dict()
        assert "plugin_id" in d
        assert "total_evaluations" in d
        assert "security_violations" in d


# ─── End-to-End Integration with All 5 Layers ─────────────────────────


class TestFiveLayerE2E:
    async def test_good_strategy_passes_all_layers(self) -> None:
        manifest = _make_manifest()
        sandbox = StrategySandbox(
            type("Strat", (), {
                "name": "good", "version": "1.0",
                "on_bar": lambda self, s, p: [Signal.buy(symbol="AAPL", strategy_id="good")],
            })(),
            manifest,
        )
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()

    async def test_import_violation_caught(self) -> None:
        manifest = _make_manifest()

        class ImportOs:
            name = "import_os"
            version = "1.0"

            def on_bar(self, s, p):
                import os  # noqa: F401
                return []

        sandbox = StrategySandbox(ImportOs(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_filesystem_violation_caught(self) -> None:
        manifest = _make_manifest()

        class ReadEtc:
            name = "read_etc"
            version = "1.0"

            def on_bar(self, s, p):
                with open("/etc/passwd") as f:
                    f.read()
                return []

        sandbox = StrategySandbox(ReadEtc(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_introspection_violation_caught(self) -> None:
        manifest = _make_manifest()

        class SubclassEscape:
            name = "subclass"
            version = "1.0"

            def on_bar(self, s, p):
                object.__subclasses__()
                return []

        sandbox = StrategySandbox(SubclassEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_eval_violation_caught(self) -> None:
        manifest = _make_manifest()

        class EvalEscape:
            name = "eval"
            version = "1.0"

            def on_bar(self, s, p):
                eval("1+1")  # noqa: S307
                return []

        sandbox = StrategySandbox(EvalEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_exec_violation_caught(self) -> None:
        manifest = _make_manifest()

        class ExecEscape:
            name = "exec"
            version = "1.0"

            def on_bar(self, s, p):
                exec("x = 1")  # noqa: S102
                return []

        sandbox = StrategySandbox(ExecEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_timeout_caught(self) -> None:
        manifest = _make_manifest(resources={"max_cpu_seconds": 1})

        class SlowStrat:
            name = "slow"
            version = "1.0"

            async def on_bar(self, s, p):
                await asyncio.sleep(60)
                return []

        sandbox = StrategySandbox(SlowStrat(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert "Timeout" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_builtins_restored_after_violation(self) -> None:
        manifest = _make_manifest()
        orig_import = builtins.__import__
        orig_open = builtins.open
        orig_getattr = builtins.getattr
        orig_object = builtins.object

        class CrashStrat:
            name = "crash"
            version = "1.0"

            def on_bar(self, s, p):
                raise RuntimeError("boom")

        sandbox = StrategySandbox(CrashStrat(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
        finally:
            sandbox.cleanup()
        assert builtins.__import__ is orig_import
        assert builtins.open is orig_open
        assert builtins.getattr is orig_getattr
        assert builtins.object is orig_object

    async def test_multiple_evaluations_maintain_isolation(self) -> None:
        manifest = _make_manifest()
        count = 0

        class CountStrat:
            name = "counter"
            version = "1.0"

            def on_bar(self, s, p):
                nonlocal count
                count += 1
                return []

        sandbox = StrategySandbox(CountStrat(), manifest)
        try:
            for _ in range(5):
                await sandbox.safe_evaluate(None, None, None)
            assert count == 5
            assert sandbox.metrics.total_evaluations == 5
        finally:
            sandbox.cleanup()


# ─── Cross-Layer Attack Vectors ─────────────────────────────────────────


class TestCrossLayerAttacks:
    async def test_import_via_eval_blocked(self) -> None:
        manifest = _make_manifest()

        class EvalImport:
            name = "eval_import"
            version = "1.0"

            def on_bar(self, s, p):
                eval("__import__('os')")  # noqa: S307
                return []

        sandbox = StrategySandbox(EvalImport(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_import_via_exec_blocked(self) -> None:
        manifest = _make_manifest()

        class ExecImport:
            name = "exec_import"
            version = "1.0"

            def on_bar(self, s, p):
                exec("import os")  # noqa: S102
                return []

        sandbox = StrategySandbox(ExecImport(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_globals_access_via_getattr_blocked(self) -> None:
        manifest = _make_manifest()

        class GlobalsViaGetattr:
            name = "globals_getattr"
            version = "1.0"

            def on_bar(self, s, p):
                getattr(self.on_bar, "__globals__")  # noqa: B009
                return []

        sandbox = StrategySandbox(GlobalsViaGetattr(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_filesystem_write_via_open_blocked(self) -> None:
        manifest = _make_manifest()

        class WriteViaOpen:
            name = "write_open"
            version = "1.0"

            def on_bar(self, s, p):
                with open("/tmp/escape_test", "w") as f:  # noqa: S108
                    f.write("pwned")
                return []

        sandbox = StrategySandbox(WriteViaOpen(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_compile_then_exec_blocked(self) -> None:
        manifest = _make_manifest()

        class CompileExec:
            name = "compile_exec"
            version = "1.0"

            def on_bar(self, s, p):
                code = compile("import os", "<mal>", "exec")
                exec(code)  # noqa: S102
                return []

        sandbox = StrategySandbox(CompileExec(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_mro_access_blocked(self) -> None:
        manifest = _make_manifest()

        class MroAccess:
            name = "mro"
            version = "1.0"

            def on_bar(self, s, p):
                getattr(int, "__mro__")  # noqa: B009
                return []

        sandbox = StrategySandbox(MroAccess(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()


# ─── Policy Edge Cases ─────────────────────────────────────────────────


class TestPolicyEdgeCases:
    def test_empty_blocked_modules_allows_all(self) -> None:
        policy = ImportPolicy(blocked_modules=set())
        assert policy.is_allowed("os") is True
        assert policy.is_allowed("anything") is True

    def test_empty_allowed_set_allows_all(self) -> None:
        policy = ImportPolicy(allowed_modules=set())
        assert policy.is_allowed("os") is True

    def test_network_policy_no_endpoints_blocks_all(self) -> None:
        policy = NetworkPolicy()
        assert policy.is_host_allowed("any.host") is False

    def test_network_policy_allowed_ports_default_empty(self) -> None:
        policy = NetworkPolicy()
        assert policy.allowed_ports == set()

    def test_resource_policy_defaults(self) -> None:
        policy = ResourcePolicy()
        assert policy.max_cpu_seconds == 30.0
        assert policy.max_memory_bytes == 512 * 1024 * 1024
        assert policy.max_file_descriptors == 64
        assert policy.max_threads == 1
        assert policy.wall_time_seconds == 60.0

    def test_filesystem_policy_defaults(self) -> None:
        policy = FilesystemPolicy()
        assert policy.read_only_paths == []
        assert policy.read_write_paths == []
        assert policy.virtual_root is None
        assert policy.block_symlinks is True
        assert policy.block_absolute_paths is True

    def test_introspection_policy_default_flags(self) -> None:
        policy = IntrospectionPolicy()
        assert policy.blocked_dunder_access is True
        assert policy.block_gc is True
        assert policy.block_inspect is True
        assert policy.block_frame_access is True

    def test_parse_memory_edge_cases(self) -> None:
        assert _parse_memory("0B") == 0
        assert _parse_memory("1B") == 1
        assert _parse_memory("1.5GB") == int(1.5 * 1024**3)
        assert _parse_memory("  512MB  ") == 512 * 1024**2
        assert _parse_memory("512mb") == 512 * 1024**2

    def test_sandbox_policy_default_trust_untrusted(self) -> None:
        policy = SandboxPolicy()
        assert policy.trust_level == "untrusted"
        assert policy.plugin_id == "unknown"

    def test_from_manifest_without_optional_fields(self) -> None:
        manifest = SimpleNamespace(
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "unknown"
        assert policy.network_policy.allowed_endpoints == []
        assert policy.filesystem_policy.read_only_paths == []
        assert len(policy.import_policy.blocked_modules) > 0

    def test_from_manifest_with_all_fields(self) -> None:
        manifest = SimpleNamespace(
            id="full_test",
            resources=SimpleNamespace(max_cpu_seconds=60, max_memory="1GB"),
            artifacts=["/data/weights.bin", "/data/config.yaml"],
            network=SimpleNamespace(allowed_endpoints=["api.example.com", "cdn.example.com"]),
            requires_network=lambda: True,
            dependencies={"numpy": "1.0"},
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "full_test"
        assert policy.resource_policy.max_cpu_seconds == 60
        assert policy.resource_policy.max_memory_bytes == 1024**3
        assert len(policy.network_policy.allowed_endpoints) == 2
        assert len(policy.filesystem_policy.read_only_paths) == 2


# ─── Violation Types ───────────────────────────────────────────────────


class TestViolationTypes:
    def test_all_categories_exist(self) -> None:
        categories = {c.value for c in SandboxViolationCategory}
        assert categories == {"import", "network", "resource", "filesystem", "introspection"}

    def test_import_violation_fields(self) -> None:
        v = ImportViolation("os", plugin_id="p1")
        assert v.module_name == "os"
        assert v.category == SandboxViolationCategory.IMPORT
        assert v.attempted_action == "import os"
        assert "os" in str(v)
        assert v.to_dict()["category"] == "import"

    def test_network_violation_fields(self) -> None:
        v = NetworkViolation("evil.com", port=443, plugin_id="p1")
        assert v.host == "evil.com"
        assert v.port == 443
        assert v.category == SandboxViolationCategory.NETWORK
        assert "443" in str(v)

    def test_filesystem_violation_fields(self) -> None:
        v = FilesystemViolation("/etc/passwd", "read", plugin_id="p1")
        assert v.path == "/etc/passwd"
        assert v.operation == "read"
        assert v.category == SandboxViolationCategory.FILESYSTEM

    def test_introspection_violation_fields(self) -> None:
        v = IntrospectionViolation("__globals__", plugin_id="p1")
        assert v.attribute == "__globals__"
        assert v.category == SandboxViolationCategory.INTROSPECTION
        assert "__globals__" in str(v)

    def test_resource_exhausted_fields(self) -> None:
        v = ResourceExhausted("memory", 512, 600, plugin_id="p1")
        assert v.resource_type == "memory"
        assert v.limit == 512
        assert v.current == 600
        assert v.category == SandboxViolationCategory.RESOURCE
        assert "512" in str(v)
        assert "600" in str(v)

    def test_violation_is_exception(self) -> None:
        for v in [
            ImportViolation("os"),
            NetworkViolation("evil.com"),
            FilesystemViolation("/var/tmp", "read"),  # noqa: S108
            IntrospectionViolation("__globals__"),
            ResourceExhausted("cpu", 30, 35),
        ]:
            assert isinstance(v, Exception)
            assert isinstance(v, SandboxViolation)

    def test_violation_to_dict_all_fields(self) -> None:
        v = ImportViolation("os", plugin_id="p1")
        d = v.to_dict()
        assert set(d.keys()) == {"category", "detail", "plugin_id", "attempted_action"}
        assert d["category"] == "import"
        assert d["plugin_id"] == "p1"
        assert "os" in d["detail"]
        assert d["attempted_action"] == "import os"
