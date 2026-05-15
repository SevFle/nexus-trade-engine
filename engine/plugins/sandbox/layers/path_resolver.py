from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import FilesystemPolicy

from engine.plugins.sandbox.core.violation import FilesystemViolation


class PathResolver:
    def __init__(
        self,
        policy: FilesystemPolicy,
        work_dir: str,
        plugin_id: str | None = None,
    ) -> None:
        self._policy = policy
        self._work_dir = os.path.realpath(work_dir)
        self._plugin_id = plugin_id

    def resolve_read_path(self, path: str) -> str:
        resolved = os.path.realpath(str(path))
        if not self._is_allowed(resolved):
            violation = FilesystemViolation(str(path), "read", plugin_id=self._plugin_id)
            raise PermissionError(violation.detail)
        return resolved

    def resolve_write_path(self, path: str) -> str:
        resolved = os.path.realpath(str(path))
        if not self._is_write_allowed(resolved):
            violation = FilesystemViolation(str(path), "write", plugin_id=self._plugin_id)
            raise PermissionError(violation.detail)
        return resolved

    def _is_allowed(self, resolved: str) -> bool:
        if resolved == self._work_dir or resolved.startswith(self._work_dir + os.sep):
            return True
        for p in self._policy.read_only_paths:
            rp = os.path.realpath(p)
            if resolved == rp or resolved.startswith(rp + os.sep):
                return True
        for p in self._policy.read_write_paths:
            rp = os.path.realpath(p)
            if resolved == rp or resolved.startswith(rp + os.sep):
                return True
        return False

    def _is_write_allowed(self, resolved: str) -> bool:
        if resolved == self._work_dir or resolved.startswith(self._work_dir + os.sep):
            return True
        for p in self._policy.read_write_paths:
            rp = os.path.realpath(p)
            if resolved == rp or resolved.startswith(rp + os.sep):
                return True
        return False
