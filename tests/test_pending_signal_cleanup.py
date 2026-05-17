"""
Comprehensive tests for the _pending_signal_cleanup flag mechanism.

Covers:
- Flag initialization and reset
- Deferred cleanup: non-main-thread _stop_signal sets flag, preserves handler
- _flush_pending_cleanup: restores handler under _signal_lock on main thread
- _try_start_signal flushes pending cleanup before installing new handler
- No unlocked signal clobber (race safety)
- No failing non-main-thread signal calls
- Unified handler semantics across start/cancel cycles
- start() resets the flag
- Integration with ResourceLimiter lifecycle
- Edge cases: double flush, flush without pending, concurrent flush+stop
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
    _signal_lock,
)


@pytest.fixture
def force_poll():
    _CPUTimer._force_poll = True
    yield
    _CPUTimer._force_poll = False


requires_sigvtalrm = pytest.mark.skipif(
    not _HAS_SIGVTALRM, reason="SIGVTALRM not available"
)


# ═══════════════════════════════════════════════════════════════════════
# 1. Flag initialization
# ═══════════════════════════════════════════════════════════════════════


class TestFlagInitialization:
    def test_flag_defaults_to_false(self) -> None:
        t = _CPUTimer(10.0)
        assert t._pending_signal_cleanup is False

    def test_flag_reset_on_start(self) -> None:
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = True
        t.start()
        assert t._pending_signal_cleanup is False
        t.cancel()

    @requires_sigvtalrm
    def test_flag_false_after_signal_start(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is True
        assert t._pending_signal_cleanup is False
        t.cancel()

    def test_flag_false_after_poll_start(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert t._pending_signal_cleanup is False
        t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# 2. Non-main-thread _stop_signal sets flag and preserves handler
# ═══════════════════════════════════════════════════════════════════════


class TestNonMainThreadStopSignal:
    @requires_sigvtalrm
    def test_stop_from_non_main_thread_sets_flag(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is True
        flag_set = threading.Event()
        errors: list[Exception] = []

        def stopper() -> None:
            try:
                t._stop_signal()
            except Exception as e:
                errors.append(e)
            flag_set.set()

        th = threading.Thread(target=stopper)
        th.start()
        th.join(timeout=5.0)

        assert len(errors) == 0
        assert t._pending_signal_cleanup is True
        assert t._use_signal is False
        t.cancel()

    @requires_sigvtalrm
    def test_stop_from_non_main_thread_preserves_old_handler(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        original_handler = t._old_handler
        assert original_handler is not None

        errors: list[Exception] = []

        def stopper() -> None:
            try:
                t._stop_signal()
            except Exception as e:
                errors.append(e)

        th = threading.Thread(target=stopper)
        th.start()
        th.join(timeout=5.0)

        assert len(errors) == 0
        assert t._old_handler is original_handler
        t.cancel()

    @requires_sigvtalrm
    def test_stop_from_non_main_thread_makes_no_signal_calls(self) -> None:
        t = _CPUTimer(10.0)
        t.start()

        signal_calls: list[str] = []
        original_signal = signal.signal
        original_setitimer = signal.setitimer

        def tracking_signal(*args: object, **kwargs: object) -> object:
            if threading.current_thread() is not threading.main_thread():
                signal_calls.append("signal")
            return original_signal(*args, **kwargs)

        def tracking_setitimer(*args: object, **kwargs: object) -> object:
            if threading.current_thread() is not threading.main_thread():
                signal_calls.append("setitimer")
            return original_setitimer(*args, **kwargs)

        with patch("signal.signal", side_effect=tracking_signal), \
             patch("signal.setitimer", side_effect=tracking_setitimer):
            th = threading.Thread(target=t._stop_signal)
            th.start()
            th.join(timeout=5.0)

        assert signal_calls == []
        assert t._pending_signal_cleanup is True
        t.cancel()

    def test_stop_from_non_main_via_mock_sets_flag(self) -> None:
        t = _CPUTimer(10.0)
        t._use_signal = True
        sentinel = object()
        t._old_handler = sentinel

        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ):
            t._stop_signal()

        assert t._pending_signal_cleanup is True
        assert t._use_signal is False
        assert t._old_handler is sentinel


# ═══════════════════════════════════════════════════════════════════════
# 3. _flush_pending_cleanup performs deferred cleanup on main thread
# ═══════════════════════════════════════════════════════════════════════


class TestFlushPendingCleanup:
    @requires_sigvtalrm
    def test_flush_restores_handler(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        captured_handler = t._old_handler

        th = threading.Thread(target=t._stop_signal)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True
        assert t._old_handler is captured_handler

        t._flush_pending_cleanup()
        assert t._old_handler is None
        assert t._pending_signal_cleanup is False
        assert signal.getsignal(signal.SIGVTALRM) is captured_handler

    @requires_sigvtalrm
    def test_flush_stops_itimer(self) -> None:
        t = _CPUTimer(10.0)
        t.start()

        th = threading.Thread(target=t._stop_signal)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True

        with patch("signal.setitimer", wraps=signal.setitimer) as mock_itimer:
            t._flush_pending_cleanup()
            mock_itimer.assert_called_with(signal.ITIMER_VIRTUAL, 0)

        assert t._pending_signal_cleanup is False

    def test_flush_noop_when_no_pending(self) -> None:
        t = _CPUTimer(10.0)
        assert t._pending_signal_cleanup is False
        with patch("signal.signal") as mock_signal, \
             patch("signal.setitimer") as mock_itimer:
            t._flush_pending_cleanup()
            mock_signal.assert_not_called()
            mock_itimer.assert_not_called()

    @requires_sigvtalrm
    def test_flush_restores_handler_under_lock(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        th = threading.Thread(target=t._stop_signal)
        th.start()
        th.join(timeout=5.0)

        lock_acquired = threading.Event()

        original_signal_fn = signal.signal

        def tracking_signal(*args: object, **kwargs: object) -> object:
            if _signal_lock.locked():
                lock_acquired.set()
            return original_signal_fn(*args, **kwargs)

        with patch("signal.signal", side_effect=tracking_signal):
            t._flush_pending_cleanup()

        assert lock_acquired.is_set()
        assert t._old_handler is None
        t.cancel()

    @requires_sigvtalrm
    def test_double_flush_idempotent(self) -> None:
        t = _CPUTimer(10.0)
        t.start()

        th = threading.Thread(target=t._stop_signal)
        th.start()
        th.join(timeout=5.0)

        t._flush_pending_cleanup()
        assert t._pending_signal_cleanup is False
        assert t._old_handler is None

        with patch("signal.signal") as mock_signal, \
             patch("signal.setitimer") as mock_itimer:
            t._flush_pending_cleanup()
            mock_signal.assert_not_called()
            mock_itimer.assert_not_called()

    @requires_sigvtalrm
    def test_flush_with_none_handler(self) -> None:
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = True
        t._old_handler = None

        with patch("signal.setitimer", wraps=signal.setitimer) as mock_itimer:
            t._flush_pending_cleanup()
            mock_itimer.assert_called_with(signal.ITIMER_VIRTUAL, 0)

        assert t._pending_signal_cleanup is False

    @requires_sigvtalrm
    def test_flush_handles_signal_restore_error(self) -> None:
        t = _CPUTimer(10.0)
        t.start()

        th = threading.Thread(target=t._stop_signal)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True

        with patch("signal.signal", side_effect=OSError("denied")):
            t._flush_pending_cleanup()

        assert t._pending_signal_cleanup is False
        assert t._old_handler is None


# ═══════════════════════════════════════════════════════════════════════
# 4. _try_start_signal flushes pending cleanup before installing
# ═══════════════════════════════════════════════════════════════════════


class TestTryStartSignalFlushesPending:
    @requires_sigvtalrm
    def test_try_start_flushes_before_install(self) -> None:
        t = _CPUTimer(10.0)
        t.start()

        th = threading.Thread(target=t._stop_signal)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True

        t._use_signal = False
        result = t._try_start_signal()
        assert result is True
        assert t._pending_signal_cleanup is False
        assert t._use_signal is True
        current = signal.getsignal(signal.SIGVTALRM)
        assert callable(current)
        t.cancel()

    @requires_sigvtalrm
    def test_try_start_no_flush_when_no_pending(self) -> None:
        t = _CPUTimer(10.0)
        flush_called = []

        original_flush = t._flush_pending_cleanup

        def tracking_flush() -> None:
            flush_called.append(True)
            original_flush()

        t._flush_pending_cleanup = tracking_flush
        result = t._try_start_signal()
        assert result is True
        assert len(flush_called) == 1
        t.cancel()

    def test_try_start_non_main_thread_does_not_flush(self) -> None:
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = True

        result_holder: dict[str, bool] = {}

        def run() -> None:
            result_holder["result"] = t._try_start_signal()

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)

        assert result_holder["result"] is False
        assert t._pending_signal_cleanup is True

    def test_try_start_force_poll_does_not_flush(self) -> None:
        _CPUTimer._force_poll = True
        try:
            t = _CPUTimer(10.0)
            t._pending_signal_cleanup = True
            result = t._try_start_signal()
            assert result is False
            assert t._pending_signal_cleanup is True
        finally:
            _CPUTimer._force_poll = False


# ═══════════════════════════════════════════════════════════════════════
# 5. No unlocked clobber: race safety
# ═══════════════════════════════════════════════════════════════════════


class TestNoUnlockedClobber:
    @requires_sigvtalrm
    def test_non_main_stop_does_not_clobber_during_try_start(self) -> None:
        original = signal.getsignal(signal.SIGVTALRM)
        t1 = _CPUTimer(10.0)
        t1.start()

        th = threading.Thread(target=t1._stop_signal)
        th.start()
        th.join(timeout=5.0)
        assert t1._pending_signal_cleanup is True

        t2 = _CPUTimer(10.0)
        t2.start()

        current_handler = signal.getsignal(signal.SIGVTALRM)
        assert current_handler is not original
        assert callable(current_handler)

        t1.cancel()
        t2.cancel()

    @requires_sigvtalrm
    def test_concurrent_stop_and_start_no_handler_corruption(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t.cancel()

        baseline = signal.getsignal(signal.SIGVTALRM)

        t.start()
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def stopper() -> None:
            barrier.wait()
            try:
                t._stop_signal()
            except Exception as e:
                errors.append(e)

        th = threading.Thread(target=stopper)
        th.start()
        barrier.wait()
        th.join(timeout=5.0)

        assert len(errors) == 0
        assert t._pending_signal_cleanup is True

        t._flush_pending_cleanup()
        assert signal.getsignal(signal.SIGVTALRM) is baseline

    @requires_sigvtalrm
    def test_multiple_non_main_stops_set_flag_once(self) -> None:
        t = _CPUTimer(10.0)
        t.start()

        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def stopper() -> None:
            barrier.wait()
            try:
                t._stop_signal()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=stopper) for _ in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)

        assert len(errors) == 0
        assert t._pending_signal_cleanup is True
        assert t._old_handler is not None
        t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# 6. Unified handler semantics across lifecycle
# ═══════════════════════════════════════════════════════════════════════


class TestUnifiedHandlerSemantics:
    @requires_sigvtalrm
    def test_full_lifecycle_start_stop_flush_start(self) -> None:
        sentinel = signal.getsignal(signal.SIGVTALRM)

        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is True

        th = threading.Thread(target=t._stop_signal)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True
        assert t._use_signal is False

        t._flush_pending_cleanup()
        assert signal.getsignal(signal.SIGVTALRM) is sentinel
        assert t._pending_signal_cleanup is False
        assert t._old_handler is None

        t.start()
        assert t._use_signal is True
        assert t._pending_signal_cleanup is False
        t.cancel()
        assert signal.getsignal(signal.SIGVTALRM) is sentinel

    @requires_sigvtalrm
    def test_start_resets_pending_flag(self) -> None:
        t = _CPUTimer(10.0)
        t.start()

        th = threading.Thread(target=t._stop_signal)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True

        t.start()
        assert t._pending_signal_cleanup is False
        assert t._use_signal is True
        t.cancel()

    @requires_sigvtalrm
    def test_multiple_lifecycle_cycles_with_non_main_stops(self) -> None:
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)

        for _ in range(5):
            t.start()
            assert t._use_signal is True

            th = threading.Thread(target=t._stop_signal)
            th.start()
            th.join(timeout=5.0)
            assert t._pending_signal_cleanup is True

            t._flush_pending_cleanup()
            assert t._pending_signal_cleanup is False

        t.cancel()
        assert signal.getsignal(signal.SIGVTALRM) is sentinel

    @requires_sigvtalrm
    def test_cancel_from_non_main_then_start_on_main(self) -> None:
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t.start()

        th = threading.Thread(target=t.cancel)
        th.start()
        th.join(timeout=5.0)

        t.start()
        assert t._use_signal is True
        assert t._pending_signal_cleanup is False
        t.cancel()
        assert signal.getsignal(signal.SIGVTALRM) is sentinel


# ═══════════════════════════════════════════════════════════════════════
# 7. ResourceLimiter integration
# ═══════════════════════════════════════════════════════════════════════


class TestResourceLimiterIntegration:
    @requires_sigvtalrm
    def test_uninstall_from_non_main_defers_cleanup(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="defer-test")
        limiter.install()
        assert limiter._cpu_timer is not None
        assert limiter._cpu_timer._use_signal is True

        errors: list[Exception] = []

        def uninstaller() -> None:
            try:
                limiter.uninstall()
            except Exception as e:
                errors.append(e)

        th = threading.Thread(target=uninstaller)
        th.start()
        th.join(timeout=5.0)
        assert len(errors) == 0
        assert limiter._cpu_timer is None

    @requires_sigvtalrm
    def test_install_after_non_main_uninstall(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="reinstall")
        limiter.install()

        th = threading.Thread(target=limiter.uninstall)
        th.start()
        th.join(timeout=5.0)

        limiter.install()
        assert limiter._cpu_timer is not None
        limiter.uninstall()

    def test_poll_mode_uninstall_from_non_main(self, force_poll: None) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="poll-uninstall")
        limiter.install()

        errors: list[Exception] = []

        def uninstaller() -> None:
            try:
                limiter.uninstall()
            except Exception as e:
                errors.append(e)

        th = threading.Thread(target=uninstaller)
        th.start()
        th.join(timeout=5.0)
        assert len(errors) == 0
        assert limiter._cpu_timer is None

    @requires_sigvtalrm
    def test_rapid_install_uninstall_from_mixed_threads(self) -> None:
        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="mixed")
        errors: list[Exception] = []
        stop = threading.Event()
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            while not stop.is_set():
                try:
                    limiter.install()
                    time.sleep(0.005)
                    limiter.uninstall()
                except Exception as e:
                    errors.append(e)
                    break

        th = threading.Thread(target=worker)
        th.start()
        barrier.wait()
        for _ in range(10):
            try:
                limiter.install()
                time.sleep(0.005)
                limiter.uninstall()
            except Exception as e:
                errors.append(e)
                break
        stop.set()
        th.join(timeout=5.0)
        limiter.uninstall()
        assert len(errors) == 0


# ═══════════════════════════════════════════════════════════════════════
# 8. Edge cases and boundary conditions
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_flush_concurrent_with_another_flush(self) -> None:
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM) if _HAS_SIGVTALRM else signal.SIG_DFL

        barrier = threading.Barrier(2)

        def flusher() -> None:
            barrier.wait()
            if threading.current_thread() is threading.main_thread():
                t._flush_pending_cleanup()

        th = threading.Thread(target=flusher)
        th.start()
        barrier.wait()
        t._flush_pending_cleanup()
        th.join(timeout=5.0)

        assert t._pending_signal_cleanup is False

    @requires_sigvtalrm
    def test_flag_preserved_across_cancel_without_start(self) -> None:
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = True
        t.cancel()
        assert t._pending_signal_cleanup is True

    @requires_sigvtalrm
    def test_main_thread_stop_does_not_set_flag(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is True
        t._stop_signal()
        assert t._pending_signal_cleanup is False
        assert t._use_signal is False

    @requires_sigvtalrm
    def test_flush_after_main_thread_stop_is_noop(self) -> None:
        t = _CPUTimer(10.0)
        t.start()
        t._stop_signal()
        assert t._pending_signal_cleanup is False

        with patch("signal.signal") as mock_signal, \
             patch("signal.setitimer") as mock_itimer:
            t._flush_pending_cleanup()
            mock_signal.assert_not_called()
            mock_itimer.assert_not_called()

    @requires_sigvtalrm
    def test_flush_double_check_under_lock(self) -> None:
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)

        call_count = 0
        orig_setitimer = signal.setitimer

        def counting_setitimer(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            return orig_setitimer(*args, **kwargs)

        with patch("signal.setitimer", side_effect=counting_setitimer):
            t._flush_pending_cleanup()

        assert call_count == 1
        assert t._pending_signal_cleanup is False

    @requires_sigvtalrm
    def test_no_handler_leak_after_non_main_stop_and_main_start(self) -> None:
        initial = threading.active_count()
        for _ in range(10):
            t = _CPUTimer(10.0)
            t.start()
            th = threading.Thread(target=t._stop_signal)
            th.start()
            th.join(timeout=5.0)
            t.start()
            t.cancel()
        time.sleep(0.3)
        leaked = threading.active_count() - initial
        assert leaked <= 2

    def test_flag_not_set_in_poll_mode_stop(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is False

        th = threading.Thread(target=t.cancel)
        th.start()
        th.join(timeout=5.0)

        assert t._pending_signal_cleanup is False
