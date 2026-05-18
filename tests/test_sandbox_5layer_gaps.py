"""
Targeted tests for remaining sandbox 5-layer security gaps.

Addresses gaps identified by comparing existing test coverage against
the implementation of all 5 security layers:
  - Layer 1: Import Restrictions
  - Layer 2: Network Whitelist
  - Layer 3: Resource Limits
  - Layer 4: Filesystem Isolation
  - Layer 5: Introspection Blocking

Plus cross-layer integration, trust-level boundary conditions, and
SandboxContext orchestration edge cases.
"""

from __future__ import annotations

import builtins
import contextlib
import os
import socket
import tempfile
import time
from unittest.mock import MagicMock

import httpx
import pytest

from engine.plugins.restricted_importer import BLOCKED_MODULES
from engine.plugins.sandbox.core.context import (
    _MAX_CPU_SECONDS_LIMITED,
    _MAX_CPU_SECONDS_UNTRUSTED,
    _MIN_BLOCKED_MODULES_LIMITED,
    _MIN_BLOCKED_MODULES_UNTRUSTED,
    SandboxContext,
)
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import (
    IntrospectionViolation,
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.sandbox.layers.filesystem_isolation import (
    _BLOCKED_SYSTEM_PREFIXES,
    FilesystemIsolation,
)
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import (
    _EXPLICITLY_BLOCKED_ATTRS,
    _FRAME_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.network_guard import (
    NetworkGuard,
    _is_private_ip,
)
from engine.plugins.sandbox.layers.resource_limiter import (
    ResourceLimiter,
    _WallTimer,
)
from engine.plugins.trust_levels import TrustLevel

# ═══════════════════════════════════════════════════════════════════════
# Layer 4: Filesystem Isolation — Gap Tests
# ═══════════════════════════════════════════════════════════════════════


class TestFilesystemNullBytePaths:
    def test_null_byte_in_path_raises(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises((PermissionError, ValueError)):
                builtins.open("/tmp/test\x00evil.txt")
        finally:
            fs.uninstall()

    def test_null_byte_does_not_bypass_sandbox(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises((PermissionError, ValueError)):
                builtins.open("/tmp/\x00hidden")
        finally:
            fs.uninstall()


class TestFilesystemUnicodePaths:
    def test_unicode_path_outside_sandbox_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/tmp/über_secret_文件.txt")
        finally:
            fs.uninstall()
        violations = fs.get_violations()
        assert len(violations) == 1

    def test_unicode_path_inside_workdir_allowed(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        work = fs.work_dir
        fs.install()
        try:
            unicode_path = os.path.join(work, "données.csv")
            builtins.open(unicode_path, "w").close()
            assert os.path.exists(unicode_path)
        finally:
            fs.uninstall()
        fs.cleanup()

    def test_unicode_path_write_in_workdir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        work = fs.work_dir
        fs.install()
        try:
            path = os.path.join(work, "результат.txt")
            with builtins.open(path, "w") as f:
                f.write("test data")
            assert os.path.exists(path)
        finally:
            fs.uninstall()
        fs.cleanup()


class TestFilesystemLongPaths:
    def test_very_long_path_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        long_component = "a" * 300
        path = f"/tmp/{long_component}/file.txt"
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open(path)
        finally:
            fs.uninstall()
        assert len(fs.get_violations()) == 1

    def test_long_path_in_workdir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        work = fs.work_dir
        long_name = "x" * 200
        path = os.path.join(work, long_name + ".txt")
        fs.install()
        try:
            with builtins.open(path, "w") as f:
                f.write("ok")
            assert os.path.exists(path)
        finally:
            fs.uninstall()
        fs.cleanup()


class TestFilesystemSystemPaths:
    @pytest.mark.parametrize("prefix", _BLOCKED_SYSTEM_PREFIXES)
    def test_system_prefix_read_blocked(self, prefix: str) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open(os.path.join(prefix, "test"))
        finally:
            fs.uninstall()
        violations = fs.get_violations()
        assert len(violations) >= 1

    def test_proc_self_status_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/proc/self/status")
        finally:
            fs.uninstall()

    def test_sys_kernel_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/sys/kernel/notes")
        finally:
            fs.uninstall()

    def test_dev_null_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/dev/null", "w")
        finally:
            fs.uninstall()


class TestFilesystemValidatePathDirect:
    def test_path_traversal_with_backslash(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        with pytest.raises(PermissionError, match="path_traversal"):
            fs._validate_path("/tmp/..\\etc/passwd")
        assert len(fs.get_violations()) == 1

    def test_validate_path_allows_normal_path(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        result = fs._validate_path(os.path.join(fs.work_dir, "file.txt"))
        assert isinstance(result, str)

    def test_multiple_path_traversal_components(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        with pytest.raises(PermissionError, match="path_traversal"):
            fs._validate_path("/tmp/foo/../../etc/passwd")


class TestFilesystemFdAccess:
    def test_fd_zero_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open(0)
        finally:
            fs.uninstall()
        violations = fs.get_violations()
        assert any(v.operation == "fd_access" for v in violations)

    def test_fd_one_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open(1)
        finally:
            fs.uninstall()
        assert any(v.operation == "fd_access" for v in fs.get_violations())

    def test_fd_two_blocked(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open(2)
        finally:
            fs.uninstall()
        assert any(v.operation == "fd_access" for v in fs.get_violations())


class TestFilesystemReadOnlyPaths:
    def test_read_only_allows_subdirectory_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "data")
            os.makedirs(subdir)
            file_path = os.path.join(subdir, "file.txt")
            with open(file_path, "w") as f:
                f.write("hello")

            fs = FilesystemIsolation(
                FilesystemPolicy(read_only_paths=[tmpdir]),
                plugin_id="p",
            )
            fs.install()
            try:
                with builtins.open(file_path) as f:
                    assert f.read() == "hello"
            finally:
                fs.uninstall()

    def test_read_only_blocks_write_in_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "data")
            os.makedirs(subdir)
            file_path = os.path.join(subdir, "file.txt")

            fs = FilesystemIsolation(
                FilesystemPolicy(read_only_paths=[tmpdir]),
                plugin_id="p",
            )
            fs.install()
            try:
                with pytest.raises(PermissionError):
                    builtins.open(file_path, "w")
            finally:
                fs.uninstall()
            assert any(v.operation == "write" for v in fs.get_violations())


class TestFilesystemReadWritePaths:
    def test_rw_path_allows_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "output.txt")
            fs = FilesystemIsolation(
                FilesystemPolicy(read_write_paths=[tmpdir]),
                plugin_id="p",
            )
            fs.install()
            try:
                with builtins.open(file_path, "w") as f:
                    f.write("data")
                assert os.path.exists(file_path)
            finally:
                fs.uninstall()

    def test_rw_path_allows_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "input.txt")
            with open(file_path, "w") as f:
                f.write("hello")
            fs = FilesystemIsolation(
                FilesystemPolicy(read_write_paths=[tmpdir]),
                plugin_id="p",
            )
            fs.install()
            try:
                with builtins.open(file_path) as f:
                    assert f.read() == "hello"
            finally:
                fs.uninstall()


class TestFilesystemSequentialOps:
    def test_multiple_reads_in_workdir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        work = fs.work_dir
        for name in ["a.txt", "b.txt", "c.txt"]:
            with open(os.path.join(work, name), "w") as f:
                f.write(name)
        fs.install()
        try:
            for name in ["a.txt", "b.txt", "c.txt"]:
                with builtins.open(os.path.join(work, name)) as f:
                    assert f.read() == name
        finally:
            fs.uninstall()
        fs.cleanup()

    def test_mixed_read_write_in_workdir(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        work = fs.work_dir
        fs.install()
        try:
            path = os.path.join(work, "data.txt")
            with builtins.open(path, "w") as f:
                f.write("initial")
            with builtins.open(path) as f:
                assert f.read() == "initial"
            with builtins.open(path, "a") as f:
                f.write("_appended")
            with builtins.open(path) as f:
                assert f.read() == "initial_appended"
        finally:
            fs.uninstall()
        fs.cleanup()

    def test_violations_accumulate_across_ops(self) -> None:
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            for _ in range(3):
                with pytest.raises(PermissionError):
                    builtins.open("/etc/passwd")
        finally:
            fs.uninstall()
        assert len(fs.get_violations()) == 3


class TestFilesystemIoOpen:
    def test_io_open_patched_too(self) -> None:
        import io as io_mod

        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                io_mod.open("/etc/passwd", "r")
        finally:
            fs.uninstall()

    def test_io_open_restored(self) -> None:
        import io as io_mod

        original = io_mod.open
        fs = FilesystemIsolation(FilesystemPolicy(), plugin_id="p")
        fs.install()
        assert io_mod.open is not original
        fs.uninstall()
        assert io_mod.open is original


# ═══════════════════════════════════════════════════════════════════════
# Layer 5: Introspection Blocking — Gap Tests
# ═══════════════════════════════════════════════════════════════════════


class TestIntrospectionRestrictedObject:
    def test_restricted_object_not_used(self) -> None:
        original = builtins.object
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            assert builtins.object is original
        finally:
            guard.uninstall()
        assert builtins.object is original

    def test_isinstance_still_works(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            assert isinstance(42, int)
            assert isinstance("hello", str)
            assert not isinstance(42, str)
        finally:
            guard.uninstall()


class TestIntrospectionBlockedDunders:
    @pytest.mark.parametrize(
        "attr",
        [
            "__init_subclass__",
            "__instancecheck__",
            "__subclasscheck__",
            "__reduce__",
            "__reduce_ex__",
            "__getstate__",
            "__setstate__",
        ],
    )
    def test_explicitly_blocked_dunder_attrs(self, attr: str) -> None:
        assert attr in _EXPLICITLY_BLOCKED_ATTRS

    def test_getattr_blocked_for_init_subclass(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr(int, "__init_subclass__")
        finally:
            guard.uninstall()

    def test_getattr_blocked_for_reduce(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr("hello", "__reduce__")
        finally:
            guard.uninstall()

    def test_getattr_blocked_for_reduce_ex(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr([], "__reduce_ex__")
        finally:
            guard.uninstall()

    def test_getattr_blocked_for_getstate(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr({}, "__getstate__")
        finally:
            guard.uninstall()


class TestIntrospectionTypeBuiltin:
    def test_type_builtin_allowed(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            assert builtins.type(42) is int
            assert builtins.type("hello") is str
        finally:
            guard.uninstall()

    def test_type_can_create_class(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            MyCls = builtins.type("MyCls", (), {"x": 1})
            assert MyCls.x == 1
        finally:
            guard.uninstall()


class TestIntrospectionSafeAttrs:
    def test_dunder_name_allowed(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            result = builtins.getattr(int, "__name__")
            assert result == "int"
        finally:
            guard.uninstall()

    def test_dunder_doc_allowed(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            result = builtins.getattr(str, "__doc__")
            assert isinstance(result, str)
        finally:
            guard.uninstall()

    def test_dunder_len_allowed(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            result = builtins.getattr([1, 2, 3], "__len__")
            assert result() == 3
        finally:
            guard.uninstall()

    def test_dunder_str_allowed(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            result = builtins.getattr(42, "__str__")
            assert result() == "42"
        finally:
            guard.uninstall()

    def test_dunder_hash_allowed(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            builtins.getattr(42, "__hash__")
        finally:
            guard.uninstall()


class TestIntrospectionFrameAccess:
    def test_frame_attrs_in_blocked_set(self) -> None:
        for attr in ("tb_frame", "f_back", "f_builtins", "f_code", "f_globals", "f_locals"):
            assert attr in _FRAME_ATTRS

    def test_traceback_attrs_blocked(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            for attr in ("__traceback__", "__context__", "__cause__"):
                with pytest.raises(PermissionError):
                    builtins.getattr(ValueError("x"), attr)
        finally:
            guard.uninstall()

    def test_setattr_blocks_frame_write(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.setattr(MagicMock(), "f_globals", {})
        finally:
            guard.uninstall()

    def test_setattr_blocks_traceback_write(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.setattr(MagicMock(), "__traceback__", None)
        finally:
            guard.uninstall()

    def test_setattr_allows_normal_attr(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")

        class Obj:
            x = 1

        obj = Obj()
        guard.install()
        try:
            builtins.setattr(obj, "x", 2)
            assert obj.x == 2
        finally:
            guard.uninstall()

    def test_block_frame_access_false_allows_frame_attrs(self) -> None:
        policy = IntrospectionPolicy(block_frame_access=False)
        guard = IntrospectionGuard(policy, plugin_id="p")
        assert not guard._is_blocked_attr("f_globals")
        assert not guard._is_blocked_attr("tb_frame")
        assert not guard._is_blocked_attr("__traceback__")


class TestIntrospectionSafeDir:
    def test_dir_filters_blocked_attrs(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            result = builtins.dir(int)
            for attr in _EXPLICITLY_BLOCKED_ATTRS:
                assert attr not in result
        finally:
            guard.uninstall()

    def test_dir_filters_frame_attrs(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            result = builtins.dir(int)
            for attr in _FRAME_ATTRS:
                assert attr not in result
        finally:
            guard.uninstall()

    def test_dir_keeps_safe_attrs(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            result = builtins.dir(str)
            assert "__len__" in result
            assert "__str__" in result
            assert "__repr__" in result
        finally:
            guard.uninstall()

    def test_dir_with_custom_blocked_attrs(self) -> None:
        policy = IntrospectionPolicy(blocked_attributes={"custom_secret"})
        guard = IntrospectionGuard(policy, plugin_id="p")
        guard.install()
        try:
            result = builtins.dir(str)
            assert "custom_secret" not in result
            assert "__len__" in result
        finally:
            guard.uninstall()


class TestIntrospectionViolationAccumulation:
    def test_multiple_violations_accumulate(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            for _ in range(5):
                with pytest.raises(PermissionError):
                    builtins.getattr(int, "__subclasses__")
        finally:
            guard.uninstall()
        assert len(guard.get_violations()) == 5
        for v in guard.get_violations():
            assert isinstance(v, IntrospectionViolation)
            assert v.attribute == "__subclasses__"

    def test_violations_from_different_attrs(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr(int, "__subclasses__")
            with pytest.raises(PermissionError):
                builtins.getattr(int, "__bases__")
            with pytest.raises(PermissionError):
                builtins.getattr(int, "__globals__" if hasattr(int, "__globals__") else "__code__")
        finally:
            guard.uninstall()
        violations = guard.get_violations()
        assert len(violations) >= 2
        attrs = {v.attribute for v in violations}
        assert "__subclasses__" in attrs
        assert "__bases__" in attrs

    def test_violations_clear_resets(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.getattr(int, "__subclasses__")
        finally:
            guard.uninstall()
        assert len(guard.get_violations()) == 1
        guard.clear_violations()
        assert len(guard.get_violations()) == 0


class TestIntrospectionBuiltinBlocking:
    @pytest.mark.parametrize("name", ["eval", "exec", "breakpoint", "vars", "globals", "locals"])
    def test_blocked_builtin_raises(self, name: str) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            blocked_fn = getattr(builtins, name, None)
            if blocked_fn is not None:
                with pytest.raises(PermissionError):
                    blocked_fn("test" if name in ("eval", "exec") else None)
        finally:
            guard.uninstall()

    def test_compile_is_whitelisted(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            code = compile("1+1", "<test>", "eval")
            assert code is not None
        finally:
            guard.uninstall()

    def test_blocked_builtin_logs_violation(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.eval("1+1")
        finally:
            guard.uninstall()
        violations = guard.get_violations()
        assert any(v.attribute == "eval" for v in violations)

    def test_custom_blocked_builtins(self) -> None:
        policy = IntrospectionPolicy(blocked_builtins={"print", "open"})
        guard = IntrospectionGuard(policy, plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                builtins.print("test")
        finally:
            guard.uninstall()

    def test_all_builtins_restored_on_uninstall(self) -> None:
        original_eval = builtins.eval
        original_exec = builtins.exec
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p")
        guard.install()
        guard.uninstall()
        assert builtins.eval is original_eval
        assert builtins.exec is original_exec


# ═══════════════════════════════════════════════════════════════════════
# Layer 3: Resource Limits — Gap Tests
# ═══════════════════════════════════════════════════════════════════════


class TestWallTimer:
    def test_wall_timer_expired_property(self) -> None:
        timer = _WallTimer(0.05, plugin_id="p")
        assert not timer.expired
        timer.start()
        time.sleep(0.1)
        assert timer.expired
        timer.stop()

    def test_wall_timer_check_raises_resource_exhausted(self) -> None:
        timer = _WallTimer(0.01, plugin_id="p")
        timer.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            timer.check()
        assert exc_info.value.resource_type == "wall_time"
        assert exc_info.value.plugin_id == "p"

    def test_wall_timer_elapsed_increases(self) -> None:
        timer = _WallTimer(10.0)
        timer.start()
        e1 = timer.elapsed
        time.sleep(0.05)
        e2 = timer.elapsed
        assert e2 > e1
        timer.stop()

    def test_wall_timer_stop_cancels(self) -> None:
        timer = _WallTimer(0.01)
        timer.start()
        timer.stop()
        time.sleep(0.05)
        assert not timer.expired


class TestResourceLimiterWallTime:
    def test_install_starts_wall_timer(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.install()
        assert limiter._wall_timer is not None
        limiter.uninstall()

    def test_uninstall_stops_wall_timer(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.install()
        limiter.uninstall()
        assert limiter._wall_timer is None

    def test_check_wall_timer_raises(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.install()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.check_wall_timer()
        assert exc_info.value.resource_type == "wall_time"
        limiter.uninstall()

    def test_wall_time_violation_fields(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="test-plug")
        limiter.install()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.check_wall_timer()
        v = exc_info.value
        assert v.resource_type == "wall_time"
        assert v.plugin_id == "test-plug"
        assert v.limit == 0.01
        assert v.current > 0.01
        assert v.category == SandboxViolationCategory.RESOURCE
        limiter.uninstall()


class TestResourceLimiterCpuTimer:
    def test_cpu_elapsed_property(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.install()
        elapsed = limiter.cpu_elapsed
        assert elapsed >= 0
        limiter.uninstall()

    def test_cpu_elapsed_without_install(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="p")
        assert limiter.cpu_elapsed == 0.0


class TestResourceThreadLimits:
    def test_thread_limit_boundary(self) -> None:
        policy = ResourcePolicy(max_threads=2)
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.increment_thread()
        assert limiter._thread_count == 1
        limiter.increment_thread()
        assert limiter._thread_count == 2
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()

    def test_decrement_at_zero_stays_zero(self) -> None:
        policy = ResourcePolicy(max_threads=1)
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.decrement_thread()
        assert limiter._thread_count == 0
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_thread_limit_zero(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="p")
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()

    def test_thread_limit_one_allows_single(self) -> None:
        policy = ResourcePolicy(max_threads=1)
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        limiter.decrement_thread()
        limiter.increment_thread()

    def test_thread_violation_logged(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="p")
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        assert len(limiter.get_violations()) == 1
        assert limiter.get_violations()[0].resource_type == "threads"


class TestResourceMemoryParseEdgeCases:
    def test_parse_memory_float_mb(self) -> None:
        assert ResourceLimiter.parse_memory("1.5MB") == int(1.5 * 1024**2)

    def test_parse_memory_float_gb(self) -> None:
        assert ResourceLimiter.parse_memory("0.5GB") == int(0.5 * 1024**3)

    def test_parse_memory_just_number(self) -> None:
        assert ResourceLimiter.parse_memory("1048576") == 1048576

    def test_parse_memory_zero(self) -> None:
        assert ResourceLimiter.parse_memory("0") == 0

    def test_parse_memory_whitespace(self) -> None:
        assert ResourceLimiter.parse_memory("  256MB  ") == 256 * 1024**2


class TestResourceInstallUninstall:
    def test_install_uninstall_cycle(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.install()
        assert limiter._installed
        limiter.uninstall()
        assert not limiter._installed

    def test_double_install_idempotent(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.install()
        limiter.install()
        assert limiter._installed
        limiter.uninstall()

    def test_double_uninstall_safe(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="p")
        limiter.install()
        limiter.uninstall()
        limiter.uninstall()
        assert not limiter._installed


# ═══════════════════════════════════════════════════════════════════════
# Layer 2: Network Whitelist — Gap Tests
# ═══════════════════════════════════════════════════════════════════════


class TestNetworkPortFiltering:
    def test_port_allowed_when_in_whitelist(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["api.example.com"],
            allowed_ports={443},
        )
        guard = NetworkGuard(policy, plugin_id="p")
        assert guard._is_port_allowed(443)

    def test_port_blocked_when_not_in_whitelist(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["api.example.com"],
            allowed_ports={443},
        )
        guard = NetworkGuard(policy, plugin_id="p")
        assert not guard._is_port_allowed(80)
        assert not guard._is_port_allowed(8080)

    def test_port_none_always_allowed(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["api.example.com"],
            allowed_ports={443},
        )
        guard = NetworkGuard(policy, plugin_id="p")
        assert guard._is_port_allowed(None)

    def test_empty_ports_allows_all(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        guard = NetworkGuard(policy, plugin_id="p")
        assert guard._is_port_allowed(80)
        assert guard._is_port_allowed(443)
        assert guard._is_port_allowed(8080)


class TestNetworkSSRF:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.1.1",
            "0.0.0.1",
        ],
    )
    def test_private_ipv4_blocked(self, ip: str) -> None:
        assert _is_private_ip(ip)

    def test_loopback_blocked(self) -> None:
        assert _is_private_ip("127.0.0.1")

    def test_ipv6_loopback_blocked(self) -> None:
        assert _is_private_ip("::1")

    def test_ipv6_ula_blocked(self) -> None:
        assert _is_private_ip("fc00::1")

    def test_ipv6_link_local_blocked(self) -> None:
        assert _is_private_ip("fe80::1")

    def test_public_ip_not_private(self) -> None:
        assert not _is_private_ip("8.8.8.8")
        assert not _is_private_ip("1.1.1.1")

    def test_hostname_not_private(self) -> None:
        assert not _is_private_ip("example.com")

    def test_invalid_ip_not_private(self) -> None:
        assert not _is_private_ip("not-an-ip")

    def test_private_ip_blocked_by_guard(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["evil.com"])
        guard = NetworkGuard(policy, plugin_id="p")
        assert not guard._is_host_allowed("127.0.0.1")
        assert not guard._is_host_allowed("10.0.0.1")

    def test_private_ip_allowed_when_in_cidr(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=[],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy, plugin_id="p")
        assert guard._is_host_allowed("10.0.0.1")

    def test_private_ip_blocked_even_with_endpoint(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["127.0.0.1"])
        guard = NetworkGuard(policy, plugin_id="p")
        assert guard._is_host_allowed("127.0.0.1")
        assert not guard._is_host_allowed("10.0.0.1")


class TestNetworkDNSInterception:
    def test_dns_blocked_for_non_whitelisted(self) -> None:
        policy = NetworkPolicy(block_dns=True)
        guard = NetworkGuard(policy, plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError, match="DNS"):
                socket.getaddrinfo("evil.com", 443)
        finally:
            guard.uninstall()

    def test_dns_allowed_for_whitelisted(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["localhost"],
            block_dns=True,
        )
        guard = NetworkGuard(policy, plugin_id="p")
        guard.install()
        try:
            socket.getaddrinfo("localhost", 80)
        finally:
            guard.uninstall()

    def test_dns_allowed_when_block_dns_false(self) -> None:
        policy = NetworkPolicy(block_dns=False)
        guard = NetworkGuard(policy, plugin_id="p")
        guard.install()
        try:
            socket.getaddrinfo("localhost", 80)
        finally:
            guard.uninstall()


class TestNetworkSocketCreateConnection:
    def test_create_connection_blocks_disallowed_host(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["allowed.com"])
        guard = NetworkGuard(policy, plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                socket.create_connection(("blocked.com", 443), timeout=0.1)
        finally:
            guard.uninstall()

    def test_create_connection_blocks_disallowed_port(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["allowed.com"],
            allowed_ports={443},
        )
        guard = NetworkGuard(policy, plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError, match="port not whitelisted"):
                socket.create_connection(("allowed.com", 80), timeout=0.1)
        finally:
            guard.uninstall()

    def test_create_connection_logs_violation(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy, plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                socket.create_connection(("evil.com", 80), timeout=0.1)
        finally:
            guard.uninstall()
        assert len(guard.get_violations()) == 1
        assert guard.get_violations()[0].host == "evil.com"


class TestNetworkCIDR:
    def test_cidr_match_allows_ip(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["192.168.1.0/24"])
        guard = NetworkGuard(policy, plugin_id="p")
        assert guard._is_host_in_cidr("192.168.1.100")

    def test_cidr_no_match(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["192.168.1.0/24"])
        guard = NetworkGuard(policy, plugin_id="p")
        assert not guard._is_host_in_cidr("10.0.0.1")

    def test_cidr_with_hostname_returns_false(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["192.168.1.0/24"])
        guard = NetworkGuard(policy, plugin_id="p")
        assert not guard._is_host_in_cidr("example.com")

    def test_invalid_cidr_ignored(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["not-a-cidr"])
        guard = NetworkGuard(policy, plugin_id="p")
        assert not guard._is_host_in_cidr("192.168.1.1")

    def test_ipv6_cidr(self) -> None:
        policy = NetworkPolicy(allowed_cidrs=["::1/128"])
        guard = NetworkGuard(policy, plugin_id="p")
        assert guard._is_host_in_cidr("::1")

    def test_combined_endpoint_and_cidr(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["api.example.com"],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy, plugin_id="p")
        assert guard._is_host_allowed("api.example.com")
        assert guard._is_host_allowed("10.0.0.1")
        assert not guard._is_host_allowed("192.168.1.1")


class TestNetworkGuardLifecycle:
    def test_install_patches_all(self) -> None:
        original_send = httpx.AsyncClient.send
        original_conn = socket.create_connection
        original_dns = socket.getaddrinfo
        policy = NetworkPolicy(allowed_endpoints=["x.com"])
        guard = NetworkGuard(policy, plugin_id="p")
        guard.install()
        assert httpx.AsyncClient.send is not original_send
        assert socket.create_connection is not original_conn
        assert socket.getaddrinfo is not original_dns
        guard.uninstall()
        assert httpx.AsyncClient.send is original_send
        assert socket.create_connection is original_conn
        assert socket.getaddrinfo is original_dns

    def test_violations_cleared(self) -> None:
        policy = NetworkPolicy()
        guard = NetworkGuard(policy, plugin_id="p")
        guard.install()
        try:
            with pytest.raises(PermissionError):
                socket.create_connection(("evil.com", 80), timeout=0.1)
        finally:
            guard.uninstall()
        assert len(guard.get_violations()) == 1
        guard.clear_violations()
        assert len(guard.get_violations()) == 0


# ═══════════════════════════════════════════════════════════════════════
# Layer 1: Import Restrictions — Gap Tests
# ═══════════════════════════════════════════════════════════════════════


class TestImportModuleBlocked:
    def test_is_module_blocked_with_allowlist(self) -> None:
        importer = RestrictedImporter(
            blocked=set(),
            allowed={"json", "math"},
            plugin_id="p",
        )
        assert not importer._is_module_blocked("json")
        assert not importer._is_module_blocked("math")
        assert importer._is_module_blocked("os")

    def test_is_module_blocked_submodule(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p")
        assert importer._is_module_blocked("os.path")
        assert importer._is_module_blocked("os.environ")

    def test_is_module_blocked_empty_allowed_allows_all(self) -> None:
        importer = RestrictedImporter(
            blocked=set(BLOCKED_MODULES),
            allowed=None,
            plugin_id="p",
        )
        assert not importer._is_module_blocked("json")
        assert not importer._is_module_blocked("math")


class TestImportViaImportlib:
    def test_importlib_import_module_blocked(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p")
        importer.install()
        try:
            import importlib

            with pytest.raises(ImportError, match="blocked"):
                importlib.import_module("os")
        finally:
            importer.uninstall()

    def test_importlib_import_module_allowed(self) -> None:
        importer = RestrictedImporter(
            blocked={"os"},
            allowed=None,
            plugin_id="p",
        )
        importer.install()
        try:
            import importlib

            mod = importlib.import_module("json")
            assert hasattr(mod, "dumps")
        finally:
            importer.uninstall()

    def test_importlib_import_module_logs_violation(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p")
        importer.install()
        try:
            import importlib

            with pytest.raises(ImportError):
                importlib.import_module("os")
        finally:
            importer.uninstall()
        assert len(importer.get_violations()) == 1
        assert importer.get_violations()[0].module_name == "os"


class TestImportViolationTracking:
    def test_multiple_violations_tracked(self) -> None:
        importer = RestrictedImporter(blocked={"os", "subprocess"}, plugin_id="p")
        importer.install()
        try:
            with pytest.raises(ImportError):
                builtins.__import__("os")
            with pytest.raises(ImportError):
                builtins.__import__("subprocess")
        finally:
            importer.uninstall()
        assert len(importer.get_violations()) == 2

    def test_violation_fields(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="test-p")
        importer.install()
        try:
            with pytest.raises(ImportError):
                builtins.__import__("os")
        finally:
            importer.uninstall()
        v = importer.get_violations()[0]
        assert v.module_name == "os"
        assert v.plugin_id == "test-p"
        assert v.category == SandboxViolationCategory.IMPORT
        assert "import os" in v.attempted_action

    def test_clear_violations(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p")
        importer.install()
        try:
            with pytest.raises(ImportError):
                builtins.__import__("os")
        finally:
            importer.uninstall()
        assert len(importer.get_violations()) == 1
        importer.clear_violations()
        assert len(importer.get_violations()) == 0


# ═══════════════════════════════════════════════════════════════════════
# SandboxContext — Gap Tests
# ═══════════════════════════════════════════════════════════════════════


class TestSandboxContextTrustLevelValidation:
    def test_untrusted_min_blocked_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="p",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"mod_{i}" for i in range(10)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=[]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_untrusted_too_few_blocked_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="p",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os", "sys"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=[]),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_untrusted_cpu_too_high(self) -> None:
        policy = SandboxPolicy(
            plugin_id="p",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"mod_{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=120),
            filesystem_policy=FilesystemPolicy(read_write_paths=[]),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_untrusted_with_rw_paths_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="p",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"mod_{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_limited_min_blocked_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="p",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={f"mod_{i}" for i in range(5)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_limited_too_few_blocked_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="p",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_limited_cpu_too_high(self) -> None:
        policy = SandboxPolicy(
            plugin_id="p",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={f"mod_{i}" for i in range(10)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=200),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_trusted_full_always_validates(self) -> None:
        policy = SandboxPolicy(
            plugin_id="p",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules={"subprocess"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=300),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_boundary_constants(self) -> None:
        assert _MIN_BLOCKED_MODULES_UNTRUSTED == 10
        assert _MIN_BLOCKED_MODULES_LIMITED == 5
        assert _MAX_CPU_SECONDS_UNTRUSTED == 60
        assert _MAX_CPU_SECONDS_LIMITED == 120


class TestSandboxContextLifecycle:
    def test_context_manager_activates_deactivates(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        assert not ctx.is_active
        with ctx:
            assert ctx.is_active
        assert not ctx.is_active

    def test_cleanup_removes_work_dir(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        work_dir = ctx.work_dir
        assert os.path.isdir(work_dir)
        ctx.activate()
        ctx.cleanup()
        assert not os.path.isdir(work_dir)

    def test_double_activate_noop(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.activate()
        assert ctx.is_active
        ctx.deactivate()

    def test_double_deactivate_noop(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.deactivate()
        ctx.deactivate()
        assert not ctx.is_active

    def test_event_logger_property(self) -> None:
        policy = SandboxPolicy(plugin_id="p")
        ctx = SandboxContext(policy)
        assert ctx.event_logger is not None
        assert ctx.event_logger._plugin_id == "p"

    def test_policy_property(self) -> None:
        policy = SandboxPolicy(plugin_id="test-id")
        ctx = SandboxContext(policy)
        assert ctx.policy is policy

    def test_trust_level_property(self) -> None:
        from engine.plugins.trust_levels import TrustLevel

        policy = SandboxPolicy(plugin_id="p", trust_level="trusted_full")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.TRUSTED_FULL

    def test_trust_level_invalid_defaults_untrusted(self) -> None:
        from engine.plugins.trust_levels import TrustLevel

        policy = SandboxPolicy(plugin_id="p", trust_level="invalid")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED


class TestSandboxContextViolationCollection:
    def test_violations_from_all_layers_collected(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        ctx.activate()
        with contextlib.suppress(Exception), pytest.raises(PermissionError):
            builtins.open("/etc/passwd")
        with contextlib.suppress(Exception), pytest.raises(PermissionError):
            builtins.getattr(int, "__subclasses__")
        ctx.deactivate()
        events = ctx.event_logger.get_events()
        assert len(events) >= 2
        categories = {e.category for e in events}
        assert SandboxViolationCategory.FILESYSTEM in categories
        assert SandboxViolationCategory.INTROSPECTION in categories

    def test_violations_cleared_after_deactivate(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        ctx.activate()
        with contextlib.suppress(Exception), pytest.raises(PermissionError):
            builtins.open("/etc/passwd")
        ctx.deactivate()
        for layer in [
            ctx._import_layer,
            ctx._network_layer,
            ctx._resource_layer,
            ctx._filesystem_layer,
            ctx._introspection_layer,
        ]:
            assert len(layer.get_violations()) == 0


class TestSandboxContextBuiltinsRestoration:
    def test_builtins_restored_after_filesystem_violation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        original_open = builtins.open
        ctx.activate()
        with contextlib.suppress(Exception), pytest.raises(PermissionError):
            builtins.open("/etc/passwd")
        ctx.deactivate()
        assert builtins.open is original_open

    def test_builtins_restored_after_introspection_violation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        original_getattr = builtins.getattr
        original_object = builtins.object
        ctx.activate()
        with contextlib.suppress(Exception), pytest.raises(PermissionError):
            builtins.getattr(int, "__subclasses__")
        ctx.deactivate()
        assert builtins.getattr is original_getattr
        assert builtins.object is original_object

    def test_builtins_restored_after_import_violation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        original_import = builtins.__import__
        ctx.activate()
        with contextlib.suppress(Exception), pytest.raises(ImportError):
            builtins.__import__("os")
        ctx.deactivate()
        assert builtins.__import__ is original_import

    def test_all_builtins_restored_after_multiple_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        original_open = builtins.open
        original_getattr = builtins.getattr
        original_object = builtins.object
        original_import = builtins.__import__
        ctx.activate()
        for _ in range(3):
            with contextlib.suppress(Exception), pytest.raises(PermissionError):
                builtins.open("/etc/passwd")
            with contextlib.suppress(Exception), pytest.raises(PermissionError):
                builtins.getattr(int, "__subclasses__")
        ctx.deactivate()
        assert builtins.open is original_open
        assert builtins.getattr is original_getattr
        assert builtins.object is original_object
        assert builtins.__import__ is original_import


# ═══════════════════════════════════════════════════════════════════════
# Cross-Layer Integration — Gap Tests
# ═══════════════════════════════════════════════════════════════════════


class TestCrossLayerCombinedAttacks:
    def test_eval_then_import_blocked(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            with pytest.raises(PermissionError):
                builtins.eval("__import__('os')")
        finally:
            ctx.deactivate()

    def test_exec_then_import_blocked(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        ctx.activate()
        try:
            with pytest.raises(PermissionError):
                builtins.exec("import os")
        finally:
            ctx.deactivate()

    def test_filesystem_and_introspection_violations_in_same_session(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        ctx.activate()
        with contextlib.suppress(Exception), pytest.raises(PermissionError):
            builtins.open("/etc/passwd")
        with contextlib.suppress(Exception), pytest.raises(PermissionError):
            builtins.getattr(int, "__subclasses__")
        ctx.deactivate()
        events = ctx.event_logger.get_events()
        categories = {e.category for e in events}
        assert SandboxViolationCategory.FILESYSTEM in categories
        assert SandboxViolationCategory.INTROSPECTION in categories

    def test_work_dir_accessible_during_sandbox(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        work = ctx.work_dir
        with ctx:
            with builtins.open(os.path.join(work, "test.txt"), "w") as f:
                f.write("data")
            with builtins.open(os.path.join(work, "test.txt")) as f:
                assert f.read() == "data"
        ctx.cleanup()

    def test_sequential_contexts_independent(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx1 = SandboxContext(policy)
        ctx1.activate()
        with contextlib.suppress(Exception), pytest.raises(PermissionError):
            builtins.open("/etc/passwd")
        ctx1.deactivate()
        ctx1.cleanup()

        ctx2 = SandboxContext(policy)
        work2 = ctx2.work_dir
        ctx2.activate()
        try:
            with builtins.open(os.path.join(work2, "safe.txt"), "w") as f:
                f.write("ok")
            assert os.path.exists(os.path.join(work2, "safe.txt"))
        finally:
            ctx2.deactivate()
            ctx2.cleanup()


class TestCrossLayerAll5LayersCollect:
    def test_all_5_layers_installed_and_uninstalled(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        original_open = builtins.open
        original_getattr = builtins.getattr
        original_import = builtins.__import__
        original_send = httpx.AsyncClient.send
        original_conn = socket.create_connection

        ctx.activate()
        assert ctx.is_active
        assert builtins.open is not original_open
        assert builtins.getattr is not original_getattr
        assert builtins.__import__ is not original_import
        assert httpx.AsyncClient.send is not original_send
        assert socket.create_connection is not original_conn

        ctx.deactivate()
        assert builtins.open is original_open
        assert builtins.getattr is original_getattr
        assert builtins.__import__ is original_import
        assert httpx.AsyncClient.send is original_send
        assert socket.create_connection is original_conn

    def test_install_order_network_first(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "p")
        ctx = SandboxContext(policy)
        original_send = httpx.AsyncClient.send
        original_conn = socket.create_connection
        ctx.activate()
        assert socket.create_connection is not original_conn
        assert httpx.AsyncClient.send is not original_send
        ctx.deactivate()


class TestNetworkPolicyEdgeCases:
    def test_empty_endpoints_blocks_all(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        assert not policy.is_host_allowed("any.host.com")

    def test_subdomain_matching(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("example.com")
        assert policy.is_host_allowed("sub.example.com")
        assert policy.is_host_allowed("deep.sub.example.com")
        assert not policy.is_host_allowed("notexample.com")
        assert not policy.is_host_allowed("example.com.evil.com")

    def test_exact_match_only(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert policy.is_host_allowed("api.example.com")
        assert policy.is_host_allowed("sub.api.example.com")
        assert not policy.is_host_allowed("example.com")
        assert not policy.is_host_allowed("notexample.com")


class TestImportPolicyEdgeCases:
    def test_blocked_takes_priority_over_allowed(self) -> None:
        policy = ImportPolicy(
            allowed_modules={"os", "json"},
            blocked_modules={"os"},
        )
        assert not policy.is_allowed("os")
        assert policy.is_allowed("json")

    def test_empty_blocked_and_allowed(self) -> None:
        policy = ImportPolicy()
        assert policy.is_allowed("os")
        assert policy.is_allowed("json")

    def test_allowlist_blocks_non_member(self) -> None:
        policy = ImportPolicy(allowed_modules={"json"})
        assert policy.is_allowed("json")
        assert not policy.is_allowed("math")
