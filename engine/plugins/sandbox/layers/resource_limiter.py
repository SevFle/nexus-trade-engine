from __future__ import annotations

import contextlib
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import ResourcePolicy

from engine.plugins.sandbox.core.violation import ResourceExhausted

try:
    import resource as _resource

    HAS_RESOURCE_MODULE = True
except ImportError:
    _resource = None
    HAS_RESOURCE_MODULE = False


class _CPUTimer:
    def __init__(self, seconds: float, plugin_id: str | None = None) -> None:
        self._seconds = seconds
        self._plugin_id = plugin_id
        self._timer: threading.Timer | None = None
        self._expired = False
        self._start_time = 0.0

    @property
    def expired(self) -> bool:
        return self._expired

    def _on_timeout(self) -> None:
        self._expired = True

    def start(self) -> None:
        self._start_time = time.monotonic()
        self._timer = threading.Timer(self._seconds, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def check(self) -> None:
        if self._expired:
            raise ResourceExhausted(
                resource_type="cpu_time",
                limit=self._seconds,
                current=time.monotonic() - self._start_time,
                plugin_id=self._plugin_id,
            )
        elapsed = time.monotonic() - self._start_time
        if elapsed > self._seconds:
            self._expired = True
            raise ResourceExhausted(
                resource_type="cpu_time",
                limit=self._seconds,
                current=elapsed,
                plugin_id=self._plugin_id,
            )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start_time


class _WallTimer:
    def __init__(self, seconds: float, plugin_id: str | None = None) -> None:
        self._seconds = seconds
        self._plugin_id = plugin_id
        self._timer: threading.Timer | None = None
        self._expired = False
        self._start_time = 0.0

    @property
    def expired(self) -> bool:
        return self._expired

    def _on_timeout(self) -> None:
        self._expired = True

    def start(self) -> None:
        self._start_time = time.monotonic()
        self._timer = threading.Timer(self._seconds, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def check(self) -> None:
        if self._expired:
            raise ResourceExhausted(
                resource_type="wall_time",
                limit=self._seconds,
                current=time.monotonic() - self._start_time,
                plugin_id=self._plugin_id,
            )
        elapsed = time.monotonic() - self._start_time
        if elapsed > self._seconds:
            self._expired = True
            raise ResourceExhausted(
                resource_type="wall_time",
                limit=self._seconds,
                current=elapsed,
                plugin_id=self._plugin_id,
            )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start_time


class ResourceLimiter:
    def __init__(self, policy: ResourcePolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._saved_limits: dict[str, tuple[int, int]] = {}
        self._active_threads: list[Any] = []
        self._installed = False
        self._violation_log: list[ResourceExhausted] = []
        self._thread_count = 0
        self._original_thread_init: Any = None
        self._cpu_timer: _CPUTimer | None = None
        self._wall_timer: _WallTimer | None = None

    def install(self) -> None:
        if self._installed:
            return
        self._apply_resource_limits()
        self._start_cpu_timer()
        self._start_wall_timer()
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._stop_wall_timer()
        self._stop_cpu_timer()
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

    def _start_cpu_timer(self) -> None:
        self._cpu_timer = _CPUTimer(
            self._policy.max_cpu_seconds,
            plugin_id=self._plugin_id,
        )
        self._cpu_timer.start()

    def _stop_cpu_timer(self) -> None:
        if self._cpu_timer is not None:
            self._cpu_timer.stop()
            self._cpu_timer = None

    def check_cpu_timer(self) -> None:
        if self._cpu_timer is not None:
            try:
                self._cpu_timer.check()
            except ResourceExhausted as exc:
                self._violation_log.append(exc)
                raise

    def _start_wall_timer(self) -> None:
        self._wall_timer = _WallTimer(
            self._policy.wall_time_seconds,
            plugin_id=self._plugin_id,
        )
        self._wall_timer.start()

    def _stop_wall_timer(self) -> None:
        if self._wall_timer is not None:
            self._wall_timer.stop()
            self._wall_timer = None

    def check_wall_timer(self) -> None:
        if self._wall_timer is not None:
            try:
                self._wall_timer.check()
            except ResourceExhausted as exc:
                self._violation_log.append(exc)
                raise

    @property
    def cpu_elapsed(self) -> float:
        if self._cpu_timer is not None:
            return self._cpu_timer.elapsed
        return 0.0

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
