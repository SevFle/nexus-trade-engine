from __future__ import annotations

import builtins
import io as _io_module
import os as _os_module
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import FilesystemPolicy

from engine.plugins.sandbox.core.violation import FilesystemViolation

_BLOCKED_SYSTEM_PREFIXES = ("/proc", "/sys", "/dev")

_orig_realpath = _os_module.path.realpath
_orig_isdir = _os_module.path.isdir
_orig_isabs = _os_module.path.isabs
_orig_islink = _os_module.path.islink


class FilesystemIsolation:
    def __init__(
        self,
        policy: FilesystemPolicy,
        plugin_id: str | None = None,
        work_dir: str | None = None,
    ) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._work_dir = work_dir or tempfile.mkdtemp(prefix="sandbox_fs_")
        self._original_open: Any = None
        self._original_io_open: Any = None
        self._original_os_mkdir: Any = None
        self._original_os_makedirs: Any = None
        self._original_os_remove: Any = None
        self._original_os_unlink: Any = None
        self._original_os_rename: Any = None
        self._original_os_rmdir: Any = None
        self._original_os_listdir: Any = None
        self._original_os_stat: Any = None
        self._original_os_access: Any = None
        self._installed = False
        self._violation_log: list[FilesystemViolation] = []
        self._owns_work_dir = work_dir is None
        self._file_audit_log: list[dict[str, Any]] = []
        self._in_check = False

    @property
    def work_dir(self) -> str:
        return self._work_dir

    def _get_allowed_paths(self) -> list[str]:
        paths = [_orig_realpath(self._work_dir)]
        for p in self._policy.read_only_paths:
            rp = _orig_realpath(p)
            paths.append(rp)
            if _orig_isdir(rp):
                paths.append(rp + _os_module.sep)
        for p in self._policy.read_write_paths:
            rp = _orig_realpath(p)
            paths.append(rp)
            if _orig_isdir(rp):
                paths.append(rp + _os_module.sep)
        return [p for p in paths if p]

    def _is_path_allowed(self, resolved: str) -> bool:
        allowed = self._get_allowed_paths()
        return any(resolved == p or resolved.startswith(p + _os_module.sep) for p in allowed)

    def _is_write_allowed(self, resolved: str) -> bool:
        rw_paths = [_orig_realpath(p) for p in self._policy.read_write_paths]
        rw_paths.append(_orig_realpath(self._work_dir))
        return any(resolved == p or resolved.startswith(p + _os_module.sep) for p in rw_paths if p)

    def _validate_path(self, path: str) -> str:
        parts = path.replace("\\", "/").split("/")
        if ".." in parts:
            violation = FilesystemViolation(path, "path_traversal", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)

        resolved = _orig_realpath(path)

        for prefix in _BLOCKED_SYSTEM_PREFIXES:
            if resolved.startswith(prefix):
                violation = FilesystemViolation(path, "system_path", plugin_id=self._plugin_id)
                self._violation_log.append(violation)
                raise PermissionError(violation.detail)

        if self._policy.block_symlinks and _orig_islink(path):
            violation = FilesystemViolation(path, "symlink", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)

        return resolved

    def _check_absolute_path_policy(self, path: str) -> None:
        if not self._policy.block_absolute_paths:
            return
        if not _orig_isabs(path):
            return
        resolved = _orig_realpath(path)
        work_dir_real = _orig_realpath(self._work_dir)
        if resolved == work_dir_real or resolved.startswith(work_dir_real + _os_module.sep):
            return
        for p in self._policy.read_only_paths:
            rp = _orig_realpath(p)
            if resolved == rp or resolved.startswith(rp + _os_module.sep):
                return
        for p in self._policy.read_write_paths:
            rp = _orig_realpath(p)
            if resolved == rp or resolved.startswith(rp + _os_module.sep):
                return
        if self._is_path_allowed(resolved):
            violation = FilesystemViolation(path, "absolute_path", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)

    def _check_path_access(self, path: str, operation: str) -> str:
        if self._in_check:
            return _orig_realpath(str(path))
        self._in_check = True
        try:
            self._check_absolute_path_policy(str(path))
            resolved = self._validate_path(str(path))
            if not self._is_path_allowed(resolved):
                violation = FilesystemViolation(str(path), operation, plugin_id=self._plugin_id)
                self._violation_log.append(violation)
                raise PermissionError(violation.detail)
            return resolved
        finally:
            self._in_check = False

    def _check_write_access(self, path: str, operation: str) -> str:
        if self._in_check:
            return _orig_realpath(str(path))
        self._in_check = True
        try:
            self._check_absolute_path_policy(str(path))
            resolved = self._validate_path(str(path))
            if not self._is_path_allowed(resolved):
                violation = FilesystemViolation(str(path), operation, plugin_id=self._plugin_id)
                self._violation_log.append(violation)
                raise PermissionError(violation.detail)
            if not self._is_write_allowed(resolved):
                violation = FilesystemViolation(str(path), operation, plugin_id=self._plugin_id)
                self._violation_log.append(violation)
                raise PermissionError(violation.detail)
            return resolved
        finally:
            self._in_check = False

    def _audit_log(self, operation: str, path: str, allowed: bool = True) -> None:
        self._file_audit_log.append(
            {
                "operation": operation,
                "path": path,
                "allowed": allowed,
                "plugin_id": self._plugin_id,
            }
        )

    def _restricted_open(
        self,
        file: Any,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if isinstance(file, int):
            violation = FilesystemViolation("<fd>", "fd_access", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            self._audit_log("fd_access", "<fd>", allowed=False)
            raise PermissionError(violation.detail)

        self._check_absolute_path_policy(str(file))

        resolved = _orig_realpath(str(file))

        if not self._is_path_allowed(resolved):
            violation = FilesystemViolation(str(file), "read", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            self._audit_log("read", str(file), allowed=False)
            raise PermissionError(violation.detail)

        is_write = any(c in mode for c in ("w", "a", "+"))
        if is_write and not self._is_write_allowed(resolved):
            violation = FilesystemViolation(str(file), "write", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            self._audit_log("write", str(file), allowed=False)
            raise PermissionError(violation.detail)

        self._audit_log("open" if not is_write else "open_write", str(file))
        return self._original_open(file, mode, *args, **kwargs)

    def _restricted_os_mkdir(
        self,
        path: str,
        mode: int = 0o777,
        **_kwargs: Any,
    ) -> None:
        resolved = self._check_write_access(path, "mkdir")
        self._original_os_mkdir(resolved, mode)

    def _restricted_os_makedirs(
        self,
        path: str,
        mode: int = 0o777,
        exist_ok: bool = False,
    ) -> None:
        resolved = self._check_write_access(path, "makedirs")
        self._original_os_makedirs(resolved, mode, exist_ok=exist_ok)

    def _restricted_os_remove(self, path: str, **_kwargs: Any) -> None:
        resolved = self._check_write_access(path, "remove")
        self._original_os_remove(resolved)

    def _restricted_os_unlink(self, path: str, **_kwargs: Any) -> None:
        resolved = self._check_write_access(path, "unlink")
        self._original_os_unlink(resolved)

    def _restricted_os_rename(self, src: str, dst: str, **_kwargs: Any) -> None:
        self._check_write_access(src, "rename_src")
        self._check_write_access(dst, "rename_dst")
        self._original_os_rename(src, dst)

    def _restricted_os_rmdir(self, path: str, **_kwargs: Any) -> None:
        resolved = self._check_write_access(path, "rmdir")
        self._original_os_rmdir(resolved)

    def _restricted_os_listdir(self, path: str = ".") -> list[str]:
        resolved = self._check_path_access(path, "listdir")
        return self._original_os_listdir(resolved)

    def _restricted_os_stat(
        self,
        path: str,
        follow_symlinks: bool = True,
        **_kwargs: Any,
    ) -> Any:
        resolved = self._check_path_access(path, "stat")
        return self._original_os_stat(resolved, follow_symlinks=follow_symlinks)

    def _restricted_os_access(
        self,
        path: str,
        mode: int,
        follow_symlinks: bool = True,
        **_kwargs: Any,
    ) -> bool:
        resolved = self._check_path_access(path, "access")
        return self._original_os_access(
            resolved,
            mode,
            follow_symlinks=follow_symlinks,
        )

    def install(self) -> None:
        if self._installed:
            return

        self._original_open = builtins.open
        builtins.open = self._restricted_open

        self._original_io_open = _io_module.open
        _io_module.open = self._restricted_open

        self._original_os_mkdir = _os_module.mkdir
        _os_module.mkdir = self._restricted_os_mkdir

        self._original_os_makedirs = _os_module.makedirs
        _os_module.makedirs = self._restricted_os_makedirs

        self._original_os_remove = _os_module.remove
        _os_module.remove = self._restricted_os_remove

        self._original_os_unlink = _os_module.unlink
        _os_module.unlink = self._restricted_os_unlink

        self._original_os_rename = _os_module.rename
        _os_module.rename = self._restricted_os_rename

        self._original_os_rmdir = _os_module.rmdir
        _os_module.rmdir = self._restricted_os_rmdir

        self._original_os_listdir = _os_module.listdir
        _os_module.listdir = self._restricted_os_listdir

        self._original_os_stat = _os_module.stat
        _os_module.stat = self._restricted_os_stat

        self._original_os_access = _os_module.access
        _os_module.access = self._restricted_os_access

        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return

        if self._original_open is not None:
            builtins.open = self._original_open
            self._original_open = None
        if self._original_io_open is not None:
            _io_module.open = self._original_io_open
            self._original_io_open = None

        if self._original_os_mkdir is not None:
            _os_module.mkdir = self._original_os_mkdir
            self._original_os_mkdir = None
        if self._original_os_makedirs is not None:
            _os_module.makedirs = self._original_os_makedirs
            self._original_os_makedirs = None
        if self._original_os_remove is not None:
            _os_module.remove = self._original_os_remove
            self._original_os_remove = None
        if self._original_os_unlink is not None:
            _os_module.unlink = self._original_os_unlink
            self._original_os_unlink = None
        if self._original_os_rename is not None:
            _os_module.rename = self._original_os_rename
            self._original_os_rename = None
        if self._original_os_rmdir is not None:
            _os_module.rmdir = self._original_os_rmdir
            self._original_os_rmdir = None
        if self._original_os_listdir is not None:
            _os_module.listdir = self._original_os_listdir
            self._original_os_listdir = None
        if self._original_os_stat is not None:
            _os_module.stat = self._original_os_stat
            self._original_os_stat = None
        if self._original_os_access is not None:
            _os_module.access = self._original_os_access
            self._original_os_access = None

        self._installed = False

    def cleanup(self) -> None:
        self.uninstall()
        if self._owns_work_dir and self._work_dir and _orig_isdir(self._work_dir):
            shutil.rmtree(self._work_dir, ignore_errors=True)

    def get_violations(self) -> list[FilesystemViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()

    def get_audit_log(self) -> list[dict[str, Any]]:
        return list(self._file_audit_log)

    def clear_audit_log(self) -> None:
        self._file_audit_log.clear()
