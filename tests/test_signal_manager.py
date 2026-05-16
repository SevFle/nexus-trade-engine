"""
Comprehensive tests for the _SignalManager singleton and _stop_signal changes.

Focus areas:
1. _SignalManager singleton pattern, ownership, and reference counting
2. _SignalManager.try_acquire() refusal when another timer owns the signal
3. _stop_signal non-main thread cleanup attempts (setitimer + signal restore)
4. Integration between _CPUTimer and _SignalManager
5. Edge cases, error conditions, and boundary values
"""

from __future__ import annotations

import signal
import threading
import time
from unittest.mock import patch

import pytest

from engine.plugins.sandbox.core.policy import ResourcePolicy
from engine.plugins.sandbox.layers.resource_limiter import (
    _HAS_SIGVTALRM,
    ResourceLimiter,
    _CPUTimer,
    _signal_mgr,
    _SignalManager,
    _WallTimer,
)


@pytest.fixture(autouse=True)
def _reset_signal_mgr():
    _signal_mgr.reset()
    _CPUTimer._force_poll = False
    yield
    _signal_mgr.reset()
    _CPUTimer._force_poll = False


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


# ═══════════════════════════════════════════════════════════════════════
# 1. _SignalManager singleton pattern
# ═══════════════════════════════════════════════════════════════════════


class TestSignalManagerSingleton:
    def test_same_instance_returned(self) -> None:
        a = _SignalManager()
        b = _SignalManager()
        assert a is b

    def test_module_level_instance_is_singleton(self) -> None:
        assert _signal_mgr is _SignalManager()

    def test_reset_clears_owner(self) -> None:
        _signal_mgr._owner = "fake"
        _signal_mgr.reset()
        assert _signal_mgr.owner is None

    def test_reset_clears_ref_count(self) -> None:
        _signal_mgr._ref_count = 42
        _signal_mgr.reset()
        assert _signal_mgr.ref_count == 0

    def test_owner_initially_none(self) -> None:
        _signal_mgr.reset()
        assert _signal_mgr.owner is None

    def test_ref_count_initially_zero(self) -> None:
        _signal_mgr.reset()
        assert _signal_mgr.ref_count == 0

    def test_internal_lock_is_threading_lock(self) -> None:
        assert isinstance(_signal_mgr._lock, type(threading.Lock()))


# ═══════════════════════════════════════════════════════════════════════
# 2. _SignalManager.try_acquire()
# ═══════════════════════════════════════════════════════════════════════


class TestSignalManagerTryAcquire:
    def test_acquire_succeeds_on_main_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        result = _signal_mgr.try_acquire(t)
        try:
            assert result is True
            assert t._use_signal is True
            assert _signal_mgr.owner is t
            assert _signal_mgr.ref_count == 1
        finally:
            t.cancel()

    def test_acquire_sets_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        result = _signal_mgr.try_acquire(t)
        try:
            assert result is True
            assert t._old_handler is sentinel
        finally:
            t.cancel()

    def test_acquire_installs_timer_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        _signal_mgr.try_acquire(t)
        try:
            current = signal.getsignal(signal.SIGVTALRM)
            assert current is not sentinel
            assert callable(current)
        finally:
            t.cancel()

    def test_acquire_sets_itimer(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        _signal_mgr.try_acquire(t)
        try:
            remaining = signal.getitimer(signal.ITIMER_VIRTUAL)
            assert remaining[0] > 0.0
        finally:
            t.cancel()

    def test_acquire_refuses_when_force_poll(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        _CPUTimer._force_poll = True
        t = _CPUTimer(10.0)
        result = _signal_mgr.try_acquire(t)
        assert result is False
        assert _signal_mgr.owner is None

    def test_acquire_refuses_from_non_main_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        result_box: dict[str, bool] = {}

        def run() -> None:
            t = _CPUTimer(10.0)
            result_box["ok"] = _signal_mgr.try_acquire(t)

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result_box.get("ok") is False
        assert _signal_mgr.owner is None

    def test_acquire_refuses_when_another_timer_owns(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t1 = _CPUTimer(10.0)
        t1.start()
        try:
            assert _signal_mgr.owner is t1
            t2 = _CPUTimer(10.0)
            result = _signal_mgr.try_acquire(t2)
            assert result is False
            assert t2._use_signal is False
            assert _signal_mgr.owner is t1
        finally:
            t1.cancel()

    def test_acquire_refuses_when_signal_signal_raises(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        with patch("signal.signal", side_effect=ValueError("no signals")):
            result = _signal_mgr.try_acquire(t)
        assert result is False
        assert _signal_mgr.owner is None
        assert t._use_signal is False

    def test_acquire_refuses_when_setitimer_raises(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        with patch("signal.setitimer", side_effect=OSError("no timer")):
            result = _signal_mgr.try_acquire(t)
        assert result is False
        assert _signal_mgr.owner is None
        assert t._use_signal is False

    def test_acquire_without_sigvtalrm_returns_false(self) -> None:
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter._HAS_SIGVTALRM",
            False,
        ):
            t = _CPUTimer(10.0)
            result = _signal_mgr.try_acquire(t)
            assert result is False

    def test_acquire_increments_ref_count(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        assert _signal_mgr.ref_count == 0
        _signal_mgr.try_acquire(t)
        try:
            assert _signal_mgr.ref_count == 1
        finally:
            t.cancel()

    def test_acquire_does_not_increment_on_failure(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        _CPUTimer._force_poll = True
        t = _CPUTimer(10.0)
        _signal_mgr.try_acquire(t)
        assert _signal_mgr.ref_count == 0


# ═══════════════════════════════════════════════════════════════════════
# 3. _SignalManager.release()
# ═══════════════════════════════════════════════════════════════════════


class TestSignalManagerRelease:
    def test_release_clears_owner(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        assert _signal_mgr.owner is t
        t.cancel()
        assert _signal_mgr.owner is None

    def test_release_decrements_ref_count(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        assert _signal_mgr.ref_count == 1
        t.cancel()
        assert _signal_mgr.ref_count == 0

    def test_release_no_op_for_non_owner(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t1 = _CPUTimer(10.0)
        t1.start()
        try:
            t2 = _CPUTimer(10.0)
            _signal_mgr.release(t2)
            assert _signal_mgr.owner is t1
            assert _signal_mgr.ref_count == 1
        finally:
            t1.cancel()

    def test_release_no_op_when_no_owner(self) -> None:
        t = _CPUTimer(10.0)
        _signal_mgr.release(t)
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0

    def test_release_does_not_go_below_zero(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()
        assert _signal_mgr.ref_count == 0
        _signal_mgr.release(t)
        assert _signal_mgr.ref_count == 0

    def test_release_from_non_main_thread_clears_owner(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        assert _signal_mgr.owner is t

        result_box: dict[str, bool] = {}

        def run() -> None:
            _signal_mgr.release(t)
            result_box["done"] = True

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result_box.get("done") is True
        assert _signal_mgr.owner is None


# ═══════════════════════════════════════════════════════════════════════
# 4. _SignalManager sequential acquire-release cycles
# ═══════════════════════════════════════════════════════════════════════


class TestSignalManagerSequential:
    def test_sequential_acquire_release(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        for i in range(5):
            t = _CPUTimer(10.0, plugin_id=f"seq-{i}")
            t.start()
            try:
                assert _signal_mgr.owner is t
                assert t.mode == "signal"
            finally:
                t.cancel()
            assert _signal_mgr.owner is None

    def test_second_timer_acquires_after_first_releases(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t1 = _CPUTimer(10.0, plugin_id="first")
        t1.start()
        assert _signal_mgr.owner is t1

        t2 = _CPUTimer(10.0, plugin_id="second")
        assert _signal_mgr.try_acquire(t2) is False

        t1.cancel()
        assert _signal_mgr.owner is None

        assert _signal_mgr.try_acquire(t2) is True
        assert _signal_mgr.owner is t2
        t2.cancel()

    def test_ref_count_across_cycles(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        assert _signal_mgr.ref_count == 0
        t = _CPUTimer(10.0)
        t.start()
        assert _signal_mgr.ref_count == 1
        t.cancel()
        assert _signal_mgr.ref_count == 0
        t2 = _CPUTimer(10.0)
        t2.start()
        assert _signal_mgr.ref_count == 1
        t2.cancel()
        assert _signal_mgr.ref_count == 0


# ═══════════════════════════════════════════════════════════════════════
# 5. _SignalManager concurrent access
# ═══════════════════════════════════════════════════════════════════════


class TestSignalManagerConcurrency:
    def test_concurrent_acquire_only_one_wins(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def try_acquire() -> None:
            t = _CPUTimer(10.0)
            barrier.wait()
            ok = _signal_mgr.try_acquire(t)
            results.append(ok)
            if ok:
                t.cancel()

        threads = [threading.Thread(target=try_acquire) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        at_most_one = sum(1 for r in results if r) <= 1
        assert at_most_one

    def test_owner_read_is_thread_safe(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        errors: list[str] = []
        barrier = threading.Barrier(3)

        def reader() -> None:
            barrier.wait()
            for _ in range(100):
                owner = _signal_mgr.owner
                if owner is not None and not isinstance(owner, _CPUTimer):
                    errors.append(f"unexpected owner type: {type(owner)}")

        t = _CPUTimer(10.0)
        t.start()
        try:
            threads = [threading.Thread(target=reader) for _ in range(3)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=5.0)
        finally:
            t.cancel()
        assert len(errors) == 0


# ═══════════════════════════════════════════════════════════════════════
# 6. _stop_signal: non-main thread attempts cleanup
# ═══════════════════════════════════════════════════════════════════════


class TestStopSignalNonMainThreadCleanup:
    def test_non_main_thread_attempts_setitimer_zero(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with (
            patch(
                "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
                return_value=threading.Thread(),
            ),
            patch("signal.setitimer") as mock_setitimer,
        ):
            t._stop_signal()
            mock_setitimer.assert_called_once_with(signal.ITIMER_VIRTUAL, 0)

    def test_non_main_thread_attempts_signal_restore(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        with (
            patch(
                "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
                return_value=threading.Thread(),
            ),
            patch("signal.signal") as mock_signal,
        ):
            t._stop_signal()
            mock_signal.assert_called_with(signal.SIGVTALRM, sentinel)

    def test_non_main_thread_does_not_crash_on_setitimer_error(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with (
            patch(
                "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
                return_value=threading.Thread(),
            ),
            patch("signal.setitimer", side_effect=OSError("denied")),
        ):
            t._stop_signal()
        assert t._use_signal is False

    def test_non_main_thread_does_not_crash_on_signal_error(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with (
            patch(
                "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
                return_value=threading.Thread(),
            ),
            patch("signal.signal", side_effect=ValueError("not main")),
        ):
            t._stop_signal()
        assert t._use_signal is False

    def test_non_main_thread_sets_use_signal_false(self) -> None:
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

    def test_non_main_thread_preserves_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ):
            t._stop_signal()
        assert t._old_handler is sentinel

    def test_non_main_thread_skips_restore_when_no_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = None
        with (
            patch(
                "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
                return_value=threading.Thread(),
            ),
            patch("signal.signal") as mock_signal,
        ):
            t._stop_signal()
            mock_signal.assert_not_called()
        assert t._use_signal is False

    def test_actual_non_main_thread_stop_signal_no_crash(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        errors: list[Exception] = []

        def run() -> None:
            t = _CPUTimer(10.0)
            t._use_signal = True
            t._old_handler = signal.getsignal(signal.SIGVTALRM)
            try:
                t._stop_signal()
            except Exception as e:
                errors.append(e)

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert len(errors) == 0

    def test_actual_non_main_thread_attempts_setitimer(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        called: list[bool] = []

        def run() -> None:
            t = _CPUTimer(10.0)
            t._use_signal = True
            t._old_handler = signal.getsignal(signal.SIGVTALRM)
            with patch("signal.setitimer", side_effect=ValueError("x")) as m:
                t._stop_signal()
                called.append(m.called)

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert len(called) == 1
        assert called[0]

    def test_actual_non_main_thread_attempts_signal_restore(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        called: list[bool] = []

        def run() -> None:
            t = _CPUTimer(10.0)
            t._use_signal = True
            t._old_handler = signal.getsignal(signal.SIGVTALRM)
            with patch("signal.signal", side_effect=ValueError("x")) as m:
                t._stop_signal()
                called.append(m.called)

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert len(called) == 1
        assert called[0]


# ═══════════════════════════════════════════════════════════════════════
# 7. _stop_signal: main thread unchanged behavior
# ═══════════════════════════════════════════════════════════════════════


class TestStopSignalMainThreadUnchanged:
    def test_main_thread_stops_itimer(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._stop_signal()
        remaining = signal.getitimer(signal.ITIMER_VIRTUAL)
        assert remaining[0] == 0.0

    def test_main_thread_restores_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = sentinel
        t._stop_signal()
        assert signal.getsignal(signal.SIGVTALRM) is sentinel
        assert t._old_handler is None

    def test_main_thread_use_signal_false(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._stop_signal()
        assert t._use_signal is False

    def test_main_thread_failed_restore_keeps_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        handler = signal.getsignal(signal.SIGVTALRM)
        t._use_signal = True
        t._old_handler = handler
        with patch("signal.signal", side_effect=OSError("denied")):
            t._stop_signal()
        assert t._old_handler is handler
        assert t._use_signal is False


# ═══════════════════════════════════════════════════════════════════════
# 8. _CPUTimer integration with _SignalManager
# ═══════════════════════════════════════════════════════════════════════


class TestCPUTimerSignalManagerIntegration:
    def test_start_registers_with_signal_mgr(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert _signal_mgr.owner is t
            assert _signal_mgr.ref_count == 1
        finally:
            t.cancel()

    def test_cancel_releases_from_signal_mgr(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        assert _signal_mgr.owner is t
        t.cancel()
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0

    def test_second_timer_falls_back_to_poll(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t1 = _CPUTimer(10.0, plugin_id="t1")
        t1.start()
        try:
            assert t1.mode == "signal"
            assert _signal_mgr.owner is t1

            t2 = _CPUTimer(10.0, plugin_id="t2")
            t2.start()
            try:
                assert t2.mode == "poll"
                assert t2._use_signal is False
                assert _signal_mgr.owner is t1
            finally:
                t2.cancel()
        finally:
            t1.cancel()

    def test_second_timer_acquires_after_first_cancels(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t1 = _CPUTimer(10.0, plugin_id="first")
        t1.start()
        assert t1.mode == "signal"
        t1.cancel()
        assert _signal_mgr.owner is None

        t2 = _CPUTimer(10.0, plugin_id="second")
        t2.start()
        try:
            assert t2.mode == "signal"
            assert _signal_mgr.owner is t2
        finally:
            t2.cancel()

    def test_poll_mode_timer_does_not_register(self) -> None:
        _CPUTimer._force_poll = True
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t.mode == "poll"
            assert _signal_mgr.owner is None
            assert _signal_mgr.ref_count == 0
        finally:
            t.cancel()

    def test_restart_reacquires_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        for _i in range(5):
            t.start()
            try:
                assert _signal_mgr.owner is t
                assert t.mode == "signal"
                assert _signal_mgr.ref_count == 1
            finally:
                t.cancel()
            assert _signal_mgr.owner is None
            assert _signal_mgr.ref_count == 0

    def test_context_manager_releases(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        with _CPUTimer(10.0) as t:
            assert _signal_mgr.owner is t
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0

    def test_del_releases(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        assert _signal_mgr.owner is t
        t.__del__()
        assert _signal_mgr.owner is None


# ═══════════════════════════════════════════════════════════════════════
# 9. ResourceLimiter integration with _SignalManager
# ═══════════════════════════════════════════════════════════════════════


class TestResourceLimiterSignalManagerIntegration:
    def test_install_registers_cpu_timer(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="rl-sm")
        limiter.install()
        try:
            assert limiter._cpu_timer is not None
            assert _signal_mgr.owner is limiter._cpu_timer
        finally:
            limiter.uninstall()

    def test_uninstall_releases_cpu_timer(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="rl-sm")
        limiter.install()
        limiter.uninstall()
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0

    def test_two_limiters_sequential(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        l1 = ResourceLimiter(policy, plugin_id="l1")
        l1.install()
        try:
            assert _signal_mgr.owner is l1._cpu_timer
        finally:
            l1.uninstall()

        l2 = ResourceLimiter(policy, plugin_id="l2")
        l2.install()
        try:
            assert _signal_mgr.owner is l2._cpu_timer
        finally:
            l2.uninstall()

    def test_two_limiters_second_falls_back(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        l1 = ResourceLimiter(policy, plugin_id="l1")
        l1.install()
        try:
            l2 = ResourceLimiter(policy, plugin_id="l2")
            l2.install()
            try:
                assert _signal_mgr.owner is l1._cpu_timer
                assert l2._cpu_timer is not None
                assert l2._cpu_timer.mode == "poll"
            finally:
                l2.uninstall()
            assert _signal_mgr.owner is l1._cpu_timer
        finally:
            l1.uninstall()


# ═══════════════════════════════════════════════════════════════════════
# 10. Edge cases and boundary values
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_timer_with_zero_seconds(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.0)
        t.start()
        try:
            time.sleep(0.05)
            assert t.expired
        finally:
            t.cancel()

    def test_timer_with_very_small_seconds(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.001)
        t.start()
        _burn_cpu(0.05)
        assert t.expired
        t.cancel()

    def test_timer_with_large_seconds(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(86400.0)
        t.start()
        try:
            assert t.mode == "signal"
            assert not t.expired
        finally:
            t.cancel()

    def test_multiple_rapid_start_cancel_cycles(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        for _ in range(50):
            t = _CPUTimer(10.0)
            t.start()
            t.cancel()
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0

    def test_cancel_without_start_no_signal_mgr_impact(self) -> None:
        t = _CPUTimer(10.0)
        t.cancel()
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0

    def test_force_poll_no_signal_mgr_registration(self) -> None:
        _CPUTimer._force_poll = True
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert _signal_mgr.owner is None
            assert _signal_mgr.ref_count == 0
            assert t.mode == "poll"
        finally:
            t.cancel()

    def test_signal_mgr_owner_property_is_locked(self) -> None:
        errors: list[str] = []
        barrier = threading.Barrier(2)

        def reader() -> None:
            barrier.wait()
            for _ in range(200):
                try:
                    owner = _signal_mgr.owner
                    assert owner is None or isinstance(owner, _CPUTimer)
                except Exception as e:
                    errors.append(str(e))

        t = _CPUTimer(10.0)
        t.start()
        try:
            th = threading.Thread(target=reader)
            th.start()
            barrier.wait()
            th.join(timeout=5.0)
        finally:
            t.cancel()
        assert len(errors) == 0

    def test_signal_mgr_ref_count_property_is_locked(self) -> None:
        errors: list[str] = []
        barrier = threading.Barrier(2)

        def reader() -> None:
            barrier.wait()
            for _ in range(200):
                try:
                    rc = _signal_mgr.ref_count
                    assert isinstance(rc, int)
                    assert rc >= 0
                except Exception as e:
                    errors.append(str(e))

        t = _CPUTimer(10.0)
        t.start()
        try:
            th = threading.Thread(target=reader)
            th.start()
            barrier.wait()
            th.join(timeout=5.0)
        finally:
            t.cancel()
        assert len(errors) == 0

    def test_reset_mid_operation(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        _signal_mgr.reset()
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0
        t.cancel()

    def test_stop_signal_releases_mgr_even_if_not_owner(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._stop_signal()
        assert t._use_signal is False
        assert _signal_mgr.owner is None


# ═══════════════════════════════════════════════════════════════════════
# 11. _WallTimer unchanged behavior (regression guard)
# ═══════════════════════════════════════════════════════════════════════


class TestWallTimerRegression:
    def test_wall_timer_not_affected_by_signal_mgr(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        try:
            assert not t.expired
            assert _signal_mgr.owner is None
        finally:
            t.cancel()

    def test_wall_timer_expiry(self) -> None:
        t = _WallTimer(0.02)
        t.start()
        time.sleep(0.1)
        assert t.expired
        t.cancel()

    def test_wall_timer_cancel(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.cancel()
        assert t._timer is None


# ═══════════════════════════════════════════════════════════════════════
# 12. _SignalManager try_acquire failure atomicity
# ═══════════════════════════════════════════════════════════════════════


class TestSignalManagerAtomicity:
    def test_acquire_failure_does_not_set_timer_state(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        _CPUTimer._force_poll = True
        t = _CPUTimer(10.0)
        result = _signal_mgr.try_acquire(t)
        assert result is False
        assert t._use_signal is False
        assert t._old_handler is None

    def test_acquire_failure_on_second_timer_no_state_leak(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t1 = _CPUTimer(10.0)
        t1.start()
        try:
            t2 = _CPUTimer(10.0)
            result = _signal_mgr.try_acquire(t2)
            assert result is False
            assert t2._use_signal is False
            assert t2._old_handler is None
        finally:
            t1.cancel()

    def test_signal_signal_failure_no_owner_set(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        with patch("signal.signal", side_effect=OSError("x")):
            result = _signal_mgr.try_acquire(t)
        assert result is False
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0

    def test_setitimer_failure_rolls_back(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        with patch("signal.setitimer", side_effect=OSError("x")):
            result = _signal_mgr.try_acquire(t)
        assert result is False
        assert _signal_mgr.owner is None
        assert _signal_mgr.ref_count == 0
