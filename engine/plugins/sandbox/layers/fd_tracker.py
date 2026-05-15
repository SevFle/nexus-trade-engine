from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import ResourcePolicy

from engine.plugins.sandbox.core.violation import ResourceExhausted


class FdTracker:
    def __init__(self, policy: ResourcePolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._open_fds: set[int] = set()

    def track(self, fd: int) -> None:
        self._open_fds.add(fd)
        self._check_limit()

    def untrack(self, fd: int) -> None:
        self._open_fds.discard(fd)

    def _check_limit(self) -> None:
        if len(self._open_fds) > self._policy.max_file_descriptors:
            raise ResourceExhausted(
                resource_type="file_descriptors",
                limit=self._policy.max_file_descriptors,
                current=len(self._open_fds),
                plugin_id=self._plugin_id,
            )

    @property
    def open_count(self) -> int:
        return len(self._open_fds)

    def reset(self) -> None:
        self._open_fds.clear()
