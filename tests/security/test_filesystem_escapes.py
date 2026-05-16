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
        policy = FilesystemPolicy()
        fs = FilesystemIsolation(policy=policy)
        work = fs.work_dir
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


class TestRestrictedOsMkdir:
    def test_mkdir_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            os.mkdir(os.path.join(work, "newdir"))
            assert os.path.isdir(os.path.join(work, "newdir"))
        finally:
            fs.uninstall()

    def test_mkdir_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.mkdir(str(tmp_path / "outside"))
        finally:
            fs.uninstall()

    def test_mkdir_violation_logged(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work, plugin_id="test_mkdir")
        fs.install()
        try:
            with pytest.raises(PermissionError):
                os.mkdir("/forbidden_mkdir")
        finally:
            fs.uninstall()
        violations = [v for v in fs.get_violations() if v.operation == "mkdir"]
        assert len(violations) >= 1
        assert violations[0].plugin_id == "test_mkdir"


class TestRestrictedOsMakedirs:
    def test_makedirs_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            os.makedirs(os.path.join(work, "a", "b", "c"))
            assert os.path.isdir(os.path.join(work, "a", "b", "c"))
        finally:
            fs.uninstall()

    def test_makedirs_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.makedirs(str(tmp_path / "outside" / "deep"))
        finally:
            fs.uninstall()


class TestRestrictedOsRemove:
    def test_remove_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        target = os.path.join(work, "todelete.txt")
        with open(target, "w") as f:
            f.write("bye")
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            os.remove(target)
            assert not os.path.exists(target)
        finally:
            fs.uninstall()

    def test_remove_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.remove("/etc/passwd")
        finally:
            fs.uninstall()


class TestRestrictedOsUnlink:
    def test_unlink_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        target = os.path.join(work, "unlinkme.txt")
        with open(target, "w") as f:
            f.write("bye")
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            os.unlink(target)
            assert not os.path.exists(target)
        finally:
            fs.uninstall()

    def test_unlink_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.unlink("/etc/passwd")
        finally:
            fs.uninstall()


class TestRestrictedOsRename:
    def test_rename_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        src = os.path.join(work, "src.txt")
        dst = os.path.join(work, "dst.txt")
        with open(src, "w") as f:
            f.write("data")
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            os.rename(src, dst)
            assert os.path.exists(dst)
            assert not os.path.exists(src)
        finally:
            fs.uninstall()

    def test_rename_src_outside_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.rename("/etc/passwd", os.path.join(work, "stolen.txt"))
        finally:
            fs.uninstall()

    def test_rename_dst_outside_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        src = os.path.join(work, "src.txt")
        with open(src, "w") as f:
            f.write("data")
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.rename(src, str(tmp_path / "outside.txt"))
        finally:
            fs.uninstall()


class TestRestrictedOsRmdir:
    def test_rmdir_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        target = os.path.join(work, "emptydir")
        os.mkdir(target)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            os.rmdir(target)
            assert not os.path.exists(target)
        finally:
            fs.uninstall()

    def test_rmdir_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.rmdir("/tmp")  # noqa: S108
        finally:
            fs.uninstall()


class TestRestrictedOsListdir:
    def test_listdir_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "file.txt"), "w") as f:
            f.write("data")
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            entries = os.listdir(work)
            assert "file.txt" in entries
        finally:
            fs.uninstall()

    def test_listdir_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.listdir("/etc")
        finally:
            fs.uninstall()


class TestRestrictedOsStat:
    def test_stat_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        target = os.path.join(work, "statme.txt")
        with open(target, "w") as f:
            f.write("data")
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            st = os.stat(target)
            assert st.st_size > 0
        finally:
            fs.uninstall()

    def test_stat_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.stat("/etc/passwd")
        finally:
            fs.uninstall()


class TestRestrictedOsAccess:
    def test_access_in_work_dir_allowed(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        target = os.path.join(work, "accessible.txt")
        with open(target, "w") as f:
            f.write("data")
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            assert os.access(target, os.R_OK) is True
        finally:
            fs.uninstall()

    def test_access_outside_sandbox_blocked(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                os.access("/etc/passwd", os.R_OK)
        finally:
            fs.uninstall()


class TestOsInstallUninstall:
    def test_install_patches_os_mkdir(self) -> None:
        original = os.mkdir
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            assert os.mkdir is not original
        finally:
            fs.cleanup()

    def test_install_patches_os_remove(self) -> None:
        original = os.remove
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            assert os.remove is not original
        finally:
            fs.cleanup()

    def test_install_patches_os_stat(self) -> None:
        original = os.stat
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            assert os.stat is not original
        finally:
            fs.cleanup()

    def test_install_patches_os_listdir(self) -> None:
        original = os.listdir
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            assert os.listdir is not original
        finally:
            fs.cleanup()

    def test_install_patches_os_access(self) -> None:
        original = os.access
        fs = FilesystemIsolation(FilesystemPolicy())
        try:
            fs.install()
            assert os.access is not original
        finally:
            fs.cleanup()

    def test_uninstall_restores_os_functions(self) -> None:
        originals = {
            "mkdir": os.mkdir,
            "makedirs": os.makedirs,
            "remove": os.remove,
            "unlink": os.unlink,
            "rename": os.rename,
            "rmdir": os.rmdir,
            "listdir": os.listdir,
            "stat": os.stat,
            "access": os.access,
        }
        fs = FilesystemIsolation(FilesystemPolicy())
        fs.install()
        fs.uninstall()
        for name, orig in originals.items():
            assert getattr(os, name) is orig


class TestAuditLog:
    def test_audit_log_records_allowed_open(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with open(os.path.join(work, "file.txt"), "w") as f:
                f.write("data")
        finally:
            fs.uninstall()
        log = fs.get_audit_log()
        assert any(entry["operation"] == "open_write" for entry in log)

    def test_audit_log_records_blocked_access(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with pytest.raises(PermissionError):
                builtins.open("/etc/passwd")  # noqa: SIM115
        finally:
            fs.uninstall()
        log = fs.get_audit_log()
        assert any(not entry["allowed"] for entry in log)

    def test_clear_audit_log(self, tmp_path: Any) -> None:
        work = str(tmp_path / "sandbox")
        os.makedirs(work, exist_ok=True)
        fs = FilesystemIsolation(FilesystemPolicy(), work_dir=work)
        fs.install()
        try:
            with open(os.path.join(work, "file.txt"), "w") as f:
                f.write("data")
        finally:
            fs.uninstall()
        assert len(fs.get_audit_log()) > 0
        fs.clear_audit_log()
        assert len(fs.get_audit_log()) == 0
