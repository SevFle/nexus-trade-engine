"""
Comprehensive tests verifying three critical fixes in _CPUTimer:

1. _stop_signal early-return path properly cleans up itimer and restores handler
   when called from a non-main thread.
2. start() no longer unconditionally spawns a polling thread when signal mode
   succeeds.
3. test_concurrent_try_start_signal_serialized exercises lock contention with
   main-thread participation.
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


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 1: _stop_signal early-return path cleans up itimer + restores handler
# ═══════════════════════════════════════════════════════════════════════════════


class TestStopSignalNonMainThreadCleanup:
    def test_non_main_thread_path_disarms_itimer(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        setitimer_calls: list[tuple] = []

        def fake_setitimer(which: int, interval: float) -> None:
            setitimer_calls.append((which, interval))

        t = _CPUTimer(10.0, plugin_id="test-cleanup")
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ), patch("signal.setitimer", side_effect=fake_setitimer):
            t._stop_signal()
        assert any(c[0] == signal.ITIMER_VIRTUAL and c[1] == 0 for c in setitimer_calls)

    def test_non_main_thread_path_restores_old_handler(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        def sentinel(*a: object) -> None:
            pass

        t = _CPUTimer(10.0, plugin_id="test-restore")
        t._use_signal = True
        t._old_handler = sentinel
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ):
            t._stop_signal()
        assert t._old_handler is None

    def test_non_main_thread_path_nulls_old_handler_on_success(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        original = signal.getsignal(signal.SIGVTALRM)
        t._old_handler = original
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ):
            t._stop_signal()
        assert t._old_handler is None
        assert t._use_signal is False

    def test_non_main_thread_path_preserves_handler_on_restore_failure(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = sentinel
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ), patch("signal.signal", side_effect=OSError("denied")):
            t._stop_signal()
        assert t._old_handler is sentinel

    def test_non_main_thread_path_clears_use_signal(self) -> None:
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

    def test_non_main_thread_cancel_from_real_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        errors: list[Exception] = []

        def run() -> None:
            t = _CPUTimer(10.0, plugin_id="thread-cancel")
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

    def test_non_main_thread_itimer_error_suppressed(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ), patch("signal.setitimer", side_effect=OSError("nope")):
            t._stop_signal()
        assert t._use_signal is False

    def test_non_main_thread_signal_error_suppressed(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ), patch("signal.signal", side_effect=ValueError("bad")):
            t._stop_signal()
        assert t._use_signal is False


class TestStopSignalMainThreadCleanup:
    def test_main_thread_path_disarms_itimer(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        setitimer_calls: list[tuple] = []

        def fake_setitimer(which: int, interval: float) -> None:
            setitimer_calls.append((which, interval))

        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch("signal.setitimer", side_effect=fake_setitimer):
            t._stop_signal()
        assert any(c[0] == signal.ITIMER_VIRTUAL and c[1] == 0 for c in setitimer_calls)

    def test_main_thread_path_nulls_old_handler_on_success(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        t._stop_signal()
        assert t._old_handler is None
        assert t._use_signal is False

    def test_main_thread_path_preserves_handler_on_failure(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = sentinel
        with patch("signal.signal", side_effect=OSError("nope")):
            t._stop_signal()
        assert t._old_handler is sentinel
        assert t._use_signal is False

    def test_main_thread_uses_signal_lock(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(10.0)
        t._use_signal = True
        t._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter._signal_lock",
            wraps=_signal_lock,
        ) as mock_lock:
            t._stop_signal()
        mock_lock.__enter__.assert_called()
        mock_lock.__exit__.assert_called()

    def test_both_paths_identical_cleanup(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t1 = _CPUTimer(10.0)
        t1._use_signal = True
        t1._old_handler = signal.getsignal(signal.SIGVTALRM)
        t1._stop_signal()
        assert t1._use_signal is False
        assert t1._old_handler is None

        t2 = _CPUTimer(10.0)
        t2._use_signal = True
        t2._old_handler = signal.getsignal(signal.SIGVTALRM)
        with patch(
            "engine.plugins.sandbox.layers.resource_limiter.threading.current_thread",
            return_value=threading.Thread(),
        ):
            t2._stop_signal()
        assert t2._use_signal is False
        assert t2._old_handler is None


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 2: start() does not spawn poll thread when signal mode succeeds
# ═══════════════════════════════════════════════════════════════════════════════


class TestStartConditionalPollThread:
    def test_signal_mode_no_poll_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        try:
            assert t._use_signal is True
            assert t._thread is None
        finally:
            t.cancel()

    def test_poll_mode_creates_poll_thread(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        try:
            assert t._use_signal is False
            assert t._thread is not None
            assert t._thread.is_alive()
        finally:
            t.cancel()

    def test_signal_failure_falls_back_to_poll_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        with patch("signal.signal", side_effect=ValueError("nope")):
            t.start()
        try:
            assert t._use_signal is False
            assert t._thread is not None
            assert t._thread.is_alive()
        finally:
            t.cancel()

    def test_setitimer_failure_falls_back_to_poll_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        with patch("signal.setitimer", side_effect=OSError("itimer fail")):
            t.start()
        try:
            assert t._use_signal is False
            assert t._thread is not None
        finally:
            t.cancel()

    def test_force_poll_creates_poll_thread(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        t.start()
        try:
            assert t._use_signal is False
            assert t._thread is not None
        finally:
            t.cancel()

    def test_no_thread_leak_after_multiple_signal_starts(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        for _ in range(10):
            t = _CPUTimer(60.0)
            t.start()
            assert t._use_signal is True
            assert t._thread is None
            t.cancel()
            assert t._use_signal is False

    def test_cancel_in_signal_mode_no_thread_to_join(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        t.start()
        assert t._thread is None
        start = time.monotonic()
        t.cancel()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5
        assert t._thread is None
        assert t._use_signal is False

    def test_signal_mode_handler_installed_and_restored(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(60.0)
        t.start()
        current = signal.getsignal(signal.SIGVTALRM)
        assert current is not sentinel
        assert callable(current)
        t.cancel()
        assert signal.getsignal(signal.SIGVTALRM) is sentinel

    def test_restart_from_signal_to_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        for _ in range(5):
            t.start()
            assert t._use_signal is True
            assert t._thread is None
            t.cancel()
            assert t._use_signal is False

    def test_mode_property_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0)
        assert t.mode == "poll"
        t.start()
        try:
            assert t.mode == "signal"
        finally:
            t.cancel()
        assert t.mode == "poll"

    def test_mode_property_poll(self, force_poll: None) -> None:
        t = _CPUTimer(60.0)
        assert t.mode == "poll"
        t.start()
        try:
            assert t.mode == "poll"
        finally:
            t.cancel()


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 3: Concurrent try_start_signal with main-thread lock contention
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentTryStartSignalWithMainThread:
    def test_main_thread_wins_over_background_thread(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        results: dict[str, bool] = {}
        main_go = threading.Event()
        thread_done = threading.Event()

        def try_from_bg() -> None:
            main_go.wait(timeout=5.0)
            t = _CPUTimer(10.0)
            results["bg"] = t._try_start_signal()
            if results["bg"]:
                t._stop_signal()
            thread_done.set()

        th = threading.Thread(target=try_from_bg)
        th.start()
        t = _CPUTimer(10.0)
        results["main"] = t._try_start_signal()
        main_go.set()
        thread_done.wait(timeout=5.0)
        th.join(timeout=5.0)
        if results["main"]:
            t._stop_signal()
        assert results["main"] is True
        assert results["bg"] is False

    def test_main_thread_serialized_with_itself(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t1 = _CPUTimer(10.0)
        r1 = t1._try_start_signal()
        assert r1 is True
        t1._stop_signal()
        t2 = _CPUTimer(10.0)
        r2 = t2._try_start_signal()
        assert r2 is True
        t2._stop_signal()

    def test_lock_contention_with_main_thread_participation(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def bg_try() -> None:
            barrier.wait()
            t = _CPUTimer(10.0)
            results.append(t._try_start_signal())
            if results[-1]:
                t._stop_signal()

        th = threading.Thread(target=bg_try)
        th.start()
        t = _CPUTimer(10.0)
        results.append(t._try_start_signal())
        barrier.wait()
        if results[-1]:
            t._stop_signal()
        th.join(timeout=5.0)
        at_most_one = sum(1 for r in results if r) <= 1
        assert at_most_one

    def test_concurrent_start_cancel_with_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        errors: list[Exception] = []
        stop = threading.Event()

        def racer() -> None:
            while not stop.is_set():
                t = _CPUTimer(10.0)
                try:
                    t.start()
                    t.cancel()
                except Exception as e:
                    errors.append(e)

        th = threading.Thread(target=racer)
        th.start()
        for _ in range(20):
            t = _CPUTimer(10.0)
            t.start()
            t.cancel()
        stop.set()
        th.join(timeout=5.0)
        assert len(errors) == 0

    def test_non_main_thread_never_gets_signal(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        results: list[bool] = []
        barrier = threading.Barrier(3)

        def try_signal() -> None:
            barrier.wait()
            t = _CPUTimer(10.0)
            results.append(t._try_start_signal())

        threads = [threading.Thread(target=try_signal) for _ in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        assert all(r is False for r in results)

    def test_main_thread_signal_blocks_concurrent_bg_attempts(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t_main = _CPUTimer(10.0)
        assert t_main._try_start_signal() is True
        bg_result: dict[str, bool] = {}
        done = threading.Event()

        def bg_try() -> None:
            t = _CPUTimer(10.0)
            bg_result["ok"] = t._try_start_signal()
            done.set()

        th = threading.Thread(target=bg_try)
        th.start()
        done.wait(timeout=5.0)
        th.join(timeout=5.0)
        t_main._stop_signal()
        assert bg_result.get("ok") is False


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION: End-to-end lifecycle with all three fixes
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEndLifecycle:
    def test_full_lifecycle_signal_mode(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        t = _CPUTimer(0.1, plugin_id="e2e-signal")
        t.start()
        assert t._use_signal is True
        assert t._thread is None
        current = signal.getsignal(signal.SIGVTALRM)
        assert current is not sentinel
        assert callable(current)
        assert not t.expired
        t.cancel()
        assert t._use_signal is False
        assert t._thread is None
        assert signal.getsignal(signal.SIGVTALRM) is sentinel

    def test_full_lifecycle_poll_mode(self, force_poll: None) -> None:
        t = _CPUTimer(0.1, plugin_id="e2e-poll")
        t.start()
        assert t._use_signal is False
        assert t._thread is not None
        thread = t._thread
        t.cancel()
        assert t._thread is None
        assert not thread.is_alive()

    def test_signal_expiry_triggers_correctly(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.05, plugin_id="e2e-expire")
        t.start()
        assert t._use_signal is True
        assert t._thread is None
        _burn_cpu(0.4)
        assert t.expired
        t.cancel()
        assert t._use_signal is False

    def test_cancel_from_bg_thread_after_signal_start(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(60.0, plugin_id="bg-cancel")
        t.start()
        assert t._use_signal is True
        errors: list[Exception] = []

        def cancel_from_bg() -> None:
            try:
                t.cancel()
            except Exception as e:
                errors.append(e)

        th = threading.Thread(target=cancel_from_bg)
        th.start()
        th.join(timeout=5.0)
        assert len(errors) == 0
        assert t._use_signal is False

    def test_context_manager_signal_mode(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        with _CPUTimer(60.0, plugin_id="ctx-sig") as t:
            assert t._use_signal is True
            assert t._thread is None
        assert t._use_signal is False
        assert signal.getsignal(signal.SIGVTALRM) is sentinel

    def test_rapid_signal_start_cancel_cycles(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        sentinel = signal.getsignal(signal.SIGVTALRM)
        for _ in range(20):
            t = _CPUTimer(60.0)
            t.start()
            assert t._use_signal is True
            assert t._thread is None
            t.cancel()
            assert t._use_signal is False
        assert signal.getsignal(signal.SIGVTALRM) is sentinel

    def test_signal_mode_expires_and_check_raises(self) -> None:
        if not _HAS_SIGVTALRM:
            pytest.skip("SIGVTALRM not available")
        t = _CPUTimer(0.05, plugin_id="expire-check")
        t.start()
        _burn_cpu(0.4)
        assert t.expired
        from engine.plugins.sandbox.core.violation import ResourceExhausted

        with pytest.raises(ResourceExhausted):
            t.check()
        t.cancel()
