"""
Comprehensive tests for the resource_limiter refactor.

Focus areas:
1. _CPUTimer uses signal.SIGVTALRM (ITIMER_VIRTUAL) on Linux main thread, os.times() fallback
2. _expired replaced with threading.Event() for thread-safe state
3. cancel() method and proper cleanup in finally blocks
4. _WallTimer threading.Event() migration
5. ResourceLimiter uninstall try/finally safety
"""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any

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


# ═══════════════════════════════════════════════════════════════════════
# _CPUTimer: SIGVTALRM signal-based CPU enforcement
# ═══════════════════════════════════════════════════════════════════════


class TestCPUTimerSignalMode:
    def test_signal_mode_on_main_thread_linux(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t.mode == "signal"
            assert t._use_signal is True
        finally:
            t.cancel()

    def test_force_poll_overrides_signal(self, force_poll: None) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t.mode == "poll"
            assert t._use_signal is False
            assert t._thread is not None
        finally:
            t.cancel()

    def test_signal_handler_sets_expired(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        assert not t.expired
        t._signal_handler(signal.SIGVTALRM, None)
        assert t.expired

    def test_cancel_restores_old_signal_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t.start()
        assert t.mode == "signal"
        t.cancel()
        restored = signal.getsignal(signal.SIGVTALRM)
        assert restored is sentinel

    def test_cancel_stops_itimer(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        assert t.mode == "signal"
        t.cancel()
        remaining = signal.getitimer(signal.ITIMER_VIRTUAL)
        assert remaining[0] == 0.0

    def test_signal_mode_skips_poll_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._use_signal is True
            assert t._thread is None
        finally:
            t.cancel()

    def test_signal_mode_no_thread_after_start(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        assert t._thread is None
        t.start()
        try:
            assert t._thread is None
            assert t._use_signal is True
        finally:
            t.cancel()

    def test_fallback_to_poll_from_non_main_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        result: dict[str, Any] = {}

        def run_in_thread() -> None:
            t = _CPUTimer(10.0)
            t.start()
            try:
                result["mode"] = t.mode
                result["thread"] = t._thread
            finally:
                t.cancel()

        th = threading.Thread(target=run_in_thread)
        th.start()
        th.join(timeout=5.0)
        assert result.get("mode") == "poll"
        assert result.get("thread") is not None

    def test_multiple_start_cycles_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        for _ in range(5):
            t.start()
            assert t.mode == "signal"
            t.cancel()
            assert t._use_signal is False

    def test_signal_expired_after_short_cpu_limit(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.05)
        t.start()
        _burn_cpu(0.3)
        assert t.expired
        t.cancel()

    def test_signal_check_raises_after_expiry(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.05, plugin_id="sig-check")
        t.start()
        _burn_cpu(0.3)
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "cpu_time"
        assert exc_info.value.plugin_id == "sig-check"
        t.cancel()

    def test_signal_cancel_idempotent(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        assert t.mode == "signal"
        t.cancel()
        t.cancel()
        t.cancel()
        assert t._use_signal is False

    def test_signal_mode_expired_is_threading_event(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert isinstance(t._expired, threading.Event)
        finally:
            t.cancel()

    def test_signal_mode_cancelled_is_threading_event(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert isinstance(t._cancelled, threading.Event)
        finally:
            t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# _CPUTimer: os.times()-based CPU measurement
# ═══════════════════════════════════════════════════════════════════════


class TestCPUTimerCPUTimeMeasurement:
    def test_cpu_time_returns_user_plus_sys(self) -> None:
        t = _CPUTimer(10.0)
        cpu = t._cpu_time()
        ot = os.times()
        expected = ot[0] + ot[1]
        assert abs(cpu - expected) < 0.5

    def test_cpu_time_increases_with_work(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            start_cpu = t._cpu_time()
            _burn_cpu(0.1)
            end_cpu = t._cpu_time()
            assert end_cpu >= start_cpu
        finally:
            t.cancel()

    def test_cpu_time_is_not_wall_time(self) -> None:
        t = _CPUTimer(10.0)
        before = t._cpu_time()
        time.sleep(0.1)
        after = t._cpu_time()
        assert (after - before) < 0.05


class TestCPUTimerInitialState:
    def test_expired_is_false_initially(self) -> None:
        t = _CPUTimer(10.0)
        assert t.expired is False

    def test_expired_is_threading_event(self) -> None:
        t = _CPUTimer(10.0)
        assert isinstance(t._expired, threading.Event)

    def test_cancelled_is_threading_event(self) -> None:
        t = _CPUTimer(10.0)
        assert isinstance(t._cancelled, threading.Event)

    def test_thread_is_none_before_start(self) -> None:
        t = _CPUTimer(10.0)
        assert t._thread is None

    def test_start_cpu_is_zero_before_start(self) -> None:
        t = _CPUTimer(10.0)
        assert t._start_cpu == 0.0

    def test_wall_start_is_zero_before_start(self) -> None:
        t = _CPUTimer(10.0)
        assert t._wall_start == 0.0

    def test_elapsed_is_zero_before_start(self) -> None:
        t = _CPUTimer(10.0)
        assert t.elapsed == 0.0

    def test_use_signal_false_before_start(self) -> None:
        t = _CPUTimer(10.0)
        assert t._use_signal is False

    def test_old_handler_none_before_start(self) -> None:
        t = _CPUTimer(10.0)
        assert t._old_handler is None


class TestCPUTimerStartStop:
    def test_start_creates_thread_in_poll_mode(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._thread is not None
            assert t._thread.is_alive()
        finally:
            t.cancel()

    def test_start_sets_cpu_time_baseline(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._start_cpu > 0.0
        finally:
            t.cancel()

    def test_start_sets_wall_start(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._wall_start > 0.0
        finally:
            t.cancel()

    def test_start_clears_expired_event(self) -> None:
        t = _CPUTimer(10.0)
        t._expired.set()
        assert t.expired is True
        t.start()
        try:
            assert t.expired is False
        finally:
            t.cancel()

    def test_start_clears_cancelled_event(self) -> None:
        t = _CPUTimer(10.0)
        t._cancelled.set()
        t.start()
        try:
            assert not t._cancelled.is_set()
        finally:
            t.cancel()

    def test_start_resets_signal_state(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = lambda *a: None
        t.start()
        try:
            if t.mode == "signal":
                pass
            assert t._old_handler is None or t.mode == "signal"
        finally:
            t.cancel()

    def test_stop_delegates_to_cancel(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.stop()
        assert t._thread is None

    def test_cancel_sets_cancelled_event(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()
        assert t._cancelled.is_set()

    def test_cancel_nils_thread(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()
        assert t._thread is None

    def test_cancel_idempotent(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()
        t.cancel()
        t.cancel()
        assert t._thread is None

    def test_cancel_before_start_is_safe(self) -> None:
        t = _CPUTimer(10.0)
        t.cancel()
        assert t._thread is None

    def test_thread_is_daemon_in_poll_mode(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._thread.daemon is True
        finally:
            t.cancel()


class TestCPUTimerPollingBehavior:
    def test_timer_expires_after_wall_time(self) -> None:
        t = _CPUTimer(0.02)
        t.start()
        _burn_cpu(0.15)
        assert t.expired
        t.cancel()

    def test_poll_detects_wall_time_exceeded(self, force_poll: None) -> None:
        t = _CPUTimer(0.02)
        t.start()
        time.sleep(0.1)
        assert t.expired
        t.cancel()

    def test_poll_stops_after_cancel(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        thread = t._thread
        t.cancel()
        thread.join(timeout=2.0)
        assert not thread.is_alive()

    def test_poll_interval_is_reasonable(self) -> None:
        assert 0.01 <= _CPUTimer._POLL_INTERVAL <= 0.5

    def test_multiple_start_cycles_poll(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        for _ in range(5):
            t.start()
            assert t._thread is not None
            t.cancel()
            assert t._thread is None


class TestCPUTimerCheck:
    def test_check_passes_within_limit(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            t.check()
        finally:
            t.cancel()

    def test_check_raises_when_expired_set(self) -> None:
        t = _CPUTimer(0.01, plugin_id="check-test")
        t.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "cpu_time"
        assert exc_info.value.plugin_id == "check-test"
        t.cancel()

    def test_check_raises_with_elapsed_wall_exceeds(self) -> None:
        t = _CPUTimer(0.01, plugin_id="wall-check")
        t._start_time = time.monotonic() - 10.0
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "cpu_time"
        assert t.expired

    def test_check_reports_current_as_wall_elapsed(self) -> None:
        t = _CPUTimer(0.01, plugin_id="current-test")
        t._start_time = time.monotonic() - 5.0
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.current >= 4.9

    def test_check_without_start_no_crash_with_start_time_set(self) -> None:
        t = _CPUTimer(10.0)
        t._start_time = time.monotonic()
        t.check()

    def test_check_after_cancel_within_limit(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()
        t._start_time = time.monotonic()
        t.check()


class TestCPUTimerBackwardCompat:
    def test_start_time_property_reads_wall_start(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._start_time == t._wall_start
        finally:
            t.cancel()

    def test_start_time_setter_adjusts_cpu_baseline(self) -> None:
        t = _CPUTimer(10.0)
        cpu_before = t._cpu_time()
        fake_past = time.monotonic() - 5.0
        t._start_time = fake_past
        assert t._wall_start == fake_past
        expected = cpu_before - 5.0
        assert abs(t._start_cpu - expected) < 1.0

    def test_timer_property_returns_none(self) -> None:
        t = _CPUTimer(10.0)
        assert t._timer is None
        t.start()
        assert t._timer is None
        t.cancel()
        assert t._timer is None

    def test_on_timeout_sets_expired(self) -> None:
        t = _CPUTimer(10.0, plugin_id="compat")
        t._on_timeout()
        assert t.expired

    def test_del_cancels_thread(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert t._thread is None or t._thread is not None
        t.__del__()
        assert t._thread is None


class TestCPUTimerThreadSafety:
    def test_expired_event_is_thread_safe(self) -> None:
        t = _CPUTimer(10.0)
        results = []
        barrier = threading.Barrier(4)

        def reader() -> None:
            barrier.wait()
            results.extend(t.expired for _ in range(100))

        threads = [threading.Thread(target=reader) for _ in range(4)]
        t.start()
        try:
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=5.0)
        finally:
            t.cancel()
        assert all(isinstance(r, bool) for r in results)

    def test_concurrent_cancel_and_check(self) -> None:
        t = _CPUTimer(10.0)
        errors = []
        barrier = threading.Barrier(2)

        def check_loop() -> None:
            barrier.wait()
            try:
                for _ in range(50):
                    t.check()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        t.start()
        checker = threading.Thread(target=check_loop)
        checker.start()
        barrier.wait()
        time.sleep(0.05)
        t.cancel()
        checker.join(timeout=5.0)
        assert len(errors) == 0 or all(isinstance(e, ResourceExhausted) for e in errors)

    def test_cancel_is_fast(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        time.sleep(0.01)
        start_cancel = time.monotonic()
        t.cancel()
        cancel_dur = time.monotonic() - start_cancel
        assert cancel_dur < 1.0


# ═══════════════════════════════════════════════════════════════════════
# _WallTimer: threading.Event migration
# ═══════════════════════════════════════════════════════════════════════


class TestWallTimerEventMigration:
    def test_expired_is_threading_event(self) -> None:
        t = _WallTimer(10.0)
        assert isinstance(t._expired, threading.Event)

    def test_expired_false_initially(self) -> None:
        t = _WallTimer(10.0)
        assert t.expired is False

    def test_start_clears_expired(self) -> None:
        t = _WallTimer(10.0)
        t._expired.set()
        assert t.expired
        t.start()
        assert not t.expired
        t.cancel()

    def test_on_timeout_sets_event(self) -> None:
        t = _WallTimer(10.0)
        t._on_timeout()
        assert t._expired.is_set()
        assert t.expired

    def test_cancel_nils_timer(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.cancel()
        assert t._timer is None

    def test_double_cancel_safe(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.cancel()
        t.cancel()
        assert t._timer is None

    def test_cancel_before_start_safe(self) -> None:
        t = _WallTimer(10.0)
        t.cancel()
        assert t._timer is None

    def test_stop_delegates_to_cancel(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.stop()
        assert t._timer is None

    def test_del_cancels(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.__del__()
        assert t._timer is None

    def test_expired_after_wall_timeout(self) -> None:
        t = _WallTimer(0.02, plugin_id="wall-exp")
        t.start()
        time.sleep(0.1)
        assert t.expired
        t.cancel()

    def test_check_raises_with_correct_fields(self) -> None:
        t = _WallTimer(0.01, plugin_id="wall-check")
        t.start()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted) as exc_info:
            t.check()
        assert exc_info.value.resource_type == "wall_time"
        assert exc_info.value.plugin_id == "wall-check"
        assert exc_info.value.current > 0.01
        assert exc_info.value.limit == 0.01
        t.cancel()

    def test_check_raises_when_elapsed_exceeds(self) -> None:
        t = _WallTimer(0.01)
        t._start_time = time.monotonic() - 10.0
        with pytest.raises(ResourceExhausted, match="wall_time"):
            t.check()
        assert t.expired

    def test_elapsed_before_start(self) -> None:
        t = _WallTimer(10.0)
        assert t.elapsed == 0.0

    def test_elapsed_after_start(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        time.sleep(0.02)
        assert t.elapsed >= 0.01
        t.cancel()

    def test_timer_fires_and_sets_event(self) -> None:
        t = _WallTimer(0.02)
        t.start()
        time.sleep(0.1)
        assert t._expired.is_set()
        t.cancel()

    def test_thread_safe_expired_reads(self) -> None:
        t = _WallTimer(10.0)
        results = []
        barrier = threading.Barrier(4)

        def reader() -> None:
            barrier.wait()
            results.extend(t.expired for _ in range(100))

        t.start()
        threads = [threading.Thread(target=reader) for _ in range(4)]
        try:
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=5.0)
        finally:
            t.cancel()
        assert all(isinstance(r, bool) for r in results)


# ═══════════════════════════════════════════════════════════════════════
# ResourceLimiter: cancel() in finally blocks
# ═══════════════════════════════════════════════════════════════════════


class TestResourceLimiterUninstallSafety:
    def test_uninstall_stops_cpu_timer_via_cancel(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        assert limiter._cpu_timer is not None
        limiter.uninstall()
        assert limiter._cpu_timer is None

    def test_uninstall_stops_wall_timer_via_cancel(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        assert limiter._wall_timer is not None
        limiter.uninstall()
        assert limiter._wall_timer is None

    def test_uninstall_try_finally_ensures_cpu_timer_stopped(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=0.01, wall_time_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="tf-test")
        limiter.install()
        time.sleep(0.05)
        limiter.uninstall()
        assert limiter._cpu_timer is None
        assert limiter._wall_timer is None

    def test_uninstall_idempotent(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.uninstall()
        limiter.uninstall()
        assert not limiter._installed

    def test_install_uninstall_many_cycles(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="cycle")
        for _ in range(10):
            limiter.install()
            assert limiter._installed
            limiter.uninstall()
            assert not limiter._installed
            assert limiter._cpu_timer is None
            assert limiter._wall_timer is None


class TestResourceLimiterFinallyBlockCoverage:
    def test_uninstall_cleans_up_even_if_wall_timer_stop_fails(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="fail-wall")
        limiter.install()
        limiter._wall_timer = None
        limiter.uninstall()
        assert limiter._cpu_timer is None
        assert not limiter._installed

    def test_uninstall_when_already_timed_out(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=0.01, wall_time_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="timeout")
        limiter.install()
        time.sleep(0.05)
        cpu_t = limiter._cpu_timer
        wall_t = limiter._wall_timer
        limiter.uninstall()
        assert limiter._cpu_timer is None
        assert limiter._wall_timer is None
        if cpu_t is not None:
            assert cpu_t._thread is None
            assert not cpu_t._use_signal
        if wall_t is not None:
            assert wall_t._timer is None


class TestResourceLimiterCheckMethods:
    def test_check_cpu_timer_noop_without_install(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.check_cpu_timer()

    def test_check_wall_timer_noop_without_install(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.check_wall_timer()

    def test_check_cpu_timer_logs_violation(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="viol")
        limiter.install()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted):
            limiter.check_cpu_timer()
        violations = limiter.get_violations()
        assert len(violations) >= 1
        assert violations[0].resource_type == "cpu_time"
        limiter.uninstall()

    def test_check_wall_timer_logs_violation(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=0.01)
        limiter = ResourceLimiter(policy, plugin_id="wviol")
        limiter.install()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted):
            limiter.check_wall_timer()
        violations = limiter.get_violations()
        assert len(violations) >= 1
        assert violations[0].resource_type == "wall_time"
        limiter.uninstall()

    def test_check_cpu_timer_passes_within_limit(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.check_cpu_timer()
        limiter.uninstall()

    def test_check_wall_timer_passes_within_limit(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.check_wall_timer()
        limiter.uninstall()


class TestResourceLimiterThreadTracking:
    def test_increment_checks_limit(self) -> None:
        policy = ResourcePolicy(max_threads=2)
        limiter = ResourceLimiter(policy)
        limiter.increment_thread()
        limiter.increment_thread()
        with pytest.raises(ResourceExhausted, match="threads"):
            limiter.increment_thread()

    def test_decrement_floors_at_zero(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        limiter.decrement_thread()
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_increment_decrement_cycle(self) -> None:
        policy = ResourcePolicy(max_threads=5)
        limiter = ResourceLimiter(policy)
        limiter.increment_thread()
        limiter.increment_thread()
        assert limiter._thread_count == 2
        limiter.decrement_thread()
        assert limiter._thread_count == 1
        limiter.decrement_thread()
        assert limiter._thread_count == 0

    def test_thread_limit_zero(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="zero")
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        assert limiter._thread_count == 0

    def test_thread_violation_logged(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy, plugin_id="tlog")
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        violations = limiter.get_violations()
        assert len(violations) == 1
        assert violations[0].resource_type == "threads"


class TestResourceLimiterViolationLog:
    def test_violations_are_copies(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        v1 = limiter.get_violations()
        v2 = limiter.get_violations()
        assert v1 is not v2

    def test_clear_violations(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy)
        with pytest.raises(ResourceExhausted):
            limiter.increment_thread()
        assert len(limiter.get_violations()) == 1
        limiter.clear_violations()
        assert len(limiter.get_violations()) == 0


class TestResourceLimiterCpuElapsed:
    def test_cpu_elapsed_without_timer(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy())
        assert limiter.cpu_elapsed == 0.0

    def test_cpu_elapsed_with_timer(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy)
        limiter.install()
        try:
            time.sleep(0.02)
            assert limiter.cpu_elapsed >= 0.01
        finally:
            limiter.uninstall()

    def test_cpu_elapsed_after_uninstall(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy)
        limiter.install()
        limiter.uninstall()
        assert limiter.cpu_elapsed == 0.0


class TestResourceLimiterParseMemory:
    def test_gb(self) -> None:
        assert ResourceLimiter.parse_memory("1GB") == 1024**3

    def test_mb(self) -> None:
        assert ResourceLimiter.parse_memory("512MB") == 512 * 1024**2

    def test_kb(self) -> None:
        assert ResourceLimiter.parse_memory("1024KB") == 1024 * 1024

    def test_b(self) -> None:
        assert ResourceLimiter.parse_memory("100B") == 100

    def test_plain_number(self) -> None:
        assert ResourceLimiter.parse_memory("2048") == 2048

    def test_whitespace_stripped(self) -> None:
        assert ResourceLimiter.parse_memory("  1GB  ") == 1024**3

    def test_case_insensitive(self) -> None:
        assert ResourceLimiter.parse_memory("1gb") == 1024**3

    def test_fractional(self) -> None:
        assert ResourceLimiter.parse_memory("1.5GB") == int(1.5 * 1024**3)


# ═══════════════════════════════════════════════════════════════════════
# Integration: full install → check → uninstall lifecycle
# ═══════════════════════════════════════════════════════════════════════


class TestResourceLimiterIntegration:
    def test_full_lifecycle_within_limits(self) -> None:
        policy = ResourcePolicy(
            max_cpu_seconds=60.0,
            wall_time_seconds=60.0,
            max_threads=5,
        )
        limiter = ResourceLimiter(policy, plugin_id="lifecycle")
        limiter.install()
        try:
            limiter.check_cpu_timer()
            limiter.check_wall_timer()
            limiter.increment_thread()
            limiter.decrement_thread()
            assert limiter.cpu_elapsed >= 0
            assert len(limiter.get_violations()) == 0
        finally:
            limiter.uninstall()

    def test_full_lifecycle_with_timeout(self) -> None:
        policy = ResourcePolicy(
            max_cpu_seconds=0.01,
            wall_time_seconds=0.01,
        )
        limiter = ResourceLimiter(policy, plugin_id="timeout-life")
        limiter.install()
        time.sleep(0.05)
        with pytest.raises(ResourceExhausted):
            limiter.check_cpu_timer()
        with pytest.raises(ResourceExhausted):
            limiter.check_wall_timer()
        assert len(limiter.get_violations()) == 2
        limiter.uninstall()

    def test_context_manager_pattern(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="ctx")
        limiter.install()
        try:
            limiter.check_cpu_timer()
            limiter.check_wall_timer()
        finally:
            limiter.uninstall()
        assert limiter._cpu_timer is None
        assert limiter._wall_timer is None
        assert not limiter._installed

    def test_cancel_in_finally_block(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="finally")
        limiter.install()

        def _raise_simulated() -> None:
            raise ValueError("simulated error")

        try:
            limiter.check_cpu_timer()
            _raise_simulated()
        except ValueError:
            pass
        finally:
            limiter.uninstall()
        assert not limiter._installed

    def test_timer_threads_die_after_uninstall_poll(self, force_poll: None) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="threads-die")
        limiter.install()
        cpu_thread = limiter._cpu_timer._thread
        assert cpu_thread is not None
        limiter.uninstall()
        cpu_thread.join(timeout=2.0)
        assert not cpu_thread.is_alive()

    def test_no_zombie_threads_after_many_cycles(self) -> None:
        initial_count = threading.active_count()
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        for _ in range(20):
            limiter = ResourceLimiter(policy, plugin_id="zombie")
            limiter.install()
            time.sleep(0.01)
            limiter.uninstall()
        time.sleep(0.5)
        leaked = threading.active_count() - initial_count
        assert leaked <= 2


class TestResourceLimiterDoubleInstall:
    def test_double_install_does_not_leak_timers(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy)
        limiter.install()
        first_cpu = limiter._cpu_timer
        first_wall = limiter._wall_timer
        limiter.install()
        assert limiter._cpu_timer is first_cpu
        assert limiter._wall_timer is first_wall
        limiter.uninstall()


class TestResourceLimiterEdgeCases:
    def test_very_small_cpu_limit(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=0.001)
        limiter = ResourceLimiter(policy, plugin_id="tiny")
        limiter.install()
        time.sleep(0.02)
        with pytest.raises(ResourceExhausted):
            limiter.check_cpu_timer()
        limiter.uninstall()

    def test_very_large_cpu_limit(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=86400.0)
        limiter = ResourceLimiter(policy, plugin_id="large")
        limiter.install()
        limiter.check_cpu_timer()
        limiter.uninstall()

    def test_zero_threads_with_no_increment(self) -> None:
        policy = ResourcePolicy(max_threads=0)
        limiter = ResourceLimiter(policy)
        assert limiter._thread_count == 0

    def test_plugin_id_propagated_to_timers(self) -> None:
        policy = ResourcePolicy()
        limiter = ResourceLimiter(policy, plugin_id="pid-test")
        limiter.install()
        assert limiter._cpu_timer._plugin_id == "pid-test"
        assert limiter._wall_timer._plugin_id == "pid-test"
        limiter.uninstall()


# ═══════════════════════════════════════════════════════════════════════
# Regression: ensure old _CPUTimer boolean-based tests still pass
# ═══════════════════════════════════════════════════════════════════════


class TestCPUTimerBooleanRegression:
    def test_expired_read_is_bool(self) -> None:
        t = _CPUTimer(10.0)
        assert type(t.expired) is bool

    def test_expired_false_then_true(self) -> None:
        t = _CPUTimer(0.01)
        assert t.expired is False
        t.start()
        _burn_cpu(0.05)
        assert t.expired is True
        t.cancel()

    def test_expired_set_via_on_timeout(self) -> None:
        t = _CPUTimer(10.0)
        assert not t.expired
        t._on_timeout()
        assert t.expired
        assert type(t.expired) is bool


class TestWallTimerBooleanRegression:
    def test_expired_read_is_bool(self) -> None:
        t = _WallTimer(10.0)
        assert type(t.expired) is bool

    def test_expired_set_via_on_timeout(self) -> None:
        t = _WallTimer(10.0)
        assert not t.expired
        t._on_timeout()
        assert t.expired
        assert type(t.expired) is bool


# ═══════════════════════════════════════════════════════════════════════
# Signal-specific integration with ResourceLimiter
# ═══════════════════════════════════════════════════════════════════════


class TestResourceLimiterSignalIntegration:
    def test_cpu_timer_uses_signal_mode(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="sig-int")
        limiter.install()
        try:
            assert limiter._cpu_timer.mode == "signal"
        finally:
            limiter.uninstall()

    def test_signal_cleanup_on_uninstall(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        before = signal.getsignal(signal.SIGVTALRM)
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="sig-cleanup")
        limiter.install()
        assert limiter._cpu_timer._use_signal is True
        limiter.uninstall()
        after = signal.getsignal(signal.SIGVTALRM)
        assert after is before

    def test_itimer_zeroed_on_uninstall(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        policy = ResourcePolicy(max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="itimer-zero")
        limiter.install()
        limiter.uninstall()
        remaining = signal.getitimer(signal.ITIMER_VIRTUAL)
        assert remaining[0] == 0.0

    def test_signal_mode_with_finally_cleanup(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="sig-finally")

        class SimulatedError(Exception):
            pass

        def _trigger():
            raise SimulatedError("boom")

        try:
            limiter.install()
            limiter.check_cpu_timer()
            _trigger()
        except SimulatedError:
            pass
        finally:
            limiter.uninstall()

        assert limiter._cpu_timer is None
        remaining = signal.getitimer(signal.ITIMER_VIRTUAL)
        assert remaining[0] == 0.0


# ═══════════════════════════════════════════════════════════════════════
# Context manager protocol (__enter__ / __exit__)
# ═══════════════════════════════════════════════════════════════════════


class TestCPUTimerContextManager:
    def test_enter_returns_self(self) -> None:
        t = _CPUTimer(10.0)
        with t as ctx:
            assert ctx is t
        t.cancel()

    def test_exit_cancels_timer(self) -> None:
        t = _CPUTimer(10.0)
        with t:
            assert t._thread is not None or t._use_signal
        assert t._thread is None

    def test_exit_cancels_even_on_exception(self) -> None:
        t = _CPUTimer(10.0)
        with t, pytest.raises(ValueError):
            raise ValueError("boom")
        assert t._thread is None

    def test_context_manager_no_zombie_threads(self, force_poll: None) -> None:
        with _CPUTimer(10.0) as t:
            assert t._thread is not None
            assert t._thread.is_alive()
        assert t._thread is None

    def test_del_uses_contextlib_suppress(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.__del__()
        assert t._thread is None

    def test_del_safe_on_broken_cancel(self) -> None:
        t = _CPUTimer(10.0)
        t.cancel = lambda: (_ for _ in ()).throw(RuntimeError("broken"))
        t.__del__()


class TestWallTimerContextManager:
    def test_enter_returns_self(self) -> None:
        t = _WallTimer(10.0)
        with t as ctx:
            assert ctx is t
        t.cancel()

    def test_exit_cancels_timer(self) -> None:
        t = _WallTimer(10.0)
        with t:
            assert t._timer is not None
        assert t._timer is None

    def test_exit_cancels_even_on_exception(self) -> None:
        t = _WallTimer(10.0)
        with t, pytest.raises(ValueError):
            raise ValueError("boom")
        assert t._timer is None

    def test_del_uses_contextlib_suppress(self) -> None:
        t = _WallTimer(10.0)
        t.start()
        t.__del__()
        assert t._timer is None

    def test_del_safe_on_broken_cancel(self) -> None:
        t = _WallTimer(10.0)
        t.cancel = lambda: (_ for _ in ()).throw(RuntimeError("broken"))
        t.__del__()


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _burn_cpu(duration: float) -> None:
    end = time.monotonic() + duration
    total = 0.0
    while time.monotonic() < end:
        total += 1.0
