from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import FilesystemPolicy


class VirtualPathResolver:
    def __init__(self, policy: FilesystemPolicy, work_dir: str) -> None:
        self._policy = policy
        self._work_dir = os.path.realpath(work_dir)
        self._virtual_root = policy.virtual_root or self._work_dir

    def resolve(self, path: str) -> str:
        if os.path.isabs(path):
            return self._resolve_absolute(path)
        return os.path.realpath(os.path.join(self._work_dir, path))

    def _resolve_absolute(self, path: str) -> str:
        resolved = os.path.realpath(path)
        if resolved.startswith(self._virtual_root):
            return resolved
        return os.path.realpath(os.path.join(self._work_dir, os.path.basename(path)))

    def is_within_sandbox(self, path: str) -> bool:
        resolved = os.path.realpath(path)
        return resolved == self._work_dir or resolved.startswith(self._work_dir + os.sep)

    @property
    def virtual_root(self) -> str:
        return self._virtual_root

    @property
    def work_dir(self) -> str:
        return self._work_dir
