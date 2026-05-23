from __future__ import annotations

import builtins
import io as _io_module
import os
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import FilesystemPolicy

from engine.plugins.sandbox.core.violation import FilesystemViolation

_BLOCKED_SYSTEM_PREFIXES: frozenset[str] = frozenset(
    {
        "proc",
        "sys",
        "dev",
        "etc",
        "root",
        "home",
    }
)


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
        self._installed = False
        self._violation_log: list[FilesystemViolation] = []
        self._owns_work_dir = work_dir is None

    @property
    def work_dir(self) -> str:
        return self._work_dir

    def _get_allowed_paths(self) -> list[str]:
        paths = [os.path.realpath(self._work_dir)]
        for p in self._policy.read_only_paths:
            rp = os.path.realpath(p)
            paths.append(rp)
            if os.path.isdir(rp):
                paths.append(rp + os.sep)
        for p in self._policy.read_write_paths:
            rp = os.path.realpath(p)
            paths.append(rp)
            if os.path.isdir(rp):
                paths.append(rp + os.sep)
        return [p for p in paths if p]

    def _is_path_allowed(self, resolved: str) -> bool:
        allowed = self._get_allowed_paths()
        return any(
            resolved == p or resolved.startswith(p + os.sep)
            for p in allowed
        )

    def _is_write_allowed(self, resolved: str) -> bool:
        rw_paths = [os.path.realpath(p) for p in self._policy.read_write_paths]
        rw_paths.append(os.path.realpath(self._work_dir))
        rw_paths = [p + os.sep if os.path.isdir(p) else p for p in rw_paths]
        return any(
            resolved == p or resolved.startswith(p + os.sep)
            for p in rw_paths
            if p
        )

    def _validate_path(self, path: str) -> str:
        resolved = os.path.realpath(str(path))

        if self._policy.block_symlinks and os.path.islink(str(path)):
            violation = FilesystemViolation(resolved, "symlink", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)

        traversals = ["..", *list(_BLOCKED_SYSTEM_PREFIXES)]
        for t in traversals:
            if t in resolved.split(os.sep):
                violation = FilesystemViolation(resolved, "traversal", plugin_id=self._plugin_id)
                self._violation_log.append(violation)
                raise PermissionError(violation.detail)

        return resolved

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
            raise PermissionError(violation.detail)

        resolved = os.path.realpath(str(file))

        if not self._is_path_allowed(resolved):
            violation = FilesystemViolation(str(file), "read", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)

        is_write = any(c in mode for c in ("w", "a", "+"))
        if is_write and not self._is_write_allowed(resolved):
            violation = FilesystemViolation(str(file), "write", plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)

        return self._original_open(file, mode, *args, **kwargs)

    def install(self) -> None:
        if self._installed:
            return

        self._original_open = builtins.open
        builtins.open = self._restricted_open

        self._original_io_open = _io_module.open
        _io_module.open = self._restricted_open

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
        self._installed = False

    def cleanup(self) -> None:
        self.uninstall()
        if self._owns_work_dir and self._work_dir and os.path.isdir(self._work_dir):
            shutil.rmtree(self._work_dir, ignore_errors=True)

    def get_violations(self) -> list[FilesystemViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
