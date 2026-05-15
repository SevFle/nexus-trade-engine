from __future__ import annotations

import builtins
import os
from typing import Any

import pytest

from engine.plugins.sandbox.core.policy import FilesystemPolicy
from engine.plugins.sandbox.layers.filesystem_isolation import FilesystemIsolation


@pytest.fixture
def isolated_fs(tmp_path: Any) -> FilesystemIsolation:
    policy = FilesystemPolicy(
        read_only_paths=[],
        read_write_paths=[],
        virtual_root=None,
        block_symlinks=True,
        block_absolute_paths=True,
    )
    return FilesystemIsolation(
        policy=policy,
        plugin_id="test_plugin",
        work_dir=str(tmp_path / "sandbox"),
    )


class TestPathPrefixMatching:
    def test_exact_path_match(self, tmp_path: Any) -> None:
        artifact = tmp_path / "safe.txt"
        artifact.write_text("safe")
        policy = FilesystemPolicy(read_only_paths=[str(artifact)])
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        assert fs._is_path_allowed(str(artifact))

    def test_child_path_match(self, tmp_path: Any) -> None:
        artifact_dir = tmp_path / "data"
        artifact_dir.mkdir()
        (artifact_dir / "file.txt").write_text("data")
        policy = FilesystemPolicy(read_only_paths=[str(artifact_dir)])
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        assert fs._is_path_allowed(str(artifact_dir / "file.txt"))

    def test_partial_directory_name_not_matched(self, tmp_path: Any) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "safe.txt").write_text("safe")

        database_dir = tmp_path / "database"
        database_dir.mkdir()
        (database_dir / "secret.txt").write_text("secret")

        policy = FilesystemPolicy(read_only_paths=[str(data_dir)])
        fs = FilesystemIsolation(policy=policy, plugin_id="test")

        assert fs._is_path_allowed(str(data_dir / "safe.txt"))
        assert not fs._is_path_allowed(str(database_dir / "secret.txt"))

    def test_work_dir_always_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "work")
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy=policy, work_dir=work)
        assert fs._is_path_allowed(os.path.realpath(work))

    def test_unrelated_path_blocked(self, tmp_path: Any) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy=policy, work_dir=str(tmp_path / "sandbox"))
        assert not fs._is_path_allowed("/etc/passwd")
        assert not fs._is_path_allowed(str(tmp_path / "other" / "file.txt"))


class TestWriteRestrictions:
    def test_read_only_paths_block_write(self, tmp_path: Any) -> None:
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        (readonly / "file.txt").write_text("data")
        policy = FilesystemPolicy(read_only_paths=[str(readonly)])
        fs = FilesystemIsolation(policy=policy, work_dir=str(tmp_path / "sandbox"))
        assert not fs._is_write_allowed(str(readonly / "file.txt"))

    def test_work_dir_allows_write(self, tmp_path: Any) -> None:
        work = str(tmp_path / "work")
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy=policy, work_dir=work)
        assert fs._is_write_allowed(os.path.realpath(work))

    def test_rw_paths_allow_write(self, tmp_path: Any) -> None:
        rw_dir = tmp_path / "readwrite"
        rw_dir.mkdir()
        policy = FilesystemPolicy(read_write_paths=[str(rw_dir)])
        fs = FilesystemIsolation(policy=policy, work_dir=str(tmp_path / "sandbox"))
        assert fs._is_write_allowed(str(rw_dir / "new_file.txt"))


class TestPathTraversal:
    def test_dotdot_in_path_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="path_traversal"):
            fs._validate_path("/safe/dir/../../../etc/passwd")

    def test_dotdot_component_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="path_traversal"):
            fs._validate_path("../../../etc/shadow")


class TestSystemPaths:
    def test_proc_path_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/proc/self/environ")

    def test_sys_path_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/sys/kernel/notes")

    def test_dev_path_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/dev/null")


class TestSymlinkBlocking:
    def test_symlink_path_blocked(self, tmp_path: Any) -> None:
        target = tmp_path / "target.txt"
        target.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="symlink"):
            fs._validate_path(str(link))


class TestFileDescriptorBlocking:
    def test_fd_open_blocked(self, isolated_fs: FilesystemIsolation) -> None:
        isolated_fs.install()
        try:
            with pytest.raises(PermissionError, match="fd_access"):
                builtins.open(0)  # noqa: SIM115
        finally:
            isolated_fs.uninstall()


class TestFileOpenRestriction:
    def test_read_outside_sandbox_blocked(self, isolated_fs: FilesystemIsolation) -> None:
        isolated_fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                builtins.open("/etc/passwd")  # noqa: SIM115
        finally:
            isolated_fs.uninstall()

    def test_write_outside_sandbox_blocked(self, isolated_fs: FilesystemIsolation) -> None:
        isolated_fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                builtins.open("/tmp/sandbox_test_write", "w")  # noqa: S108, SIM115
        finally:
            isolated_fs.uninstall()


class TestFilesystemIsolationLifecycle:
    def test_install_replaces_open(self) -> None:
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy=policy)
        original = builtins.open
        fs.install()
        assert builtins.open is not original
        fs.uninstall()
        assert builtins.open is original

    def test_cleanup_removes_work_dir(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox_cleanup_test")
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy=policy, work_dir=work)
        os.makedirs(work, exist_ok=True)
        assert os.path.isdir(work)
        fs.cleanup()
        assert not os.path.isdir(work)

    def test_violation_recording(self, isolated_fs: FilesystemIsolation) -> None:
        isolated_fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/etc/passwd")  # noqa: SIM115
        finally:
            isolated_fs.uninstall()
        violations = isolated_fs.get_violations()
        assert len(violations) >= 1
        isolated_fs.clear_violations()
        assert len(isolated_fs.get_violations()) == 0
