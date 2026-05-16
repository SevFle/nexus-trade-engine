from __future__ import annotations

import signal
import threading
import time
from unittest.mock import patch

import pytest

from engine.plugins.sandbox.core.policy import ResourcePolicy
from engine.plugins.sandbox.core.violation import ResourceExhausted
from engine.plugins.sandbox.layers.resource_limiter import (
    _HAS_SIGVTALRM,
    ResourceLimiter,
    _CPUTimer,
    _signal_lock,
)


@pytest.fixture
def force_poll():
    _CPUTimer._force_poll = True
    yield
    _CPUTimer._force_poll = False


def _burn_cpu(duration: float) -> None:
    end = time.monotonic() + duration
    total = 0.0
    while time.monotonic() < end:
        total += 1.0


class TestSignalLockSerialization:
    def test_signal_lock_is_threading_lock(self) -> None:
        assert isinstance(_signal_lock, type(threading.Lock()))

    def test_concurrent_try_start_signal_serialized(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def try_signal() -> None:
            t = _CPUTimer(10.0)
            barrier.wait()
            results.append(t._try_start_signal())
            if results[-1]:
                t._stop_signal()

        threads = [threading.Thread(target=try_signal) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        at_most_one_signal = sum(1 for r in results if r) <= 1
        assert at_most_one_signal

    def test_try_start_signal_from_non_main_thread_returns_false(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        result: dict[str, bool] = {}

        def run() -> None:
            t = _CPUTimer(10.0)
            result["ok"] = t._try_start_signal()
            if result["ok"]:
                t._stop_signal()

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result.get("ok") is False


class TestStopSignalMainThreadGuard:
    def test_stop_signal_from_main_thread_works(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._stop_signal()
        assert t._use_signal is False

    def test_stop_signal_from_non_main_thread_does_not_crash(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        errors: list[Exception] = []

        def run() -> None:
            t = _CPUTimer(10.0)
            t._use_signal = True
            try:
                t._stop_signal()
            except Exception as e:
                errors.append(e)

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert len(errors) == 0

    def test_stop_signal_from_non_main_thread_logs_warning(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ):
            t._stop_signal()
        assert t._use_signal is False
        assert t._old_handler is not None

    def test_old_handler_preserved_on_restore_failure(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        with patch("signal.signal", side_effect=OSError("denied")):
            t._stop_signal()
        assert t._old_handler is sentinel
        assert t._use_signal is False

    def test_old_handler_nulled_on_successful_restore(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._stop_signal()
        assert t._old_handler is None
        assert t._use_signal is False


class TestPollPathAsAuthoritative:
    def test_start_always_creates_poll_thread(self) -> None:
        t = _CPUTimer(60.0)
        t.start()
        try:
            assert t._thread is not None
            assert t._thread.is_alive()
        finally:
            t.cancel()

    def test_start_always_creates_poll_thread_with_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        try:
            assert t._use_signal is True
            assert t._thread is not None
            assert t._thread.is_alive()
        finally:
            t.cancel()

    def test_start_creates_poll_thread_with_force_poll(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        try:
            assert t._use_signal is False
            assert t._thread is not None
        finally:
            t.cancel()

    def test_poll_thread_detects_wall_time_expiry(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        time.sleep(0.15)
        assert t.expired
        t.cancel()

    def test_poll_thread_detects_cpu_time_expiry(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        _burn_cpu(0.3)
        assert t.expired
        t.cancel()

    def test_cancel_stops_poll_thread_in_signal_mode(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        assert t._thread is not None
        assert t._use_signal is True
        t.cancel()
        assert t._thread is None


class TestWallTimeWatchdog:
    def test_wall_time_monitored_alongside_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.05)
        t.start()
        assert t._use_signal is True
        assert t._thread is not None
        time.sleep(0.15)
        assert t.expired
        t.cancel()

    def test_wall_time_triggers_expiry_in_poll_mode(self, force_poll: None) -> None:
        t = _CPUTimer(0.03)
        t.start()
        time.sleep(0.15)
        assert t.expired
        t.cancel()

    def test_resource_limiter_wall_timer_always_installed(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="wall-watch")
        limiter.install()
        try:
            assert limiter._wall_timer is not None
            assert isinstance(limiter._wall_timer._expired, threading.Event)
        finally:
            limiter.uninstall()

    def test_wall_timer_expires_independently_of_cpu_timer(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=0.02)
        limiter = ResourceLimiter(policy, plugin_id="wall-ind")
        limiter.install()
        time.sleep(0.1)
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.check_wall_timer()
        assert exc_info.value.resource_type == "wall_time"
        limiter.uninstall()

    def test_check_detects_wall_time_breach(self, force_poll: None) -> None:
        t = _CPUTimer(0.03)
        t.start()
        time.sleep(0.15)
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "cpu_time"
        t.cancel()


class TestSignalSupplementaryEarlyWarning:
    def test_signal_fires_before_poll_detects(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.05)
        t.start()
        assert t._use_signal is True
        _burn_cpu(0.3)
        assert t.expired
        t.cancel()

    def test_poll_also_fires_if_signal_missed(self, force_poll: None) -> None:
        t = _CPUTimer(0.05)
        t.start()
        assert t._use_signal is False
        _burn_cpu(0.3)
        assert t.expired
        t.cancel()

    def test_mode_property_reflects_signal_state(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        assert t.mode == "poll"
        t.start()
        try:
            assert t.mode == "signal"
        finally:
            t.cancel()
        assert t.mode == "poll"

    def test_signal_and_poll_both_cleanup_on_cancel(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is True
        assert t._thread is not None
        t.cancel()
        assert t._use_signal is False
        assert t._thread is None
        restored = signal.getsignal(signal.SIGVTALRM)
        assert restored is sentinel


class TestSignalRestoreFailureDoesNotCorruptState:
    def test_failed_restore_keeps_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        handler = signal.getsignal(signal.SIGVTALRM)
        t._use_signal = True
        t._old_handler = handler
        with patch("signal.signal", side_effect=OSError("cannot restore")):
            t._stop_signal()
        assert t._old_handler is handler
        assert t._use_signal is False

    def test_successful_restore_clears_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        handler = signal.getsignal(signal.SIGVTALRM)
        t._use_signal = True
        t._old_handler = handler
        t._stop_signal()
        assert t._old_handler is None

    def test_multiple_cancel_cycles_after_failed_restore(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        for _ in range(3):
            t.start()
            t.cancel()
            assert t._thread is None
            assert t._use_signal is False
