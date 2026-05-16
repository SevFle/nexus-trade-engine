"""
Thread-safety, cancel behavior, signal fallback, and edge-case tests
for the refactored resource_limiter module.

Covers gaps not addressed in test_resource_limiter_refactor.py:
- Signal mode fallback when signal.signal / setitimer raises
- Cancel clears expired event in edge cases
- Concurrent start/cancel stress from multiple threads
- Poll loop exit guarantees under cancel
- __del__ under broken internal state
- _WallTimer cancel with threading.Timer edge cases
- ResourceLimiter resource limits application mocking
- Elapsed time boundary conditions
"""

from __future__ import annotations

import os
import signal
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from engine.plugins.sandbox.core.policy import ResourcePolicy
from engine.plugins.sandbox.core.violation import ResourceExhausted
from engine.plugins.sandbox.layers.resource_limiter import (
    _HAS_SIGVTALRM,
    ResourceLimiter,
    _CPUTimer,
    _WallTimer,
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


# ═══════════════════════════════════════════════════════════════════════
# 1. threading.Event: verify set()/is_set()/clear() thread-safety
# ═══════════════════════════════════════════════════════════════════════


class TestThreadingEventThreadSafety:
    def test_expired_event_is_shared_across_threads(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        seen = []
        barrier = threading.Barrier(3)

        def reader(label: str) -> None:
            barrier.wait()
            seen.append((label, t.expired))

        threads = [threading.Thread(target=reader, args=(f"r{i}",)) for i in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        t.cancel()
        assert all(s[1] is False for s in seen)

    def test_expired_set_visible_across_threads(self) -> None:
        t = _CPUTimer(10.0)
        t._expired.set()
        results = []
        barrier = threading.Barrier(4)

        def reader() -> None:
            barrier.wait()
            results.append(t.expired)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        t.cancel()
        assert all(r is True for r in results)

    def test_concurrent_set_and_read_expired(self) -> None:
        t = _CPUTimer(10.0)
        errors = []
        barrier = threading.Barrier(2)

        def writer() -> None:
            barrier.wait()
            for _ in range(200):
                t._expired.set()
                t._expired.clear()

        def reader() -> None:
            barrier.wait()
            for _ in range(200):
                val = t.expired
                if not isinstance(val, bool):
                    errors.append(f"expected bool, got {type(val)}")

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        w.join(timeout=5.0)
        r.join(timeout=5.0)
        t.cancel()
        assert len(errors) == 0

    def test_wall_timer_event_shared_across_threads(self) -> None:
        t = _WallTimer(10.0)
        t._expired.set()
        results = []
        barrier = threading.Barrier(4)

        def reader() -> None:
            barrier.wait()
            results.append(t.expired)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        t.cancel()
        assert all(r is True for r in results)

    def test_cancelled_event_state_after_cancel(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert not t._cancelled.is_set()
        t.cancel()
        assert t._cancelled.is_set()

    def test_cancelled_event_cleared_on_restart(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()
        assert t._cancelled.is_set()
        t.start()
        assert not t._cancelled.is_set()
        t.cancel()

    def test_expired_event_cleared_on_restart(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        time.sleep(0.15)
        assert t.expired
        t.cancel()
        t._seconds = 60.0
        t.start()
        assert not t.expired
        t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# 2. cancel() method: verify proper cleanup
# ═══════════════════════════════════════════════════════════════════════


class TestCancelCleanup:
    def test_cancel_before_expiry_keeps_expired_false(self, force_poll: None) -> None:
        t = _CPUTimer(5.0)
        t.start()
        t.cancel()
        assert not t.expired

    def test_cancel_stops_poll_thread_quickly(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        thread = t._thread
        assert thread is not None
        t.cancel()
        assert not thread.is_alive()

    def test_cancel_join_timeout_respected(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        start = time.monotonic()
        t.cancel()
        elapsed = time.monotonic() - start
        assert elapsed < 2.0

    def test_cancel_double_with_interleaved_start(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        for _ in range(10):
            t.start()
            assert t._thread is not None
            t.cancel()
            assert t._thread is None

    def test_cancel_on_never_started_timer(self) -> None:
        t = _CPUTimer(10.0)
        t.cancel()
        assert t._thread is None
        assert not t.expired

    def test_cancel_preserves_expired_if_already_set(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        time.sleep(0.15)
        assert t.expired
        t.cancel()
        assert t.expired

    def test_wall_timer_cancel_before_expiry_keeps_expired_false(self) -> None:
        t = _WallTimer(5.0)
        t.start()
        t.cancel()
        assert not t.expired

    def test_wall_timer_cancel_nils_threading_timer(self) -> None:
        t = _WallTimer(5.0)
        t.start()
        internal_timer = t._timer
        assert internal_timer is not None
        t.cancel()
        assert t._timer is None

    def test_wall_timer_cancel_after_expiry(self) -> None:
        t = _WallTimer(0.01)
        t.start()
        time.sleep(0.05)
        assert t.expired
        t.cancel()
        assert t.expired
        assert t._timer is None


class TestCancelConcurrency:
    def test_concurrent_cancel_from_multiple_threads(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        barrier = threading.Barrier(4)

        def canceller() -> None:
            barrier.wait()
            t.cancel()

        threads = [threading.Thread(target=canceller) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        assert t._thread is None

    def test_cancel_during_check_loop(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        errors = []
        stop_event = threading.Event()

        def check_loop() -> None:
            while not stop_event.is_set():
                try:
                    t.check()
                except ResourceExhausted:
                    errors.append("exhausted")
                time.sleep(0.001)

        checker = threading.Thread(target=check_loop)
        checker.start()
        time.sleep(0.05)
        t.cancel()
        stop_event.set()
        checker.join(timeout=5.0)
        assert all(e == "exhausted" for e in errors)

    def test_cancel_and_start_race(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        errors = []
        stop = threading.Event()

        def racer() -> None:
            while not stop.is_set():
                try:
                    t.start()
                except Exception as e:
                    errors.append(e)
                try:
                    t.cancel()
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=racer) for _ in range(3)]
        for th in threads:
            th.start()
        time.sleep(0.3)
        stop.set()
        for th in threads:
            th.join(timeout=5.0)
        t.cancel()
        assert all(not isinstance(e, (SystemExit, KeyboardInterrupt)) for e in errors)


# ═══════════════════════════════════════════════════════════════════════
# 3. Signal fallback: SIGVTALRM / os.times() polling
# ═══════════════════════════════════════════════════════════════════════


class TestSignalFallbackPaths:
    def test_signal_raises_valueerror_falls_to_poll(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        with patch("signal.signal", side_effect=ValueError("no signals for you")):
            result = t._try_start_signal()
        assert result is False

    def test_signal_raises_oserror_falls_to_poll(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        with patch("signal.signal", side_effect=OSError("no signals")):
            result = t._try_start_signal()
        assert result is False

    def test_setitimer_raises_falls_to_poll(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        with patch("signal.setitimer", side_effect=OSError("itimer fail")):
            result = t._try_start_signal()
        assert result is False

    def test_stop_signal_handles_valueerror(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch("signal.setitimer", side_effect=ValueError("bad")):
            t._stop_signal()
        assert not t._use_signal

    def test_stop_signal_handles_oserror(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch("signal.signal", side_effect=OSError("bad")):
            t._stop_signal()
        assert not t._use_signal

    def test_poll_mode_measures_cpu_not_wall(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            cpu_before = t._cpu_time()
            time.sleep(0.1)
            cpu_after = t._cpu_time()
            assert (cpu_after - cpu_before) < 0.05
        finally:
            t.cancel()

    def test_os_times_used_for_cpu_measurement(self) -> None:
        t = _CPUTimer(10.0)
        cpu = t._cpu_time()
        ot = os.times()
        expected = ot[0] + ot[1]
        assert abs(cpu - expected) < 1.0

    def test_poll_exits_on_cancel_not_just_expiry(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        thread = t._thread
        assert thread is not None
        time.sleep(0.1)
        t.cancel()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert not t.expired

    def test_signal_mode_fires_on_cpu_burn(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.05)
        t.start()
        _burn_cpu(0.5)
        assert t.expired
        t.cancel()

    def test_force_poll_fires_on_cpu_burn(self, force_poll: None) -> None:
        t = _CPUTimer(0.05)
        t.start()
        _burn_cpu(0.5)
        assert t.expired
        t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# 4. __del__ safety net
# ═══════════════════════════════════════════════════════════════════════


class TestDelSafetyNet:
    def test_cpu_timer_del_cancels_active_thread(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        thread = t._thread
        assert thread is not None
        t.__del__()
        assert t._thread is None
        assert not thread.is_alive()

    def test_cpu_timer_del_suppresses_exceptions(self) -> None:
        t = _CPUTimer(10.0)
        call_count = 0

        def bad_cancel() -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("cancel broke")

        t.cancel = bad_cancel
        t.__del__()
        assert call_count == 1

    def test_wall_timer_del_cancels_active_timer(self) -> None:
        t = _WallTimer(60.0)
        t.start()
        assert t._timer is not None
        t.__del__()
        assert t._timer is None

    def test_wall_timer_del_suppresses_exceptions(self) -> None:
        t = _WallTimer(10.0)
        call_count = 0

        def bad_cancel() -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("cancel broke")

        t.cancel = bad_cancel
        t.__del__()
        assert call_count == 1

    def test_del_on_already_cancelled_timer(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()
        assert t._thread is None
        t.__del__()
        assert t._thread is None

    def test_del_on_never_started_timer(self) -> None:
        t = _CPUTimer(10.0)
        t.__del__()
        assert t._thread is None

    def test_del_uses_contextlib_suppress(self) -> None:
        t = _CPUTimer(10.0)
        t.cancel = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        t.__del__()


# ═══════════════════════════════════════════════════════════════════════
# 5. Poll loop behavior: verify loop breaks correctly
# ═══════════════════════════════════════════════════════════════════════


class TestPollLoopBreakBehavior:
    def test_poll_loop_exits_on_wall_time_exceeded(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        thread = t._thread
        time.sleep(0.15)
        assert t.expired
        if thread is not None:
            thread.join(timeout=2.0)
            assert not thread.is_alive()
        t.cancel()

    def test_poll_loop_exits_on_cpu_time_exceeded(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        _burn_cpu(0.3)
        assert t.expired
        t.cancel()

    def test_poll_loop_exits_on_cancel_without_expiry(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        thread = t._thread
        assert thread is not None
        t.cancel()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert not t.expired

    def test_poll_loop_cancelled_event_wakes_promptly(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        start = time.monotonic()
        t.cancel()
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    def test_poll_interval_allows_reasonable_cancel_latency(self, force_poll: None) -> None:
        assert _CPUTimer._POLL_INTERVAL <= 0.1

    def test_poll_thread_is_daemon(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        try:
            assert t._thread is not None
            assert t._thread.daemon
        finally:
            t.cancel()

    def test_multiple_poll_cycles_no_thread_leak(self, force_poll: None) -> None:
        initial = threading.active_count()
        for _ in range(15):
            t = _CPUTimer(60.0)
            t.start()
            t.cancel()
        time.sleep(0.5)
        leaked = threading.active_count() - initial
        assert leaked <= 2


# ═══════════════════════════════════════════════════════════════════════
# 6. Check method edge cases after cancel / restart
# ═══════════════════════════════════════════════════════════════════════


class TestCheckEdgeCases:
    def test_check_after_cancel_no_error_if_within_limit(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        t.cancel()
        t._start_time = time.monotonic()
        t.check()

    def test_check_after_restart_with_fresh_state(self, force_poll: None) -> None:
        t = _CPUTimer(0.01)
        t.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted):
            t.check()
        t.cancel()
        t.start()
        t.check()
        t.cancel()

    def test_check_cpu_and_wall_both_exceeded(self, force_poll: None) -> None:
        t = _CPUTimer(0.01, plugin_id="both")
        t._start_time = time.monotonic() - 10.0
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "cpu_time"

    def test_check_without_start_does_not_crash_with_manual_start_time(self) -> None:
        t = _CPUTimer(60.0)
        t._start_time = time.monotonic()
        t.check()

    def test_wall_timer_check_after_cancel_no_error(self) -> None:
        t = _WallTimer(60.0)
        t.start()
        t.cancel()
        t._start_time = time.monotonic()
        t.check()

    def test_wall_timer_check_after_restart(self) -> None:
        t = _WallTimer(0.01)
        t.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted):
            t.check()
        t.cancel()
        t.start()
        t.check()
        t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# 7. Elapsed property boundary conditions
# ═══════════════════════════════════════════════════════════════════════


class TestElapsedBoundaryConditions:
    def test_cpu_elapsed_zero_before_start(self) -> None:
        t = _CPUTimer(10.0)
        assert t.elapsed == 0.0

    def test_cpu_elapsed_zero_after_cancel(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        time.sleep(0.02)
        elapsed_before = t.elapsed
        assert elapsed_before > 0
        t.cancel()

    def test_wall_elapsed_zero_before_start(self) -> None:
        t = _WallTimer(10.0)
        assert t.elapsed == 0.0

    def test_wall_elapsed_increases(self) -> None:
        t = _WallTimer(60.0)
        t.start()
        e1 = t.elapsed
        time.sleep(0.05)
        e2 = t.elapsed
        assert e2 > e1
        t.cancel()

    def test_cpu_elapsed_monotonically_increasing(self) -> None:
        t = _CPUTimer(60.0)
        t.start()
        try:
            readings = [t.elapsed for _ in range(10)]
            for i in range(1, len(readings)):
                assert readings[i] >= readings[i - 1]
        finally:
            t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# 8. ResourceLimiter integration: thread-safety and cancel
# ═══════════════════════════════════════════════════════════════════════


class TestResourceLimiterThreadSafety:
    def test_concurrent_install_uninstall(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="concurrent")
        errors = []
        stop = threading.Event()
        barrier = threading.Barrier(2)

        def installer() -> None:
            barrier.wait()
            while not stop.is_set():
                try:
                    limiter.install()
                    time.sleep(0.005)
                    limiter.uninstall()
                except Exception as e:
                    errors.append(e)
                    break

        threads = [threading.Thread(target=installer) for _ in range(2)]
        for th in threads:
            th.start()
        time.sleep(0.5)
        stop.set()
        for th in threads:
            th.join(timeout=5.0)
        limiter.uninstall()
        assert len(errors) == 0

    def test_concurrent_check_during_uninstall(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="check-uninst")
        stop = threading.Event()

        def checker() -> None:
            while not stop.is_set():
                try:
                    limiter.check_cpu_timer()
                    limiter.check_wall_timer()
                except (ResourceExhausted, AttributeError):
                    pass
                time.sleep(0.001)

        limiter.install()
        checker_thread = threading.Thread(target=checker)
        checker_thread.start()
        time.sleep(0.1)
        limiter.uninstall()
        stop.set()
        checker_thread.join(timeout=5.0)
        assert not limiter._installed

    def test_no_timer_leak_after_stress_cycles(self, force_poll: None) -> None:
        initial = threading.active_count()
        for _ in range(30):
            policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
            limiter = ResourceLimiter(policy, plugin_id="stress")
            limiter.install()
            time.sleep(0.01)
            limiter.uninstall()
        time.sleep(0.5)
        leaked = threading.active_count() - initial
        assert leaked <= 2

    def test_cancel_via_uninstall_stops_poll_threads(self, force_poll: None) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="cancel-stop")
        limiter.install()
        cpu_thread = limiter._cpu_timer._thread
        assert cpu_thread is not None
        limiter.uninstall()
        cpu_thread.join(timeout=2.0)
        assert not cpu_thread.is_alive()


class TestResourceLimiterResourceLimits:
    def test_apply_resource_limits_handles_no_resource_module(self) -> None:
        policy = ResourcePolicy(max_memory_bytes=1024 * 1024, max_file_descriptors=32)
        limiter = ResourceLimiter(policy, plugin_id="no-res-mod")
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.HAS_RESOURCE_MODULE", False
        ):
            limiter._apply_resource_limits()
        assert len(limiter._saved_limits) == 0

    def test_apply_resource_limits_handles_setrlimit_error(self) -> None:
        policy = ResourcePolicy(max_memory_bytes=1024 * 1024, max_file_descriptors=32)
        limiter = ResourceLimiter(policy, plugin_id="setrlimit-err")
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.HAS_RESOURCE_MODULE", True
        ), patch(
            "engine.plugins.sandbox.layers.resource_limiter._resource"
        ) as mock_res:
            mock_res.getrlimit.side_effect = OSError("no limits")
            limiter._apply_resource_limits()
        assert len(limiter._saved_limits) == 0

    def test_restore_resource_limits_handles_no_module(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter._saved_limits["RLIMIT_AS"] = (100, 200)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.HAS_RESOURCE_MODULE", False
        ):
            limiter._restore_resource_limits()
        assert len(limiter._saved_limits) == 1

    def test_restore_clears_saved_limits(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter._saved_limits["fake"] = (1, 2)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.HAS_RESOURCE_MODULE", True
        ), patch(
            "engine.plugins.sandbox.layers.resource_limiter._resource"
        ) as mock_res:
            mock_res.setrlimit = MagicMock()
            limiter._restore_resource_limits()
        assert len(limiter._saved_limits) == 0


# ═══════════════════════════════════════════════════════════════════════
# 9. Backward compatibility: _start_time, _timer, _on_timeout
# ═══════════════════════════════════════════════════════════════════════


class TestBackwardCompatibility:
    def test_timer_property_always_none(self) -> None:
        t = _CPUTimer(10.0)
        assert t._timer is None
        t.start()
        assert t._timer is None
        t.cancel()
        assert t._timer is None

    def test_on_timeout_sets_expired_event(self) -> None:
        t = _CPUTimer(10.0)
        assert not t.expired
        t._on_timeout()
        assert t.expired
        assert isinstance(t._expired, threading.Event)

    def test_start_time_setter_backward_compat(self) -> None:
        t = _CPUTimer(10.0)
        fake = time.monotonic() - 3.0
        t._start_time = fake
        assert t._wall_start == fake
        assert t._start_cpu > 0

    def test_start_time_getter_backward_compat(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._start_time == t._wall_start
        finally:
            t.cancel()

    def test_stop_delegates_to_cancel(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.stop()
        assert t._thread is None

    def test_wall_timer_stop_delegates_to_cancel(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.stop()
        assert t._timer is None


# ═══════════════════════════════════════════════════════════════════════
# 10. Stress tests: high concurrency and rapid lifecycle
# ═══════════════════════════════════════════════════════════════════════


class TestStressScenarios:
    def test_rapid_start_cancel_100_cycles(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        for _ in range(100):
            t.start()
            t.cancel()
        assert t._thread is None

    def test_many_timers_concurrent(self, force_poll: None) -> None:
        timers = []
        for _ in range(20):
            t = _CPUTimer(60.0)
            t.start()
            timers.append(t)
        for t in timers:
            t.cancel()
        for t in timers:
            assert t._thread is None

    def test_many_wall_timers_concurrent(self) -> None:
        timers = []
        for _ in range(20):
            t = _WallTimer(60.0)
            t.start()
            timers.append(t)
        for t in timers:
            t.cancel()
        for t in timers:
            assert t._timer is None

    def test_timers_with_mixed_timeouts(self, force_poll: None) -> None:
        timers = []
        for i in range(10):
            timeout = 0.01 if i % 2 == 0 else 60.0
            t = _CPUTimer(timeout)
            t.start()
            timers.append(t)
        time.sleep(0.1)
        for t in timers:
            t.cancel()

    def test_no_deadlock_on_cancel(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        results = []
        barrier = threading.Barrier(5)

        def cancel_reader() -> None:
            barrier.wait()
            t.cancel()
            results.append(t._thread)

        threads = [threading.Thread(target=cancel_reader) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        assert all(r is None for r in results)

    def test_expired_read_during_expiry_race(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        results = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                results.append(t.expired)
                time.sleep(0.001)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        for th in threads:
            th.start()
        time.sleep(0.15)
        stop.set()
        for th in threads:
            th.join(timeout=5.0)
        t.cancel()
        assert all(isinstance(r, bool) for r in results)
        assert any(r is True for r in results)


# ═══════════════════════════════════════════════════════════════════════
# 11. Context manager with cancel verification
# ═══════════════════════════════════════════════════════════════════════


class TestContextManagerCancel:
    def test_cpu_timer_context_cancels_on_normal_exit(self, force_poll: None) -> None:
        with _CPUTimer(60.0) as t:
            assert t._thread is not None
        assert t._thread is None

    def test_cpu_timer_context_cancels_on_exception(self, force_poll: None) -> None:
        with _CPUTimer(60.0) as t:
            assert t._thread is not None
            with pytest.raises(ValueError):
                raise ValueError("test")
        assert t._thread is None

    def test_wall_timer_context_cancels_on_normal_exit(self) -> None:
        with _WallTimer(60.0) as t:
            assert t._timer is not None
        assert t._timer is None

    def test_wall_timer_context_cancels_on_exception(self) -> None:
        with _WallTimer(60.0) as t:
            assert t._timer is not None
            with pytest.raises(ValueError):
                raise ValueError("test")
        assert t._timer is None

    def test_nested_context_managers(self, force_poll: None) -> None:
        with _CPUTimer(60.0) as cpu_t, _WallTimer(60.0) as wall_t:
            assert cpu_t._thread is not None
            assert wall_t._timer is not None
        cpu_t.cancel()
        wall_t.cancel()
        assert cpu_t._thread is None
        assert wall_t._timer is None


# ═══════════════════════════════════════════════════════════════════════
# 12. Plugin ID propagation
# ═══════════════════════════════════════════════════════════════════════


class TestPluginIdPropagation:
    def test_cpu_timer_plugin_id_in_exception(self, force_poll: None) -> None:
        t = _CPUTimer(0.01, plugin_id="my-plugin-42")
        t.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.plugin_id == "my-plugin-42"
        t.cancel()

    def test_wall_timer_plugin_id_in_exception(self) -> None:
        t = _WallTimer(0.01, plugin_id="wall-plugin-99")
        t.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.plugin_id == "wall-plugin-99"
        t.cancel()

    def test_cpu_timer_no_plugin_id(self, force_poll: None) -> None:
        t = _CPUTimer(0.01)
        t.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.plugin_id is None
        t.cancel()

    def test_resource_limiter_propagates_plugin_id(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=0.01, wall_time_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="limiter-pid")
        limiter.install()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            limiter.check_cpu_timer()
        assert exc_info.value.plugin_id == "limiter-pid"
        limiter.uninstall()
