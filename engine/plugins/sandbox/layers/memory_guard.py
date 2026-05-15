from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import ResourcePolicy

from engine.plugins.sandbox.core.violation import ResourceExhausted


class MemoryGuard:
    def __init__(self, policy: ResourcePolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._allocated: int = 0

    def allocate(self, size: int) -> None:
        self._allocated += size
        if self._allocated > self._policy.max_memory_bytes:
            raise ResourceExhausted(
                resource_type="memory",
                limit=self._policy.max_memory_bytes,
                current=self._allocated,
                plugin_id=self._plugin_id,
            )

    def deallocate(self, size: int) -> None:
        self._allocated = max(0, self._allocated - size)

    @property
    def current_usage(self) -> int:
        return self._allocated

    @property
    def limit(self) -> int:
        return self._policy.max_memory_bytes

    def check_limit(self) -> None:
        if self._allocated > self._policy.max_memory_bytes:
            raise ResourceExhausted(
                resource_type="memory",
                limit=self._policy.max_memory_bytes,
                current=self._allocated,
                plugin_id=self._plugin_id,
            )

    def reset(self) -> None:
        self._allocated = 0
