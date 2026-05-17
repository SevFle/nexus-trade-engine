"""
Comprehensive tests for the deferred signal cleanup mechanism in _CPUTimer.

Covers:
1. _pending_signal_cleanup flag initialization
2. Non-main-thread _stop_signal sets _pending_signal_cleanup under lock
3. _try_deferred_cleanup() performs actual restoration on main thread
4. _try_deferred_cleanup() called at start of _try_start_signal
5. _old_handler=None only on successful cleanup (unified both paths)
6. Full start/stop/restart cycles with deferred cleanup
7. Edge cases: no handler, restore failure, concurrent access, double cleanup
"""

from __future__ import annotations

import signal
import threading
import time
from unittest.mock import patch

import pytest

from engine.plugins.sandbox.layers.resource_limiter import (
    _HAS_SIGVTALRM,
    _CPUTimer,
)


@pytest.fixture
def force_poll():
    _CPUTimer._force_poll = True
    yield
    _CPUTimer._force_poll = False


# ═══════════════════════════════════════════════════════════════════════
# 1. _pending_signal_cleanup initialization
# ═══════════════════════════════════════════════════════════════════════


class TestPendingSignalCleanupInit:
    def test_flag_false_on_new_instance(self) -> None:
        t = _CPUTimer(10.0)
        assert t._pending_signal_cleanup is False

    def test_flag_false_after_start(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._pending_signal_cleanup is False
        finally:
            t.cancel()

    def test_flag_false_after_start_force_poll(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        try:
            assert t._pending_signal_cleanup is False
        finally:
            t.cancel()

    def test_flag_reset_on_restart(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = True
        t.start()
        try:
            assert t._pending_signal_cleanup is False
        finally:
            t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# 2. Non-main-thread _stop_signal sets _pending_signal_cleanup
# ═══════════════════════════════════════════════════════════════════════


class TestStopSignalNonMainThreadDefers:
    def test_stop_from_non_main_sets_pending_flag(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        result: dict[str, bool] = {}

        def run() -> None:
            t = _CPUTimer(10.0)
            t._use_signal = True
            t._old_handler = signal.getsignal(signal.SIGVTALRM)
            t._stop_signal()
            result["pending"] = t._pending_signal_cleanup
            result["use_signal"] = t._use_signal

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result.get("pending") is True
        assert result.get("use_signal") is False

    def test_stop_from_non_main_preserves_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        result: dict[str, object] = {}

        def run() -> None:
            t = _CPUTimer(10.0)
            t._use_signal = True
            sentinel = signal.getsignal(signal.SIGVTALRM)
            t._old_handler = sentinel
            t._stop_signal()
            result["old_handler"] = t._old_handler
            result["sentinel"] = sentinel

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result["old_handler"] is result["sentinel"]

    def test_stop_from_non_main_does_not_call_signal_directly(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        signal_calls: list[tuple[str, ...]] = []

        original_signal = signal.signal
        original_setitimer = signal.setitimer

        def tracking_signal(*args: object) -> object:
            signal_calls.append(("signal", *tuple(str(a) for a in args)))
            return original_signal(*args)

        def tracking_setitimer(*args: object) -> object:
            signal_calls.append(("setitimer", *tuple(str(a) for a in args)))
            return original_setitimer(*args)

        def run() -> None:
            t = _CPUTimer(10.0)
            t._use_signal = True
            t._old_handler = signal.getsignal(signal.SIGVTALRM)
            with patch("signal.signal", side_effect=tracking_signal), \
                 patch("signal.setitimer", side_effect=tracking_setitimer):
                t._stop_signal()

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert len(signal_calls) == 0

    def test_stop_from_non_main_logs_warning(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0, plugin_id="warn-test")
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ):
            t._stop_signal()
        assert t._pending_signal_cleanup is True
        assert t._use_signal is False

    def test_stop_from_non_main_acquires_signal_lock(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        result: dict[str, bool] = {}

        def run() -> None:
            t._stop_signal()
            result["pending"] = t._pending_signal_cleanup

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result.get("pending") is True


# ═══════════════════════════════════════════════════════════════════════
# 3. _try_deferred_cleanup performs restoration on main thread
# ═══════════════════════════════════════════════════════════════════════


class TestTryDeferredCleanup:
    def test_noop_when_no_pending_cleanup(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = False
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False

    def test_performs_cleanup_when_pending_on_main_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False
        assert t._old_handler is None
        assert signal.getsignal(signal.SIGVTALRM) is sentinel

    def test_skips_cleanup_from_non_main_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        result: dict[str, bool] = {}

        def run() -> None:
            t = _CPUTimer(10.0)
            t._old_handler = signal.getsignal(signal.SIGVTALRM)
            t._pending_signal_cleanup = True
            t._try_deferred_cleanup()
            result["still_pending"] = t._pending_signal_cleanup
            result["handler_intact"] = t._old_handler is not None

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result.get("still_pending") is True
        assert result.get("handler_intact") is True

    def test_clears_pending_flag_on_success(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._pending_signal_cleanup = True
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False

    def test_clears_old_handler_on_success(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._pending_signal_cleanup = True
        t._try_deferred_cleanup()
        assert t._old_handler is None

    def test_calls_setitimer_zero(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._pending_signal_cleanup = True
        setitimer_args: list[tuple[object, ...]] = []
        original_setitimer = signal.setitimer

        def tracking_setitimer(*args: object) -> object:
            setitimer_args.append(args)
            return original_setitimer(*args)

        with patch("signal.setitimer", side_effect=tracking_setitimer):
            t._try_deferred_cleanup()
        assert len(setitimer_args) == 1
        assert setitimer_args[0] == (signal.ITIMER_VIRTUAL, 0)

    def test_restores_old_handler_via_signal_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        signal_args: list[tuple[object, ...]] = []
        original_signal_fn = signal.signal

        def tracking_signal(*args: object) -> object:
            signal_args.append(args)
            return original_signal_fn(*args)

        with patch("signal.signal", side_effect=tracking_signal):
            t._try_deferred_cleanup()
        assert any(
            a[0] is signal.SIGVTALRM and a[1] is sentinel
            for a in signal_args
        )

    def test_handles_old_handler_none_gracefully(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = None
        t._pending_signal_cleanup = True
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False

    def test_handles_setitimer_error(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._pending_signal_cleanup = True
        with patch("signal.setitimer", side_effect=OSError("boom")):
            t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False

    def test_handles_signal_restore_error_preserves_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        with patch("signal.signal", side_effect=OSError("denied")):
            t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False
        assert t._old_handler is sentinel

    def test_double_deferred_cleanup_idempotent(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False
        assert t._old_handler is None

    def test_concurrent_deferred_cleanup_from_bg_threads_skipped(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def try_cleanup() -> None:
            barrier.wait()
            t._try_deferred_cleanup()
            results.append(t._pending_signal_cleanup)

        threads = [threading.Thread(target=try_cleanup) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        assert all(r is True for r in results)
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False


# ═══════════════════════════════════════════════════════════════════════
# 4. _try_start_signal calls _try_deferred_cleanup at start
# ═══════════════════════════════════════════════════════════════════════


class TestTryStartSignalCallsDeferredCleanup:
    def test_try_start_signal_triggers_deferred_cleanup(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        result = t._try_start_signal()
        assert t._pending_signal_cleanup is False
        if result:
            t._stop_signal()

    def test_deferred_cleanup_runs_before_new_signal_install(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        cleanup_order: list[str] = []
        original_cleanup = t._try_deferred_cleanup

        def tracking_cleanup() -> None:
            cleanup_order.append("cleanup")
            original_cleanup()

        t._try_deferred_cleanup = tracking_cleanup
        t._force_poll = False
        result = t._try_start_signal()
        if result:
            assert "cleanup" in cleanup_order
            t._stop_signal()

    def test_try_start_signal_false_on_non_main_still_runs_cleanup(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        cleanup_ran: dict[str, bool] = {"ran": False}

        def run() -> None:
            t = _CPUTimer(10.0)
            t._old_handler = signal.getsignal(signal.SIGVTALRM)
            t._pending_signal_cleanup = True
            original_cleanup = t._try_deferred_cleanup

            def tracking_cleanup() -> None:
                cleanup_ran["ran"] = True
                original_cleanup()

            t._try_deferred_cleanup = tracking_cleanup
            result = t._try_start_signal()
            assert result is False
            assert t._pending_signal_cleanup is True
            cleanup_ran["checked"] = True

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert cleanup_ran.get("checked") is True


# ═══════════════════════════════════════════════════════════════════════
# 5. _old_handler=None only on successful cleanup (both paths)
# ═══════════════════════════════════════════════════════════════════════


class TestOldHandlerNullOnlyOnSuccess:
    def test_main_thread_success_clears_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._stop_signal()
        assert t._old_handler is None

    def test_main_thread_failure_preserves_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._use_signal = True
        t._old_handler = sentinel
        with patch("signal.signal", side_effect=OSError("fail")):
            t._stop_signal()
        assert t._old_handler is sentinel

    def test_non_main_thread_preserves_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        result: dict[str, object] = {}

        def run() -> None:
            t = _CPUTimer(10.0)
            sentinel = signal.getsignal(signal.SIGVTALRM)
            t._use_signal = True
            t._old_handler = sentinel
            t._stop_signal()
            result["old_handler"] = t._old_handler
            result["sentinel"] = sentinel

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result["old_handler"] is result["sentinel"]

    def test_deferred_cleanup_success_clears_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        t._try_deferred_cleanup()
        assert t._old_handler is None

    def test_deferred_cleanup_failure_preserves_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        with patch("signal.signal", side_effect=OSError("denied")):
            t._try_deferred_cleanup()
        assert t._old_handler is sentinel

    def test_start_resets_old_handler_to_none(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t.start()
        try:
            if t._use_signal:
                assert t._old_handler is not None
            else:
                assert t._old_handler is None
        finally:
            t.cancel()


# ═══════════════════════════════════════════════════════════════════════
# 6. Full start/stop/restart cycles with deferred cleanup
# ═══════════════════════════════════════════════════════════════════════


class TestFullDeferredCycles:
    def test_stop_from_bg_thread_then_start_on_main_deferred(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        assert t._use_signal is True

        def stop_from_bg() -> None:
            t._stop_signal()

        th = threading.Thread(target=stop_from_bg)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True
        assert t._use_signal is False

        t.start()
        assert t._pending_signal_cleanup is False
        assert t._use_signal is True
        t.cancel()

    def test_deferred_cleanup_restores_handler_before_new_install(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        assert t._use_signal is True

        def stop_from_bg() -> None:
            t._stop_signal()

        th = threading.Thread(target=stop_from_bg)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True

        t._use_signal = False
        t._old_handler = None
        t._pending_signal_cleanup = False
        t.cancel()

    def test_multiple_stop_from_bg_accumulates_single_pending(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        assert t._use_signal is True

        def stop_from_bg() -> None:
            t._stop_signal()

        for _ in range(3):
            th = threading.Thread(target=stop_from_bg)
            th.start()
            th.join(timeout=5.0)

        assert t._pending_signal_cleanup is True
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False
        t.cancel()

    def test_cancel_after_deferred_stop_cleans_up(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        assert t._use_signal is True

        def stop_from_bg() -> None:
            t._stop_signal()

        th = threading.Thread(target=stop_from_bg)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True

        t.cancel()
        assert t._thread is None

    def test_start_clears_pending_flag(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t._pending_signal_cleanup = True
        t.start()
        assert t._pending_signal_cleanup is False
        t.cancel()

    def test_lifecycle_start_cancel_start_preserves_signals(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        original = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is True
        t.cancel()
        assert t._use_signal is False
        assert t._old_handler is None
        t.start()
        assert t._use_signal is True
        t.cancel()
        assert t._use_signal is False
        restored = signal.getsignal(signal.SIGVTALRM)
        assert restored is original


# ═══════════════════════════════════════════════════════════════════════
# 7. Edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestDeferredCleanupEdgeCases:
    def test_try_deferred_cleanup_with_setitimer_valueerror(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._pending_signal_cleanup = True
        with patch("signal.setitimer", side_effect=ValueError("bad")):
            t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False

    def test_try_deferred_cleanup_with_signal_valueerror(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = sentinel
        t._pending_signal_cleanup = True
        with patch("signal.signal", side_effect=ValueError("bad")):
            t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False
        assert t._old_handler is sentinel

    def test_deferred_cleanup_noop_when_already_cleaned(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._pending_signal_cleanup = False
        setitimer_calls: list[tuple[object, ...]] = []
        original_setitimer = signal.setitimer

        def tracking_setitimer(*args: object) -> object:
            setitimer_calls.append(args)
            return original_setitimer(*args)

        with patch("signal.setitimer", side_effect=tracking_setitimer):
            t._try_deferred_cleanup()
        assert len(setitimer_calls) == 0

    def test_race_pending_cleared_between_check_and_lock(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._pending_signal_cleanup = True
        barrier = threading.Barrier(2)
        results: list[bool] = []

        def compete() -> None:
            barrier.wait()
            t._try_deferred_cleanup()
            results.append(t._pending_signal_cleanup)

        th = threading.Thread(target=compete)
        th.start()
        barrier.wait()
        t._try_deferred_cleanup()
        results.append(t._pending_signal_cleanup)
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is False

    def test_force_poll_defers_no_cleanup_since_signal_never_started(self, force_poll: None) -> None:
        t = _CPUTimer(10.0)
        t.start()
        assert t._use_signal is False
        assert t._pending_signal_cleanup is False

        def stop_from_bg() -> None:
            t._stop_signal()

        th = threading.Thread(target=stop_from_bg)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is False
        t.cancel()

    def test_stop_signal_main_thread_no_pending_flag(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._stop_signal()
        assert t._pending_signal_cleanup is False
        assert t._use_signal is False
        assert t._old_handler is None

    def test_concurrent_stop_and_deferred_cleanup(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        errors: list[Exception] = []
        t = _CPUTimer(60.0)
        t.start()
        assert t._use_signal is True
        barrier = threading.Barrier(2)

        def stopper() -> None:
            try:
                barrier.wait()
                t._stop_signal()
            except Exception as e:
                errors.append(e)

        def cleaner() -> None:
            try:
                barrier.wait()
                t._try_deferred_cleanup()
            except Exception as e:
                errors.append(e)

        th1 = threading.Thread(target=stopper)
        th2 = threading.Thread(target=cleaner)
        th1.start()
        th2.start()
        th1.join(timeout=5.0)
        th2.join(timeout=5.0)
        assert len(errors) == 0
        t.cancel()

    def test_deferred_cleanup_with_signal_lock_contention(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._pending_signal_cleanup = True
        t._try_deferred_cleanup()
        assert t._pending_signal_cleanup is False
        assert t._old_handler is None


# ═══════════════════════════════════════════════════════════════════════
# 8. Integration: ResourceLimiter with deferred cleanup
# ═══════════════════════════════════════════════════════════════════════


class TestDeferredCleanupIntegration:
    def test_resource_limiter_uninstall_from_bg_sets_pending(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        from engine.plugins.sandbox.core.policy import ResourcePolicy

        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter

        limiter = ResourceLimiter(policy, plugin_id="deferred-int")
        limiter.install()
        cpu_timer = limiter._cpu_timer
        assert cpu_timer is not None

        result: dict[str, bool] = {}

        def run() -> None:
            cpu_timer._stop_signal()
            result["pending"] = cpu_timer._pending_signal_cleanup

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert result.get("pending") is True
        limiter.uninstall()

    def test_resource_limiter_reinstall_clears_pending(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        from engine.plugins.sandbox.core.policy import ResourcePolicy

        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter

        limiter = ResourceLimiter(policy, plugin_id="reinstall-int")
        limiter.install()
        limiter.uninstall()
        limiter.install()
        cpu_timer = limiter._cpu_timer
        assert cpu_timer is not None
        assert cpu_timer._pending_signal_cleanup is False
        limiter.uninstall()

    def test_rapid_install_uninstall_no_signal_leak(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        from engine.plugins.sandbox.core.policy import ResourcePolicy

        policy = ResourcePolicy(max_cpu_seconds=60.0, wall_time_seconds=60.0)
        from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter

        original = signal.getsignal(signal.SIGVTALRM)
        for _ in range(20):
            limiter = ResourceLimiter(policy, plugin_id="leak-test")
            limiter.install()
            limiter.uninstall()
        restored = signal.getsignal(signal.SIGVTALRM)
        assert restored is original


# ═══════════════════════════════════════════════════════════════════════
# 9. Stress: concurrent deferred cleanup scenarios
# ═══════════════════════════════════════════════════════════════════════


class TestDeferredCleanupStress:
    def test_many_timers_concurrent_stop_and_cleanup(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        timers = []
        errors: list[Exception] = []
        for _ in range(10):
            t = _CPUTimer(60.0)
            t.start()
            timers.append(t)

        barrier = threading.Barrier(len(timers))

        def stop_timer(timer: _CPUTimer) -> None:
            try:
                barrier.wait()
                timer._stop_signal()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=stop_timer, args=(t,)) for t in timers]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        assert len(errors) == 0
        for t in timers:
            t.cancel()

    def test_interleaved_start_stop_deferred_cleanup(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        errors: list[Exception] = []
        stop = threading.Event()

        def bg_stopper() -> None:
            while not stop.is_set():
                try:
                    t._stop_signal()
                except Exception as e:
                    errors.append(e)
                time.sleep(0.01)

        th = threading.Thread(target=bg_stopper)
        th.start()
        for _ in range(20):
            t.start()
            time.sleep(0.01)
            t.cancel()
        stop.set()
        th.join(timeout=5.0)
        assert all(not isinstance(e, (SystemExit, KeyboardInterrupt)) for e in errors)
        t.cancel()

    def test_no_deadlock_with_pending_cleanup_and_start(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        errors: list[Exception] = []

        def run() -> None:
            try:
                t._stop_signal()
            except Exception as e:
                errors.append(e)

        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=5.0)
        assert t._pending_signal_cleanup is True

        done = threading.Event()

        def restart() -> None:
            try:
                t.cancel()
                t.start()
            except Exception as e:
                errors.append(e)
            done.set()

        th2 = threading.Thread(target=restart)
        th2.start()
        assert done.wait(timeout=5.0)
        assert t._pending_signal_cleanup is False
        assert len(errors) == 0
        t.cancel()
