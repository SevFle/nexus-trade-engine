from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import threading

    from engine.plugins.sandbox.core.policy import ResourcePolicy

from engine.plugins.sandbox.core.violation import ResourceExhausted

try:
    import resource as _resource

    HAS_RESOURCE_MODULE = True
except ImportError:
    _resource = None
    HAS_RESOURCE_MODULE = False


class ResourceLimiter:
    def __init__(self, policy: ResourcePolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._saved_limits: dict[str, tuple[int, int]] = {}
        self._active_threads: list[threading.Thread] = []
        self._installed = False
        self._violation_log: list[ResourceExhausted] = []
        self._thread_count = 0
        self._original_thread_init: Any = None

    def install(self) -> None:
        if self._installed:
            return
        self._apply_resource_limits()
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._restore_resource_limits()
        self._installed = False

    def _apply_resource_limits(self) -> None:
        if not HAS_RESOURCE_MODULE:
            return

        try:
            max_bytes = self._policy.max_memory_bytes
            soft, hard = _resource.getrlimit(_resource.RLIMIT_AS)
            new_soft = min(max_bytes, hard)
            _resource.setrlimit(_resource.RLIMIT_AS, (new_soft, hard))
            self._saved_limits["RLIMIT_AS"] = (soft, hard)
        except (ValueError, OSError, AttributeError):
            pass

        try:
            soft, hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
            new_soft = min(self._policy.max_file_descriptors, hard)
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (new_soft, hard))
            self._saved_limits["RLIMIT_NOFILE"] = (soft, hard)
        except (ValueError, OSError, AttributeError):
            pass

    def _restore_resource_limits(self) -> None:
        if not HAS_RESOURCE_MODULE:
            return
        for name, (soft, hard) in self._saved_limits.items():
            with contextlib.suppress(ValueError, OSError, AttributeError):
                _resource.setrlimit(getattr(_resource, name), (soft, hard))
        self._saved_limits.clear()

    def check_thread_limit(self) -> None:
        if self._thread_count >= self._policy.max_threads:
            exc = ResourceExhausted(
                resource_type="threads",
                limit=self._policy.max_threads,
                current=self._thread_count,
                plugin_id=self._plugin_id,
            )
            self._violation_log.append(exc)
            raise exc

    def increment_thread(self) -> None:
        self.check_thread_limit()
        self._thread_count += 1

    def decrement_thread(self) -> None:
        self._thread_count = max(0, self._thread_count - 1)

    def get_violations(self) -> list[ResourceExhausted]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()

    @staticmethod
    def parse_memory(mem_str: str) -> int:
        val = mem_str.strip().upper()
        units: dict[str, int] = {
            "GB": 1024**3,
            "MB": 1024**2,
            "KB": 1024,
            "B": 1,
        }
        for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
            if val.endswith(suffix):
                return int(float(val[: -len(suffix)]) * multiplier)
        return int(val)
