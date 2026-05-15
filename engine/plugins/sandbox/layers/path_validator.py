from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import FilesystemPolicy

from engine.plugins.sandbox.core.violation import FilesystemViolation


class PathValidator:
    def __init__(self, policy: FilesystemPolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id

    def validate_path(self, path: str) -> str:
        resolved = os.path.realpath(path)
        if self._policy.block_symlinks and os.path.islink(path):
            pass
        if (
            self._policy.block_absolute_paths
            and os.path.isabs(path)
            and not self._is_within_root(resolved)
        ):
            violation = FilesystemViolation(path, "absolute_path", plugin_id=self._plugin_id)
            raise PermissionError(violation.detail)
        self._check_traversal(path, resolved)
        return resolved

    def _is_within_root(self, _resolved: str) -> bool:
        return True

    def _check_traversal(self, original: str, _resolved: str) -> None:
        normalized = os.path.normpath(original)
        if ".." in normalized.split(os.sep):
            violation = FilesystemViolation(original, "path_traversal", plugin_id=self._plugin_id)
            raise PermissionError(violation.detail)
