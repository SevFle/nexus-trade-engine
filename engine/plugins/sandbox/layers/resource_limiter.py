from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import ResourcePolicy

from engine.plugins.sandbox.core.violation import ResourceExhausted

logger = structlog.get_logger()

try:
    import resource as _resource

    HAS_RESOURCE_MODULE = True
except ImportError:
    _resource = None
    HAS_RESOURCE_MODULE = False

_HAS_SIGVTALRM = hasattr(signal, "SIGVTALRM") and hasattr(signal, "ITIMER_VIRTUAL")
_signal_lock = threading.Lock()


class _CPUTimer:
    _POLL_INTERVAL = 0.05
    _force_poll = False

    def __init__(self, seconds: float, plugin_id: str | None = None) -> None:
        self._seconds = seconds
        self._plugin_id = plugin_id
        self._expired = threading.Event()
        self._cancelled = threading.Event()
        self._start_cpu: float = 0.0
        self._wall_start: float = 0.0
        self._thread: threading.Thread | None = None
        self._use_signal = False
        self._old_handler: Any = None
        self._pending_signal_cleanup = False

    @property
    def expired(self) -> bool:
        return self._expired.is_set()

    @property
    def mode(self) -> str:
        return "signal" if self._use_signal else "poll"

    def _cpu_time(self) -> float:
        t = os.times()
        return t[0] + t[1]

    def _signal_handler(self, _signum: int, _frame: Any) -> None:
        self._expired.set()

    def _poll(self) -> None:
        while not self._cancelled.wait(timeout=self._POLL_INTERVAL):
            cpu_elapsed = self._cpu_time() - self._start_cpu
            wall_elapsed = time.monotonic() - self._wall_start
            if cpu_elapsed >= self._seconds or wall_elapsed >= self._seconds:
                self._expired.set()
                return

    def _try_start_signal(self) -> bool:
        self._try_deferred_cleanup()
        if self._force_poll:
            return False
        if not _HAS_SIGVTALRM:
            return False
        if threading.current_thread() is not threading.main_thread():
            return False
        with _signal_lock:
            try:
                self._old_handler = signal.signal(signal.SIGVTALRM, self._signal_handler)
                signal.setitimer(signal.ITIMER_VIRTUAL, self._seconds)
                self._use_signal = True
            except (ValueError, OSError):
                return False
            else:
                return True

    def _try_deferred_cleanup(self) -> None:
        if not self._pending_signal_cleanup:
            return
        if threading.current_thread() is not threading.main_thread():
            return
        with _signal_lock:
            if not self._pending_signal_cleanup:
                return
            with contextlib.suppress(ValueError, OSError):
                signal.setitimer(signal.ITIMER_VIRTUAL, 0)
            if self._old_handler is not None:
                try:
                    signal.signal(signal.SIGVTALRM, self._old_handler)
                except (ValueError, OSError):
                    logger.warning(
                        "sandbox.signal_restore_failed",
                        plugin_id=self._plugin_id,
                    )
                else:
                    self._old_handler = None
            self._pending_signal_cleanup = False

    def _stop_signal(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            logger.warning(
                "sandbox.stop_signal_not_main_thread",
                plugin_id=self._plugin_id,
            )
            if self._use_signal or self._old_handler is not None:
                with _signal_lock:
                    self._pending_signal_cleanup = True
            self._use_signal = False
            return
        with contextlib.suppress(ValueError, OSError):
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)
        if self._old_handler is not None:
            with _signal_lock:
                try:
                    signal.signal(signal.SIGVTALRM, self._old_handler)
                except (ValueError, OSError):
                    logger.warning(
                        "sandbox.signal_restore_failed",
                        plugin_id=self._plugin_id,
                    )
                else:
                    self._old_handler = None
        self._use_signal = False

    def start(self) -> None:
        self._start_cpu = self._cpu_time()
        self._wall_start = time.monotonic()
        self._cancelled.clear()
        self._expired.clear()
        self._use_signal = False
        self._old_handler = None
        self._pending_signal_cleanup = False
        self._try_start_signal()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.cancel()

    def cancel(self) -> None:
        self._cancelled.set()
        if self._use_signal:
            self._stop_signal()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def check(self) -> None:
        if self._expired.is_set():
            raise ResourceExhausted(
                resource_type="cpu_time",
                limit=self._seconds,
                current=time.monotonic() - self._wall_start,
                plugin_id=self._plugin_id,
            )
        cpu_elapsed = self._cpu_time() - self._start_cpu
        wall_elapsed = time.monotonic() - self._wall_start
        if cpu_elapsed > self._seconds or wall_elapsed > self._seconds:
            self._expired.set()
            raise ResourceExhausted(
                resource_type="cpu_time",
                limit=self._seconds,
                current=time.monotonic() - self._wall_start,
                plugin_id=self._plugin_id,
            )

    @property
    def elapsed(self) -> float:
        if self._wall_start == 0.0:
            return 0.0
        return time.monotonic() - self._wall_start

    @property
    def _start_time(self) -> float:
        return self._wall_start

    @_start_time.setter
    def _start_time(self, value: float) -> None:
        delta = time.monotonic() - value
        self._start_cpu = self._cpu_time() - delta
        self._wall_start = value

    @property
    def _timer(self) -> None:
        return None

    def _on_timeout(self) -> None:
        self._expired.set()

    def __enter__(self) -> _CPUTimer:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cancel()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.cancel()


class _WallTimer:
    def __init__(self, seconds: float, plugin_id: str | None = None) -> None:
        self._seconds = seconds
        self._plugin_id = plugin_id
        self._timer: threading.Timer | None = None
        self._expired = threading.Event()
        self._start_time = 0.0

    @property
    def expired(self) -> bool:
        return self._expired.is_set()

    def _on_timeout(self) -> None:
        self._expired.set()

    def start(self) -> None:
        self._start_time = time.monotonic()
        self._expired.clear()
        self._timer = threading.Timer(self._seconds, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        self.cancel()

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def check(self) -> None:
        if self._expired.is_set():
            raise ResourceExhausted(
                resource_type="wall_time",
                limit=self._seconds,
                current=time.monotonic() - self._start_time,
                plugin_id=self._plugin_id,
            )
        elapsed = time.monotonic() - self._start_time
        if elapsed > self._seconds:
            self._expired.set()
            raise ResourceExhausted(
                resource_type="wall_time",
                limit=self._seconds,
                current=elapsed,
                plugin_id=self._plugin_id,
            )

    @property
    def elapsed(self) -> float:
        if self._start_time == 0.0:
            return 0.0
        return time.monotonic() - self._start_time

    def __enter__(self) -> _WallTimer:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cancel()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.cancel()


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
        try:
            self._stop_wall_timer()
        finally:
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
            self._cpu_timer.cancel()
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
            self._wall_timer.cancel()
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
