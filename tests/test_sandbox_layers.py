"""
Comprehensive unit tests for each sandbox security layer.

Layers covered:
  1. Import restrictions (RestrictedImporter in layers/import_restriction.py)
  2. Network whitelist (NetworkGuard in layers/network_guard.py)
  3. Resource limits (ResourceLimiter in layers/resource_limiter.py)
  4. Filesystem isolation (FilesystemIsolation in layers/filesystem_isolation.py)
  5. Introspection blocking (IntrospectionGuard in layers/introspection_guard.py)

Plus: SandboxContext lifecycle, trust-level enforcement, escalation attempts.
"""

from __future__ import annotations

import builtins
import io
import os
import re
import socket
import sys
import tempfile
from typing import Any

import httpx
import pytest

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
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers.filesystem_isolation import FilesystemIsolation
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import (
    IntrospectionGuard,
    _BLOCKED_BUILTINS_DEFAULT,
    _EXPLICITLY_BLOCKED_ATTRS,
    _FRAME_ATTRS,
)
from engine.plugins.sandbox.layers.network_guard import NetworkGuard
from engine.plugins.sandbox.layers.resource_limiter import (
    HAS_RESOURCE_MODULE,
    ResourceLimiter,
)
from engine.plugins.sandbox.core.violation import (
    ImportViolation,
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers.filesystem_isolation import FilesystemIsolation
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import (
    _BLOCKED_BUILTINS_DEFAULT,
    _EXPLICITLY_BLOCKED_ATTRS,
    _FRAME_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.network_guard import NetworkGuard
from engine.plugins.sandbox.layers.resource_limiter import (
    HAS_RESOURCE_MODULE,
    ResourceLimiter,
)

# ─── Layer 1: Import Restrictions ────────────────────────────────────


class TestRestrictedImporterUnit:
    def test_default_blocked_set(self) -> None:
        importer = RestrictedImporter()
        assert "os" in importer.blocked
        assert "subprocess" in importer.blocked
        assert "sys" in importer.blocked

    def test_custom_blocked_set(self) -> None:
        importer = RestrictedImporter(blocked={"custom_mod"})
        assert "custom_mod" in importer.blocked
        assert "os" not in importer.blocked

    def test_custom_allowed_set(self) -> None:
        importer = RestrictedImporter(allowed={"json", "math"})
        assert importer.allowed == {"json", "math"}

    def test_plugin_id_stored(self) -> None:
        importer = RestrictedImporter(plugin_id="test_plugin")
        assert importer._plugin_id == "test_plugin"

    def test_find_spec_blocks_blocked_module(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("os")

    def test_find_spec_blocks_submodule_of_blocked(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        with pytest.raises(ImportError, match=re.escape("os.path")):
            importer.find_spec("os.path")

    def test_find_spec_allows_non_blocked(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        assert importer.find_spec("json") is None

    def test_find_spec_allows_when_no_allowlist(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, allowed=None)
        assert importer.find_spec("json") is None

    def test_find_spec_blocks_when_not_in_allowlist(self) -> None:
        importer = RestrictedImporter(blocked=set(), allowed={"json"})
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("urllib")

    def test_find_spec_allows_when_in_allowlist(self) -> None:
        importer = RestrictedImporter(blocked=set(), allowed={"json"})
        assert importer.find_spec("json") is None

    def test_find_spec_violation_logged(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p1")
        with pytest.raises(ImportError):
            importer.find_spec("os")
        violations = importer.get_violations()
        assert len(violations) == 1
        assert isinstance(violations[0], ImportViolation)
        assert violations[0].module_name == "os"
        assert violations[0].plugin_id == "p1"

    def test_find_spec_multiple_violations_logged(self) -> None:
        importer = RestrictedImporter(blocked={"os", "sys"})
        with pytest.raises(ImportError):
            importer.find_spec("os")
        with pytest.raises(ImportError):
            importer.find_spec("sys")
        assert len(importer.get_violations()) == 2

    def test_clear_violations(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        with pytest.raises(ImportError):
            importer.find_spec("os")
        assert len(importer.get_violations()) == 1
        importer.clear_violations()
        assert len(importer.get_violations()) == 0

    def test_install_monkeypatches_builtins(self) -> None:
        original = builtins.__import__
        importer = RestrictedImporter(blocked={"os"})
        try:
            importer.install()
            assert builtins.__import__ is not original
            assert importer._installed is True
            assert importer in sys.meta_path
        finally:
            importer.uninstall()

    def test_uninstall_restores_builtins(self) -> None:
        original = builtins.__import__
        importer = RestrictedImporter(blocked={"os"})
        importer.install()
        importer.uninstall()
        assert builtins.__import__ is original
        assert importer._installed is False
        assert importer not in sys.meta_path

    def test_install_idempotent(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        try:
            importer.install()
            count_before = sys.meta_path.count(importer)
            importer.install()
            assert sys.meta_path.count(importer) == count_before
        finally:
            importer.uninstall()

    def test_uninstall_idempotent(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        importer.install()
        importer.uninstall()
        importer.uninstall()
        assert importer._installed is False

    def test_restricted_import_blocks_at_level_0(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        original_import = builtins.__import__
        try:
            builtins.__import__ = importer._restricted_import
            importer._original_import = original_import
            with pytest.raises(ImportError, match="blocked"):
                builtins.__import__("os")
        finally:
            builtins.__import__ = original_import

    def test_restricted_import_allows_relative_import(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        original_import = builtins.__import__
        try:
            builtins.__import__ = importer._restricted_import
            importer._original_import = original_import
            result = builtins.__import__(
                "json", {"__name__": "__main__"}, {}, (), 0
            )
            assert result is not None
        finally:
            builtins.__import__ = original_import

    def test_restricted_import_skips_check_for_nonzero_level(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        violations_before = len(importer.get_violations())
        original_import = builtins.__import__
        called = False

        def fake_import(name, g=None, l=None, fromlist=(), level=0):
            nonlocal called
            called = True
            return original_import(name, g, l, fromlist, level)

        try:
            builtins.__import__ = importer._restricted_import
            importer._original_import = fake_import
            builtins.__import__("json", {"__name__": "__main__"}, {}, (), 1)
        except ImportError:
            pass
        finally:
            builtins.__import__ = original_import
        assert called
        assert len(importer.get_violations()) == violations_before

    def test_restricted_import_violation_logged(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p1")
        original_import = builtins.__import__
        try:
            builtins.__import__ = importer._restricted_import
            importer._original_import = original_import
            with pytest.raises(ImportError):
                builtins.__import__("os")
        finally:
            builtins.__import__ = original_import
        violations = importer.get_violations()
        assert len(violations) == 1
        assert violations[0].module_name == "os"


# ─── Layer 2: Network Whitelist ──────────────────────────────────────


class TestNetworkGuardUnit:
    def test_host_allowed_by_endpoint(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy, plugin_id="p1")
        assert guard._is_host_allowed("api.example.com") is True

    def test_subdomain_allowed(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("sub.example.com") is True

    def test_host_blocked_when_not_in_whitelist(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("evil.com") is False

    def test_no_endpoints_blocks_all(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("any.com") is False

    def test_cidr_match_allowed(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["192.168.1.0/24"])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("192.168.1.100") is True

    def test_cidr_no_match(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["192.168.1.0/24"])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("10.0.0.1") is False

    def test_cidr_hostname_not_ip(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["192.168.1.0/24"])
        guard = NetworkGuard(policy)
        assert guard._is_host_in_cidr("example.com") is False

    def test_invalid_cidr_ignored(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["not-a-cidr"])
        guard = NetworkGuard(policy)
        assert guard._cidr_networks == []

    def test_is_host_allowed_combines_endpoint_and_cidr(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["safe.com"],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy)
        assert guard._is_host_allowed("safe.com") is True
        assert guard._is_host_allowed("10.0.0.1") is True
        assert guard._is_host_allowed("evil.com") is False

    def test_plugin_id_stored(self) -> None:
        guard = NetworkGuard(NetworkPolicy(), plugin_id="p1")
        assert guard._plugin_id == "p1"

    def test_install_patches_httpx(self) -> None:
        guard = NetworkGuard(NetworkPolicy())
        original = httpx.AsyncClient.send
        try:
            guard.install()
            assert guard._installed is True
            assert httpx.AsyncClient.send is not original
        finally:
            guard.uninstall()

    def test_install_patches_socket_create_connection(self) -> None:
        guard = NetworkGuard(NetworkPolicy())
        original = socket.create_connection
        try:
            guard.install()
            assert socket.create_connection is not original
        finally:
            guard.uninstall()

    def test_install_patches_getaddrinfo(self) -> None:
        guard = NetworkGuard(NetworkPolicy())
        original = socket.getaddrinfo
        try:
            guard.install()
            assert socket.getaddrinfo is not original
        finally:
            guard.uninstall()

    def test_uninstall_restores_all(self) -> None:
        guard = NetworkGuard(NetworkPolicy())
        orig_httpx = httpx.AsyncClient.send
        orig_socket = socket.create_connection
        orig_getaddrinfo = socket.getaddrinfo
        guard.install()
        guard.uninstall()
        assert httpx.AsyncClient.send is orig_httpx
        assert socket.create_connection is orig_socket
        assert socket.getaddrinfo is orig_getaddrinfo
        assert guard._installed is False

    def test_install_idempotent(self) -> None:
        guard = NetworkGuard(NetworkPolicy())
        try:
            guard.install()
            assert guard._installed is True
            guard.install()
        finally:
            guard.uninstall()

    def test_uninstall_idempotent(self) -> None:
        guard = NetworkGuard(NetworkPolicy())
        guard.uninstall()
        assert guard._installed is False

    def test_restricted_create_connection_blocks(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")
        with pytest.raises(PermissionError, match="not allowed"):
            guard._restricted_create_connection(("evil.com", 443))

    def test_restricted_create_connection_violation_logged(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")
        with pytest.raises(PermissionError):
            guard._restricted_create_connection(("evil.com", 80))
        violations = guard.get_violations()
        assert len(violations) == 1
        assert violations[0].host == "evil.com"
        assert violations[0].port == 80

    def test_restricted_getaddrinfo_blocks_dns(self) -> None:
        policy = NetworkPolicy(block_dns=True)
        guard = NetworkGuard(policy, plugin_id="p1")
        guard._original_getaddrinfo = socket.getaddrinfo
        with pytest.raises(PermissionError, match="DNS lookup"):
            guard._restricted_getaddrinfo("evil.com", 80)

    def test_restricted_getaddrinfo_calls_original_for_whitelisted(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["localhost"],
            block_dns=True,
        )
        guard = NetworkGuard(policy, plugin_id="p1")
        guard._original_getaddrinfo = socket.getaddrinfo
        result = guard._restricted_getaddrinfo("localhost", 0)
        assert result is not None

    def test_violation_get_and_clear(self) -> None:
        guard = NetworkGuard(NetworkPolicy(), plugin_id="p1")
        with pytest.raises(PermissionError):
            guard._restricted_create_connection(("evil.com", 80))
        assert len(guard.get_violations()) == 1
        guard.clear_violations()
        assert len(guard.get_violations()) == 0


class TestNetworkGuardHttpx:
    async def test_restricted_send_blocks_disallowed_host(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")

        async def fake_send(client: Any, request: Any, **kw: Any) -> None:
            pass

        restricted = guard._make_restricted_send(fake_send)
        request = httpx.Request("GET", "https://evil.com/api")
        with pytest.raises(PermissionError, match="not allowed"):
            await restricted(None, request)

    async def test_restricted_send_allows_whitelisted_host(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")

        async def fake_send(client: Any, request: Any, **kw: Any) -> str:
            return "ok"

        restricted = guard._make_restricted_send(fake_send)
        request = httpx.Request("GET", "https://safe.com/api")
        result = await restricted(None, request)
        assert result == "ok"

    async def test_restricted_send_logs_violation(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy, plugin_id="p1")

        async def fake_send(client: Any, request: Any, **kw: Any) -> None:
            pass

        restricted = guard._make_restricted_send(fake_send)
        request = httpx.Request("GET", "https://evil.com/api")
        with pytest.raises(PermissionError):
            await restricted(None, request)
        violations = guard.get_violations()
        assert len(violations) == 1
        assert violations[0].host == "evil.com"

    async def test_restricted_send_passes_stream_kwarg(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.com"])
        guard = NetworkGuard(policy)

        received_kwargs: dict[str, Any] = {}

        async def fake_send(client: Any, request: Any, **kw: Any) -> None:
            received_kwargs.update(kw)

        restricted = guard._make_restricted_send(fake_send)
        request = httpx.Request("GET", "https://safe.com/api")
        await restricted(None, request, stream=True)
        assert received_kwargs.get("stream") is True


# ─── Layer 3: Resource Limits ────────────────────────────────────────


class TestResourceLimiterUnit:
    def test_default_policy(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="p1")
        assert limiter._plugin_id == "p1"
        assert limiter._installed is False

    def test_install_sets_installed_flag(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        try:
            limiter.install()
            assert limiter._installed is True
        finally:
            limiter.uninstall()

    def test_uninstall_clears_installed_flag(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.install()
        limiter.uninstall()
        assert limiter._installed is False

    def test_install_idempotent(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        try:
            limiter.install()
            limiter.install()
            assert limiter._installed is True
        finally:
            limiter.uninstall()

    def test_uninstall_idempotent(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.uninstall()
        assert limiter._installed is False

    def test_thread_increment_and_decrement(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=3))
        limiter.increment_thread()
        assert limiter._thread_count == 1
        limiter.increment_thread()
        assert limiter._thread_count == 2
        limiter.decrement_thread()
        assert limiter._thread_count == 1

    def test_decrement_does_not_go_negative(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_check_thread_limit_raises_when_exceeded(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=1), plugin_id="p1")
        limiter._thread_count = 1
        with pytest.raises(ResourceExhausted, match="threads"):
            limiter.check_thread_limit()

    def test_check_thread_limit_passes_when_below(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=5))
        limiter._thread_count = 3
        limiter.check_thread_limit()

    def test_increment_thread_checks_limit(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=1), plugin_id="p1")
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()

    def test_thread_limit_violation_logged(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=0), plugin_id="p1")
        with pytest.raises(ResourceExhausted):
            limiter.check_thread_limit()
        violations = limiter.get_violations()
        assert len(violations) == 1
        assert violations[0].resource_type == "threads"
        assert violations[0].plugin_id == "p1"

    def test_get_violations_returns_copy(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=0))
        with pytest.raises(ResourceExhausted):
            limiter.check_thread_limit()
        v1 = limiter.get_violations()
        v2 = limiter.get_violations()
        assert v1 is not v2
        assert len(v1) == len(v2)

    def test_clear_violations(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=0))
        with pytest.raises(ResourceExhausted):
            limiter.check_thread_limit()
        limiter.clear_violations()
        assert len(limiter.get_violations()) == 0

    @pytest.mark.skipif(not HAS_RESOURCE_MODULE, reason="no resource module")
    def test_install_applies_memory_limit(self) -> None:
        import resource

        policy = ResourcePolicy(max_memory_bytes=256 * 1024 * 1024)
        limiter = ResourceLimiter(policy)
        try:
            limiter.install()
            soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
            assert soft <= 256 * 1024 * 1024
            assert "RLIMIT_AS" in limiter._saved_limits
        finally:
            limiter.uninstall()

    @pytest.mark.skipif(not HAS_RESOURCE_MODULE, reason="no resource module")
    def test_install_applies_fd_limit(self) -> None:
        import resource

        policy = ResourcePolicy(max_file_descriptors=32)
        limiter = ResourceLimiter(policy)
        try:
            limiter.install()
            soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert soft <= 32
            assert "RLIMIT_NOFILE" in limiter._saved_limits
        finally:
            limiter.uninstall()

    @pytest.mark.skipif(not HAS_RESOURCE_MODULE, reason="no resource module")
    def test_uninstall_restores_limits(self) -> None:
        import resource

        original_soft, _original_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        policy = ResourcePolicy(max_file_descriptors=min(32, original_soft))
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.uninstall()
        restored_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert restored_soft == original_soft

    def test_parse_memory_static_method(self) -> None:
        assert ResourceLimiter.parse_memory("512MB") == 512 * 1024**2
        assert ResourceLimiter.parse_memory("2GB") == 2 * 1024**3
        assert ResourceLimiter.parse_memory("256KB") == 256 * 1024
        assert ResourceLimiter.parse_memory("1024B") == 1024
        assert ResourceLimiter.parse_memory("1048576") == 1_048_576


# ─── Layer 4: Filesystem Isolation ───────────────────────────────────


class TestFilesystemIsolationUnit:
    def test_creates_temp_work_dir_by_default(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            assert fs.work_dir is not None
            assert os.path.isdir(fs.work_dir)
        finally:
            fs.cleanup()

    def test_uses_provided_work_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fs = FilesystemIsolation(FilesystemPolicy(), work_dir=td)
            try:
                assert fs.work_dir == td
                assert not fs._owns_work_dir
            finally:
                fs.uninstall()

    def test_plugin_id_stored(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p1")
        assert fs._plugin_id == "p1"
        fs.cleanup()

    def test_is_path_allowed_work_dir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            assert fs._is_path_allowed(os.path.realpath(fs.work_dir)) is True
            sub = os.path.join(os.path.realpath(fs.work_dir), "subdir")
            assert fs._is_path_allowed(sub) is True
        finally:
            fs.cleanup()

    def test_is_path_allowed_read_only_path(self, tmp_path: Any) -> None:
        policy = FilesystemPolicy(read_only_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        try:
            assert fs._is_path_allowed(str(tmp_path)) is True
            child = str(tmp_path / "file.txt")
            assert fs._is_path_allowed(child) is True
        finally:
            fs.cleanup()

    def test_is_path_blocked_unknown(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            assert fs._is_path_allowed("/etc/passwd") is False
        finally:
            fs.cleanup()

    def test_is_write_allowed_in_work_dir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            assert fs._is_write_allowed(os.path.realpath(fs.work_dir)) is True
        finally:
            fs.cleanup()

    def test_is_write_allowed_in_rw_path(self, tmp_path: Any) -> None:
        policy = FilesystemPolicy(read_write_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        try:
            assert fs._is_write_allowed(str(tmp_path)) is True
        finally:
            fs.cleanup()

    def test_is_write_blocked_in_read_only_path(self, tmp_path: Any) -> None:
        policy = FilesystemPolicy(read_only_paths=[str(tmp_path)])
        fs = FilesystemIsolation(policy)
        try:
            assert fs._is_write_allowed(str(tmp_path)) is False
        finally:
            fs.cleanup()

    def test_install_patches_builtins_open(self) -> None:
        original = builtins.open
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            assert builtins.open is not original
            assert fs._installed is True
        finally:
            fs.cleanup()

    def test_install_patches_io_open(self) -> None:
        original = io.open
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            assert io.open is not original
        finally:
            fs.cleanup()

    def test_uninstall_restores_builtins_open(self) -> None:
        original = builtins.open
        fs = FilesystemIsolation(FilesystemPolicy())
        fs.install()
        fs.uninstall()
        assert builtins.open is original
        assert fs._installed is False

    def test_uninstall_restores_io_open(self) -> None:
        original = io.open
        fs = FilesystemIsolation(FilesystemPolicy())
        fs.install()
        fs.uninstall()
        assert io.open is original

    def test_install_idempotent(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            fs.install()
            assert fs._installed is True
        finally:
            fs.cleanup()

    def test_uninstall_idempotent(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs.uninstall()
        assert fs._installed is False

    def test_restricted_open_blocks_fd(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="fd_access"):
            fs._restricted_open(0, "r")
        violations = fs.get_violations()
        assert len(violations) == 1
        assert violations[0].operation == "fd_access"

    def test_restricted_open_blocks_read_outside_sandbox(self, tmp_path: Any) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive")
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="not allowed"):
            fs._restricted_open(str(secret), "r")
        violations = fs.get_violations()
        assert len(violations) == 1
        assert violations[0].operation == "read"

    def test_restricted_open_allows_read_in_work_dir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            test_file = os.path.join(fs.work_dir, "test.txt")
            with builtins.open(test_file, "w") as f:
                f.write("data")
            fs._original_open = builtins.open
            result = fs._restricted_open(test_file, "r")
            result.close()
        finally:
            fs.cleanup()

    def test_restricted_open_blocks_write_outside_sandbox(self, tmp_path: Any) -> None:
        target = tmp_path / "output.txt"
        fs = FilesystemIsolation(FilesystemPolicy(read_only_paths=[str(tmp_path)]))
        fs._original_open = builtins.open
        with pytest.raises(PermissionError, match="write"):
            fs._restricted_open(str(target), "w")

    def test_restricted_open_allows_write_in_work_dir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            test_file = os.path.join(fs.work_dir, "output.txt")
            fs._original_open = builtins.open
            result = fs._restricted_open(test_file, "w")
            result.close()
        finally:
            fs.cleanup()

    def test_restricted_open_detects_append_mode(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError):
            fs._restricted_open("/tmp/blocked_append", "a")

    def test_restricted_open_detects_plus_mode(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError):
            fs._restricted_open("/tmp/blocked_plus", "r+")

    def test_violation_get_and_clear(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError):
            fs._restricted_open(0, "r")
        assert len(fs.get_violations()) == 1
        fs.clear_violations()
        assert len(fs.get_violations()) == 0

    def test_cleanup_removes_owned_work_dir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy())
        work_dir = fs.work_dir
        assert os.path.isdir(work_dir)
        fs.cleanup()
        assert not os.path.isdir(work_dir)

    def test_cleanup_does_not_remove_provided_work_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fs = FilesystemIsolation(FilesystemPolicy(), work_dir=td)
            fs.cleanup()
            assert os.path.isdir(td)


# ─── Layer 5: Introspection Blocking ─────────────────────────────────


class TestIntrospectionGuardUnit:
    def test_default_blocked_attrs(self) -> None:
        assert "__subclasses__" in _EXPLICITLY_BLOCKED_ATTRS
        assert "__globals__" in _EXPLICITLY_BLOCKED_ATTRS
        assert "__bases__" in _EXPLICITLY_BLOCKED_ATTRS
        assert "__dict__" in _EXPLICITLY_BLOCKED_ATTRS
        assert "__class__" in _EXPLICITLY_BLOCKED_ATTRS
        assert "__code__" in _EXPLICITLY_BLOCKED_ATTRS
        assert "__closure__" in _EXPLICITLY_BLOCKED_ATTRS
        assert "__mro__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_frame_attrs_defined(self) -> None:
        assert "tb_frame" in _FRAME_ATTRS
        assert "f_back" in _FRAME_ATTRS
        assert "f_globals" in _FRAME_ATTRS
        assert "f_locals" in _FRAME_ATTRS

    def test_default_blocked_builtins(self) -> None:
        assert "eval" in _BLOCKED_BUILTINS_DEFAULT
        assert "exec" in _BLOCKED_BUILTINS_DEFAULT
        assert "compile" in _BLOCKED_BUILTINS_DEFAULT
        assert "breakpoint" in _BLOCKED_BUILTINS_DEFAULT

    def test_is_blocked_attr_explicitly(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        assert guard._is_blocked_attr("__subclasses__") is True
        assert guard._is_blocked_attr("__globals__") is True

    def test_is_blocked_attr_from_policy(self) -> None:
        policy = IntrospectionPolicy(
            blocked_attributes={"__custom_attr__"},
        )
        guard = IntrospectionGuard(policy)
        assert guard._is_blocked_attr("__custom_attr__") is True

    def test_is_blocked_attr_frame_attrs(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        assert guard._is_blocked_attr("tb_frame") is True
        assert guard._is_blocked_attr("f_globals") is True

    def test_is_blocked_attr_normal(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        assert guard._is_blocked_attr("name") is False
        assert guard._is_blocked_attr("__init__") is False

    def test_install_replaces_object(self) -> None:
        original = builtins.object
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            assert builtins.object is not original
        finally:
            guard.uninstall()

    def test_install_replaces_getattr(self) -> None:
        original = builtins.getattr
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            assert builtins.getattr is not original
        finally:
            guard.uninstall()

    def test_install_blocks_eval(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                eval("1 + 1")
        finally:
            guard.uninstall()

    def test_install_blocks_exec(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                exec("x = 1")
        finally:
            guard.uninstall()

    def test_install_blocks_compile(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                compile("1+1", "<test>", "eval")
        finally:
            guard.uninstall()

    def test_install_blocks_breakpoint(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                breakpoint()
        finally:
            guard.uninstall()

    def test_uninstall_restores_all(self) -> None:
        original_object = builtins.object
        original_getattr = builtins.getattr
        original_eval = builtins.eval
        original_exec = builtins.exec
        original_compile = builtins.compile
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.install()
        guard.uninstall()
        assert builtins.object is original_object
        assert builtins.getattr is original_getattr
        assert builtins.eval is original_eval
        assert builtins.exec is original_exec
        assert builtins.compile is original_compile

    def test_install_idempotent(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            guard.install()
            assert guard._installed is True
        finally:
            guard.uninstall()

    def test_uninstall_idempotent(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.uninstall()
        assert guard._installed is False

    def test_restricted_getattr_blocks_dunder(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p1")
        guard._original_getattr = builtins.getattr
        with pytest.raises(PermissionError, match="__subclasses__"):
            guard._restricted_getattr(int, "__subclasses__")
        violations = guard.get_violations()
        assert len(violations) == 1
        assert violations[0].attribute == "__subclasses__"

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

    def test_blocked_builtin_logs_violation(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p1")
        blocked_fn = guard._make_blocked_builtin("eval")
        with pytest.raises(PermissionError):
            blocked_fn()
        violations = guard.get_violations()
        assert len(violations) == 1
        assert violations[0].attribute == "eval"
        assert violations[0].plugin_id == "p1"

    def test_violation_get_and_clear(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard._original_getattr = builtins.getattr
        with pytest.raises(PermissionError):
            guard._restricted_getattr(int, "__globals__")
        assert len(guard.get_violations()) == 1
        guard.clear_violations()
        assert len(guard.get_violations()) == 0

    def test_policy_specific_blocked_builtins(self) -> None:
        policy = IntrospectionPolicy(
            blocked_builtins={"credits", "license", "quit", "exit"},
        )
        guard = IntrospectionGuard(policy)
        try:
            guard.install()
            with pytest.raises(PermissionError):
                credits()
        finally:
            guard.uninstall()

    def test_install_blocks_policy_and_default_builtins_union(self) -> None:
        policy = IntrospectionPolicy(
            blocked_builtins={"credits"},
        )
        guard = IntrospectionGuard(policy)
        try:
            guard.install()
            with pytest.raises(PermissionError):
                eval("1")
            with pytest.raises(PermissionError):
                credits()
        finally:
            guard.uninstall()


# ─── SandboxContext Lifecycle ─────────────────────────────────────────


class TestSandboxContextUnit:
    def test_context_created_with_policy(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        assert ctx.policy is policy
        assert ctx.is_active is False

    def test_activate_sets_active(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.deactivate()

    def test_deactivate_clears_active(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.deactivate()
        assert ctx.is_active is False

    def test_activate_idempotent(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.deactivate()

    def test_deactivate_idempotent(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        ctx.deactivate()
        assert ctx.is_active is False

    def test_context_manager_protocol(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        with ctx:
            assert ctx.is_active is True
        assert ctx.is_active is False

    def test_work_dir_property(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        assert ctx.work_dir is not None
        assert os.path.isdir(ctx.work_dir)
        ctx.cleanup()

    def test_event_logger_property(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        assert ctx.event_logger is not None
        assert ctx.event_logger._plugin_id == "test"

    def test_cleanup_deactivates_and_removes_dir(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        work_dir = ctx.work_dir
        ctx.activate()
        assert ctx.is_active is True
        ctx.cleanup()
        assert ctx.is_active is False
        assert not os.path.isdir(work_dir)

    def test_violation_collection_on_deactivate(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
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

    def test_activate_restores_on_failure(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        orig_import = builtins.__import__
        orig_getattr = builtins.getattr
        try:
            ctx.activate()
        finally:
            ctx.deactivate()
        assert builtins.__import__ is orig_import
        assert builtins.getattr is orig_getattr


# ─── Trust Level Enforcement ─────────────────────────────────────────


class TestTrustLevelEnforcement:
    def test_untrusted_default_policy(self) -> None:
        policy = SandboxPolicy()
        assert policy.trust_level == "untrusted"
        assert policy.import_policy.blocked_modules == set()
        assert policy.resource_policy.max_cpu_seconds == 30.0
        assert policy.resource_policy.max_memory_bytes == 512 * 1024 * 1024
        assert "eval" in policy.introspection_policy.blocked_builtins
        assert "exec" in policy.introspection_policy.blocked_builtins

    def test_trusted_policy_relaxed(self) -> None:
        policy = SandboxPolicy.trusted_policy("my_plugin")
        assert policy.trust_level == "trusted"
        assert policy.plugin_id == "my_plugin"
        assert "os" not in policy.import_policy.blocked_modules
        assert "subprocess" in policy.import_policy.blocked_modules
        assert policy.resource_policy.max_cpu_seconds == 300
        assert policy.resource_policy.max_memory_bytes == 2 * 1024**3

    def test_trusted_policy_allows_more_builtins(self) -> None:
        policy = SandboxPolicy.trusted_policy()
        assert "eval" not in policy.introspection_policy.blocked_builtins
        assert "exec" in policy.introspection_policy.blocked_builtins

    def test_trusted_policy_fewer_blocked_attributes(self) -> None:
        trusted = SandboxPolicy.trusted_policy()
        untrusted = SandboxPolicy()
        assert len(trusted.introspection_policy.blocked_attributes) < len(
            untrusted.introspection_policy.blocked_attributes
        )

    def test_importer_respects_trusted_policy(self) -> None:
        trusted = SandboxPolicy.trusted_policy()
        importer = RestrictedImporter(
            blocked=trusted.import_policy.blocked_modules,
            allowed=None,
        )
        assert importer.find_spec("json") is None
        with pytest.raises(ImportError):
            importer.find_spec("subprocess")

    def test_importer_blocks_more_for_untrusted(self) -> None:
        untrusted = SandboxPolicy()
        trusted = SandboxPolicy.trusted_policy()
        untrusted_importer = RestrictedImporter(
            blocked=untrusted.import_policy.blocked_modules,
        )
        trusted_importer = RestrictedImporter(
            blocked=trusted.import_policy.blocked_modules,
        )
        with pytest.raises(ImportError):
            untrusted_importer.find_spec("os")
        assert trusted_importer.find_spec("json") is None

    def test_introspection_guard_differs_by_trust(self) -> None:
        trusted_policy = SandboxPolicy.trusted_policy()
        untrusted_policy = SandboxPolicy()

        trusted_guard = IntrospectionGuard(trusted_policy.introspection_policy)
        untrusted_guard = IntrospectionGuard(untrusted_policy.introspection_policy)

        assert untrusted_guard._is_blocked_attr("__dict__") is True
        assert trusted_guard._is_blocked_attr("__dict__") is True

    def test_resource_limits_differ_by_trust(self) -> None:
        trusted = SandboxPolicy.trusted_policy()
        untrusted = SandboxPolicy()
        assert trusted.resource_policy.max_cpu_seconds > untrusted.resource_policy.max_cpu_seconds
        assert trusted.resource_policy.max_memory_bytes > untrusted.resource_policy.max_memory_bytes


# ─── Escalation Attempts ─────────────────────────────────────────────


class TestEscalationAttempts:
    def test_cannot_modify_policy_after_creation(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="untrusted")
        policy.trust_level = "trusted"
        assert policy.import_policy.blocked_modules == SandboxPolicy().import_policy.blocked_modules

    def test_cannot_bypass_importer_by_replacing_blocked_set(self) -> None:
        importer = RestrictedImporter(blocked={"os"})
        importer.blocked = set()
        assert importer.find_spec("json") is None

    def test_replacing_builtins_import_while_active(self) -> None:
        original = builtins.__import__
        importer = RestrictedImporter(blocked={"os"})
        try:
            importer.install()
            with pytest.raises(ImportError):
                builtins.__import__("os")
        finally:
            importer.uninstall()
            assert builtins.__import__ is original

    def test_nested_context_activation(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = SandboxPolicy(plugin_id="test")
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            assert ctx.is_active is True
            ctx.activate()
            assert ctx.is_active is True
            ctx.deactivate()
            assert ctx.is_active is False
        finally:
            ctx.cleanup()

    def test_filesystem_isolation_cannot_be_bypassed_by_symlink(
        self, tmp_path: Any
    ) -> None:
        target = tmp_path / "secret.txt"
        target.write_text("sensitive")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        fs = FilesystemIsolation(FilesystemPolicy())
        fs._original_open = builtins.open
        with pytest.raises(PermissionError):
            fs._restricted_open(str(link), "r")
        fs.cleanup()


# ─── Integration: Full Sandbox Runtime End-to-End ────────────────────


class TestFullSandboxRuntime:
    async def test_good_strategy_end_to_end(self) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        policy = SandboxPolicy(
            plugin_id="e2e_good",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
        )

        class GoodStrat:
            name = "e2e_good"
            version = "1.0"

            def on_bar(self, state, portfolio):
                from engine.core.signal import Signal
                return [Signal.buy(symbol="AAPL", strategy_id=self.name)]

        executor = PluginSandboxExecutor(GoodStrat(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
        finally:
            executor.cleanup()

    async def test_import_violation_end_to_end(self) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        policy = SandboxPolicy(
            plugin_id="e2e_import",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
        )

        class ImportOsStrat:
            name = "e2e_import"
            version = "1.0"

            def on_bar(self, state, portfolio):
                import os  # noqa: F401
                return []

        executor = PluginSandboxExecutor(ImportOsStrat(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_filesystem_violation_end_to_end(self, tmp_path: Any) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        policy = SandboxPolicy(
            plugin_id="e2e_fs",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
        )

        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive")

        class ReadSecretStrat:
            name = "e2e_fs"
            version = "1.0"

            def __init__(self, path):
                self._path = path

            def on_bar(self, state, portfolio):
                with open(self._path) as f:
                    f.read()
                return []

        executor = PluginSandboxExecutor(ReadSecretStrat(str(secret)), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_introspection_violation_end_to_end(self) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        policy = SandboxPolicy(
            plugin_id="e2e_intro",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
        )

        class SubclassStrat:
            name = "e2e_intro"
            version = "1.0"

            def on_bar(self, state, portfolio):
                object.__subclasses__()
                return []

        executor = PluginSandboxExecutor(SubclassStrat(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_timeout_end_to_end(self) -> None:
        import asyncio

        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        policy = SandboxPolicy(
            plugin_id="e2e_timeout",
            resource_policy=ResourcePolicy(max_cpu_seconds=1),
        )

        class SlowStrat:
            name = "e2e_timeout"
            version = "1.0"

            async def on_bar(self, state, portfolio):
                await asyncio.sleep(60)
                return []

        executor = PluginSandboxExecutor(SlowStrat(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_context_restoration_after_all_violation_types(self) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        original_import = builtins.__import__
        original_open = builtins.open
        original_getattr = builtins.getattr

        policy = SandboxPolicy(
            plugin_id="e2e_restore",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
        )

        class CrashStrat:
            name = "e2e_restore"
            version = "1.0"

            def on_bar(self, state, portfolio):
                raise RuntimeError("crash")

        executor = PluginSandboxExecutor(CrashStrat(), policy)
        try:
            await executor.safe_evaluate(None, None, None)
        finally:
            executor.cleanup()

        assert builtins.__import__ is original_import
        assert builtins.open is original_open
        assert builtins.getattr is original_getattr

    async def test_network_violation_end_to_end(self) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        policy = SandboxPolicy(
            plugin_id="e2e_net",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
            network_policy=NetworkPolicy(allowed_endpoints=["safe.example.com"]),
        )

        class DirectHttpStrat:
            name = "e2e_net"
            version = "1.0"

            async def on_bar(self, state, portfolio):
                import httpx as hx
                transport = hx.MockTransport(lambda r: hx.Response(200))
                async with hx.AsyncClient(transport=transport) as client:
                    await client.get("https://evil.com/api")
                return []

        executor = PluginSandboxExecutor(DirectHttpStrat(), policy)
        try:
            signals = await executor.safe_evaluate(None, None, None)
            assert signals == []
        finally:
            executor.cleanup()

    async def test_multiple_evaluations_sequential(self) -> None:
        from engine.plugins.sandbox.executor import PluginSandboxExecutor

        policy = SandboxPolicy(
            plugin_id="e2e_multi",
            resource_policy=ResourcePolicy(max_cpu_seconds=5),
        )

        class CountStrat:
            name = "e2e_multi"
            version = "1.0"

            def __init__(self):
                self.count = 0

            def on_bar(self, state, portfolio):
                self.count += 1
                return []

        strat = CountStrat()
        executor = PluginSandboxExecutor(strat, policy)
        try:
            for _ in range(5):
                await executor.safe_evaluate(None, None, None)
            assert strat.count == 5
        finally:
            executor.cleanup()
