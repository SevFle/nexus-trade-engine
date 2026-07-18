"""Tests for the Layer-3 :mod:`engine.plugins.sandbox.resource_limits` module.

Covers:

1. The public API surface (``SandboxResourceError``, ``ResourceLimits``,
   ``resource_limits``) and the resource-kind metadata carried on the
   exception.
2. CPU guard — ``SIGALRM``-based preemption of a tight compute loop.
   :class:`SandboxResourceError` derives from :class:`RuntimeError` (a
   regular :class:`Exception` subclass); defeat-resistance for sandboxed
   strategies comes from the sandbox runtime blocking introspection, not
   from the exception hierarchy.
3. Memory guard — :mod:`tracemalloc`-based Python-allocation soft-cap.
4. The single-flight module-level :class:`threading.Lock` — re-entrant
   (same-thread) and concurrent (cross-thread) entry must be rejected with a
   clear :class:`SandboxResourceError` (``kind="single_flight"``) rather
   than deadlocking or silently corrupting the process-global SIGALRM
   handler / tracemalloc peak counter.
5. Graceful degradation — disabled guards, missing ``signal``/``tracemalloc``
   support, and resource-limit values ``<= 0``.
6. Teardown correctness — the single-flight lock is released even when the
   body raises :class:`SandboxResourceError`, and the prior SIGALRM handler
   is restored.
"""

from __future__ import annotations

import signal
import threading
from typing import Any

import pytest

from engine.plugins.sandbox.resource_limits import (
    CPU_RESOURCE,
    MEMORY_RESOURCE,
    SINGLE_FLIGHT_RESOURCE,
    ResourceLimits,
    SandboxResourceError,
    _can_use_signals,
    _guard_lock,
    resource_limits,
)

#: Skip marker for tests that require the ``SIGALRM``/``setitimer`` CPU guard
#: (POSIX main thread only).  Defined once so the skip reason stays uniform.
requires_sigalrm: pytest.MarkDecorator = pytest.mark.skipif(
    not _can_use_signals(),
    reason="SIGALRM-based CPU guard requires POSIX main thread",
)

# ── Helpers ──────────────────────────────────────────────────────────

#: Collected to keep references alive across tracemalloc peak measurement
#: (otherwise peak would drop when the temporary list is GC'd before exit).
_ALLOCATED: list[Any] = []


def _drain_allocated() -> list[Any]:
    """Drop the module-level pin so subsequent tests start with a clean peak."""
    items = list(_ALLOCATED)
    _ALLOCATED.clear()
    return items


# ── 1. Public API surface ────────────────────────────────────────────


class TestPublicAPISurface:
    def test_sandbox_resource_error_inherits_from_runtime_error(self) -> None:
        """The resource-violation signal MUST derive from :class:`RuntimeError`
        (and therefore :class:`Exception`) so host-side code that does a
        blanket ``except Exception`` — task workers, observability
        middleware, asyncio error handlers — sees and logs it correctly
        instead of letting it tear the worker down as an unhandled
        :class:`BaseException` would.

        This is the single most important invariant of the Layer-3 guards:
        defeat-resistance for sandboxed strategies comes from the sandbox
        runtime layer (the restricted importer denies ``engine.*``, frame
        filters strip host frames from raised exceptions, and the host
        sandbox re-asserts the ``SIGALRM`` guard on the next bytecode
        boundary), **not** from the exception hierarchy.  Keeping the
        exception inside the ordinary :class:`Exception` hierarchy is the
        observability win that motivated the choice; this test pins it down
        so a future contributor who re-introduces the hierarchy-based
        defence cannot silently change observable behaviour."""
        # SandboxResourceError must be reachable via RuntimeError (so a
        # blanket ``except Exception`` in host code catches it) and via
        # Exception (the standard catch-all).
        assert issubclass(SandboxResourceError, RuntimeError)
        assert issubclass(SandboxResourceError, Exception)
        # And therefore it is, transitively, a BaseException too — but the
        # decisive relationship is the RuntimeError one above.
        assert issubclass(SandboxResourceError, BaseException)

    def test_sandbox_resource_error_catchable_by_except_exception(self) -> None:
        """A directly-raised :class:`SandboxResourceError` is caught by a
        blanket ``except Exception`` clause.  This is the host-side
        contract: observability / worker code can wrap a guarded call in
        ``try: ... except Exception: log(...)`` and still see the resource
        violation surface, rather than having to special-case
        :class:`BaseException` (which would risk swallowing
        :class:`KeyboardInterrupt` / :class:`SystemExit`)."""
        caught: Exception | None = None

        def _raise_violation() -> None:
            raise SandboxResourceError(CPU_RESOURCE, limit=2.5)

        try:
            _raise_violation()
        except Exception as exc:
            # to assert the host-side contract documented above.
            caught = exc
        assert caught is not None
        assert isinstance(caught, SandboxResourceError)
        assert isinstance(caught, RuntimeError)
        assert caught.kind == CPU_RESOURCE
        assert caught.limit == 2.5

    def test_sandbox_resource_error_mro_includes_runtime_error(self) -> None:
        """``SandboxResourceError`` slots directly under :class:`RuntimeError`
        (sibling with other :class:`RuntimeError` subclasses like
        :class:`RecursionError`), not buried deep in a custom hierarchy.
        A shallow MRO keeps the catchable-by-``except Exception`` contract
        obvious and stable."""
        bases = SandboxResourceError.__bases__
        assert RuntimeError in bases
        # And it must NOT be slotted directly under BaseException (which
        # would put it alongside SystemExit / KeyboardInterrupt and break
        # the ``except Exception`` contract).
        assert BaseException not in bases

    def test_cpu_resource_constants(self) -> None:
        assert CPU_RESOURCE == "cpu"
        assert MEMORY_RESOURCE == "memory"
        assert SINGLE_FLIGHT_RESOURCE == "single_flight"

    def test_cpu_error_message(self) -> None:
        err = SandboxResourceError(CPU_RESOURCE, limit=2.5)
        assert err.kind == CPU_RESOURCE
        assert err.limit == 2.5
        assert err.actual is None
        assert "CPU time limit" in str(err)
        assert "2.5s" in str(err)
        assert "SIGALRM" in str(err)

    def test_memory_error_message(self) -> None:
        err = SandboxResourceError(MEMORY_RESOURCE, limit=512, actual=600.25)
        assert err.kind == MEMORY_RESOURCE
        assert err.limit == 512
        assert err.actual == 600.25
        assert "memory limit" in str(err)
        assert "512MiB" in str(err)
        assert "600.25MiB" in str(err)

    def test_unknown_kind_message(self) -> None:
        err = SandboxResourceError("fds", limit=1)
        assert "fds resource limit" in str(err)

    def test_single_flight_error_message(self) -> None:
        """The single-flight violation carries the explanatory message and
        the structured ``kind`` metadata so callers catching
        :class:`SandboxResourceError` can branch on it just like the CPU /
        memory guards."""
        err = SandboxResourceError(SINGLE_FLIGHT_RESOURCE, limit=None)
        assert err.kind == SINGLE_FLIGHT_RESOURCE
        assert err.limit is None
        assert err.actual is None
        message = str(err)
        # The message names the offending API and the cause.
        assert "resource_limits" in message
        assert "re-entrant" in message
        # And the process-global resource that motivates the lock.
        assert "SIGALRM" in message
        assert "tracemalloc" in message

    def test_resource_limits_defaults(self) -> None:
        limits = ResourceLimits()
        assert limits.cpu_timeout_seconds == 30.0
        assert limits.max_memory_mb == 512

    def test_resource_limits_is_frozen(self) -> None:
        limits = ResourceLimits()
        with pytest.raises((AttributeError, Exception)):
            limits.cpu_timeout_seconds = 99  # type: ignore[misc]


# ── 2. CPU guard ─────────────────────────────────────────────────────


def _hot_compute_loop() -> int:
    """Tight CPU loop with no ``await`` — the asyncio timeout cannot preempt
    this, only the ``SIGALRM`` guard can."""
    i = 0
    while True:
        i += 1


@pytest.fixture(autouse=True)
def _reset_guard_lock_between_tests() -> Any:
    """Snapshot/restore the single-flight :data:`_guard_lock` so a buggy test
    that fails to release it cannot poison every subsequent test in the
    session.

    We assert the lock is unheld on entry (catches lock-leak regressions
    from prior tests) and then yield.  On teardown we only *assert* — we do
    not forcibly release, because forcibly releasing a lock held by a
    different thread is undefined behaviour.  If a test leaves the lock
    held it must be fixed at source.
    """
    assert not _guard_lock.locked(), (
        "single-flight _guard_lock is held at test entry — a previous test "
        "leaked it; resource_limits() will now reject every call."
    )
    yield
    assert not _guard_lock.locked(), (
        "single-flight _guard_lock is still held after test exited — "
        "this test leaked it and must be fixed."
    )


class TestCpuGuard:
    """CPU timeout via ``SIGALRM`` / ``setitimer``."""

    def test_can_use_signals_on_main_thread(self) -> None:
        """Sanity-check the test harness itself runs on the POSIX main
        thread — the remaining CPU-guard tests depend on this."""
        # pytest's default runner executes tests on the main thread; we
        # assert that the helper agrees so the skip fixture behaves.
        assert threading.current_thread() is threading.main_thread()
        assert hasattr(signal, "SIGALRM")
        assert hasattr(signal, "setitimer")

    @requires_sigalrm
    def test_cpu_timeout_kills_tight_loop(self) -> None:
        """A tight compute loop with no ``await`` (which the asyncio timeout
        cannot preempt) is killed by the ``SIGALRM`` guard."""
        limits = ResourceLimits(cpu_timeout_seconds=0.1, max_memory_mb=0)
        with pytest.raises(SandboxResourceError, match="CPU"), resource_limits(limits):
            _hot_compute_loop()

    @requires_sigalrm
    def test_cpu_violation_swallowable_inside_guarded_body(self) -> None:
        """**Documents the Layer-3 design trade-off (replaces the old
        hierarchy-based defeat-resistance test).**

        Because :class:`SandboxResourceError` now derives from
        :class:`RuntimeError` (a regular :class:`Exception` subclass), a
        hostile ``except Exception`` clause *can* swallow a
        :class:`SandboxResourceError` raised inside the guarded region.
        This is the deliberate design choice documented in the module
        docstring: defeat-resistance for sandboxed strategies is provided
        by the host sandbox runtime (which blocks introspection of the
        type via the restricted importer and frame filters, and
        re-asserts the ``SIGALRM`` guard on the next bytecode boundary so
        any surviving exception is translated into a hard kill), **not**
        by the exception hierarchy.

        This test pins down the new contract: a directly-raised
        :class:`SandboxResourceError` inside the body is catchable by
        ``except Exception``, and the guarded region exits cleanly.  We
        do *not* assert that an infinite ``except Exception: pass`` loop
        is killed at this module layer, because at this layer it is not
        — that is the host sandbox's responsibility.  A future
        contributor who silently re-introduces the hierarchy-based
        defence will see this test fail, prompting a review of the
        design rather than a quiet regression.
        """
        limits = ResourceLimits(cpu_timeout_seconds=0.1, max_memory_mb=0)
        delivery_flag: list[str] = []

        def _body_that_swallows_and_exits() -> None:
            # Raise a SandboxResourceError directly and verify the
            # blanket ``except Exception`` swallows it as documented —
            # this is the contract host-side code (task workers,
            # observability middleware) relies on, and is also what a
            # hostile strategy could exploit at *this* module layer
            # (the host sandbox re-asserts SIGALRM to defeat that).
            def _raise_violation() -> None:
                raise SandboxResourceError(CPU_RESOURCE, limit=0.1)

            try:
                _raise_violation()
            except Exception:
                # host-side-catch / strategy-side-swallow contract.
                delivery_flag.append("swallowed")

        # The guarded body runs to completion (no SandboxResourceError
        # escapes to the ``with`` block) — this is the new contract.
        with resource_limits(limits):
            _body_that_swallows_and_exits()
        assert delivery_flag == ["swallowed"]

    @requires_sigalrm
    def test_cpu_error_carries_limit_metadata(self) -> None:
        limits = ResourceLimits(cpu_timeout_seconds=0.05, max_memory_mb=0)
        with pytest.raises(SandboxResourceError) as exc_info, resource_limits(limits):
            _hot_compute_loop()
        assert exc_info.value.kind == CPU_RESOURCE
        assert exc_info.value.limit == 0.05

    @requires_sigalrm
    def test_prior_sigalrm_handler_restored(self) -> None:
        """After the guarded block exits, the caller's prior ``SIGALRM``
        handler must be restored — even when the body raised."""
        original = signal.getsignal(signal.SIGALRM)

        def _my_handler(signum: int, frame: Any) -> None:
            pass

        signal.signal(signal.SIGALRM, _my_handler)
        try:
            limits = ResourceLimits(cpu_timeout_seconds=0.05, max_memory_mb=0)
            with pytest.raises(SandboxResourceError), resource_limits(limits):
                _hot_compute_loop()
            # After the guarded block the handler we installed is back.
            assert signal.getsignal(signal.SIGALRM) is _my_handler
        finally:
            signal.signal(signal.SIGALRM, original)

    @requires_sigalrm
    def test_cpu_guard_disabled_for_non_positive_timeout(self) -> None:
        """``cpu_timeout_seconds <= 0`` disables the CPU guard — the body
        runs to completion and no handler is installed."""
        original = signal.getsignal(signal.SIGALRM)
        try:
            limits = ResourceLimits(cpu_timeout_seconds=0, max_memory_mb=0)
            with resource_limits(limits):
                # If a handler WERE installed with a 0s timer it would fire
                # immediately; running a tiny bit of work confirms it does
                # not.
                _ = sum(range(1000))
            assert signal.getsignal(signal.SIGALRM) is original
        finally:
            signal.signal(signal.SIGALRM, original)

    @requires_sigalrm
    def test_itimer_disarmed_after_normal_exit(self) -> None:
        """After a clean guarded exit the ``ITIMER_REAL`` is disarmed
        (interval zero) so no spurious ``SIGALRM`` fires later."""
        original = signal.getsignal(signal.SIGALRM)
        try:
            limits = ResourceLimits(cpu_timeout_seconds=1.0, max_memory_mb=0)
            with resource_limits(limits):
                _ = sum(range(1000))
            seconds, _interval = signal.getitimer(signal.ITIMER_REAL)
            assert seconds == 0.0
        finally:
            signal.signal(signal.SIGALRM, original)


# ── 3. Memory guard ──────────────────────────────────────────────────


class TestMemoryGuard:
    """:mod:`tracemalloc`-based Python-allocation soft-cap."""

    def test_memory_breach_on_exit_raises(self) -> None:
        """Allocating more than ``max_memory_mb`` of *Python-tracked* memory
        (here: ``bytearray`` objects, which tracemalloc observes) raises a
        ``MEMORY_RESOURCE`` :class:`SandboxResourceError` on context exit."""
        limits = ResourceLimits(cpu_timeout_seconds=5.0, max_memory_mb=8)
        # ~16 MiB of bytearray buffers — comfortably over the 8 MiB
        # cap and *visible* to tracemalloc (unlike raw bytes() of
        # small repeated payloads, which CPython may dedup).
        with (
            pytest.raises(SandboxResourceError, match="memory") as exc_info,
            resource_limits(limits),
        ):
            _ALLOCATED.extend(bytearray(1024) for _ in range(16 * 1024))
        assert exc_info.value.kind == MEMORY_RESOURCE
        assert exc_info.value.limit == 8
        assert exc_info.value.actual is not None
        assert exc_info.value.actual > 8  # observed peak exceeds the cap
        _drain_allocated()

    def test_memory_under_cap_does_not_raise(self) -> None:
        """A small allocation well within the cap completes normally."""
        limits = ResourceLimits(cpu_timeout_seconds=5.0, max_memory_mb=64)
        with resource_limits(limits):
            _ALLOCATED.append(bytearray(1024))  # 1 KiB — far below 64 MiB
        _drain_allocated()

    def test_memory_guard_disabled_for_non_positive_cap(self) -> None:
        """``max_memory_mb <= 0`` disables the memory guard entirely."""
        limits = ResourceLimits(cpu_timeout_seconds=5.0, max_memory_mb=0)
        # Even an arbitrarily large allocation is allowed when the cap is
        # disabled; keep it modest so the test is fast.
        with resource_limits(limits):
            _ALLOCATED.append(bytearray(64 * 1024))
        _drain_allocated()


# ── 4. Single-flight guard (the headline task item 4b) ───────────────


class TestSingleFlightGuard:
    """The module-level :data:`_guard_lock` rejects re-entrant and concurrent
    entry into :func:`resource_limits` because the underlying state
    (``SIGALRM`` handler, tracemalloc peak counter) is process-global.

    A re-entrant call would otherwise clobber the outer guard's teardown
    (the inner exit restores the outer's handler and stops the outer's
    tracemalloc session mid-flight).  The rejection is raised as a
    :class:`SandboxResourceError` with ``kind="single_flight"`` so callers
    only need to catch the single Layer-3 exception type.
    """

    def test_reentrant_entry_from_same_thread_rejected(self) -> None:
        """**Critical regression test (task item 4b).**

        Entering :func:`resource_limits` while already inside another
        :func:`resource_limits` (on the same thread) must raise a clear
        :class:`SandboxResourceError` (``kind="single_flight"``) rather
        than deadlocking (a plain :class:`threading.Lock` would deadlock) or
        silently corrupting the outer guard's teardown state."""
        limits = ResourceLimits(cpu_timeout_seconds=2.0, max_memory_mb=0)
        with (
            resource_limits(limits),
            pytest.raises(SandboxResourceError, match="not re-entrant"),
            resource_limits(limits),
        ):
            pytest.fail("re-entrant entry must be rejected")

    def test_reentrant_error_message_names_the_cause(self) -> None:
        """The :class:`SandboxResourceError` raised on re-entrant entry must
        clearly explain *why* re-entry is forbidden (so the caller knows to
        serialise their guarded regions) and carry ``kind="single_flight"``
        metadata so callers can branch on it."""
        limits = ResourceLimits(cpu_timeout_seconds=2.0, max_memory_mb=0)
        with resource_limits(limits):
            with (
                pytest.raises(SandboxResourceError) as exc_info,
                resource_limits(limits),
            ):
                pass
            err = exc_info.value
            assert err.kind == SINGLE_FLIGHT_RESOURCE
            assert err.limit is None
            assert err.actual is None
            message = str(err)
            assert "resource_limits" in message
            assert "re-entrant" in message
            # Mentions the process-global resource that motivates the lock.
            assert "SIGALRM" in message or "tracemalloc" in message

    def test_lock_released_after_normal_exit(self) -> None:
        """After a normal guarded exit the single-flight lock is released —
        a subsequent call must succeed."""
        limits = ResourceLimits(cpu_timeout_seconds=2.0, max_memory_mb=0)
        assert not _guard_lock.locked()
        with resource_limits(limits):
            assert _guard_lock.locked()
        assert not _guard_lock.locked()
        # And a fresh call works.
        with resource_limits(limits):
            pass
        assert not _guard_lock.locked()

    def test_lock_released_after_cpu_violation(self) -> None:
        """The lock is released even when the body raises a CPU
        :class:`SandboxResourceError`."""
        if not _can_use_signals():
            pytest.skip("requires SIGALRM")
        limits = ResourceLimits(cpu_timeout_seconds=0.05, max_memory_mb=0)
        with pytest.raises(SandboxResourceError), resource_limits(limits):
            _hot_compute_loop()
        assert not _guard_lock.locked()

    def test_lock_released_after_memory_violation(self) -> None:
        """The lock is released even when teardown raises a memory
        :class:`SandboxResourceError`."""
        limits = ResourceLimits(cpu_timeout_seconds=5.0, max_memory_mb=8)
        with pytest.raises(SandboxResourceError), resource_limits(limits):
            _ALLOCATED.extend(bytearray(1024) for _ in range(16 * 1024))
        _drain_allocated()
        assert not _guard_lock.locked()

    def test_lock_released_after_body_exception(self) -> None:
        """The lock is released even when the body raises an unrelated
        exception."""
        limits = ResourceLimits(cpu_timeout_seconds=2.0, max_memory_mb=0)
        with pytest.raises(ValueError, match="boom"), resource_limits(limits):
            raise ValueError("boom")
        assert not _guard_lock.locked()

    def test_concurrent_entry_from_another_thread_rejected(self) -> None:
        """A second thread that attempts to enter :func:`resource_limits`
        while the main thread holds the lock is rejected with the same
        clear :class:`SandboxResourceError` (``kind="single_flight"``,
        raised in the second thread)."""
        limits = ResourceLimits(cpu_timeout_seconds=2.0, max_memory_mb=0)
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def _inner() -> None:
            barrier.wait()
            try:
                with resource_limits(limits):
                    # If we reach here re-entry was *not* rejected, which
                    # is the bug this test guards against — record a
                    # sentinel so the assertion below fails loudly.
                    errors.append(
                        RuntimeError("re-entry unexpectedly succeeded"),
                    )
            except BaseException as exc:
                # Capture every BaseException here so we record the
                # :class:`SandboxResourceError` regardless of any
                # ``KeyboardInterrupt`` / thread-teardown noise that might
                # fire during failure modes.  (``SandboxResourceError`` is
                # itself a regular :class:`RuntimeError` / :class:`Exception`
                # subclass under the current design, so an ``except Exception``
                # would suffice; we keep ``except BaseException`` here purely
                # for defence-in-depth in the test harness.)
                errors.append(exc)

        with resource_limits(limits):
            t = threading.Thread(target=_inner)
            t.start()
            # Release the barrier so the inner thread races the lock
            # against our held lock.
            barrier.wait()
            t.join(timeout=2.0)
        assert not t.is_alive(), "inner thread should have terminated promptly"
        assert len(errors) == 1
        assert isinstance(errors[0], SandboxResourceError)
        assert errors[0].kind == SINGLE_FLIGHT_RESOURCE
        assert "re-entrant" in str(errors[0])

    def test_sequential_entries_from_different_threads_succeed(self) -> None:
        """Once the lock is released, a *different* thread may enter
        :func:`resource_limits` normally — the single-flight guard is about
        overlap, not thread affinity."""
        limits = ResourceLimits(cpu_timeout_seconds=2.0, max_memory_mb=0)
        result: list[str] = []

        def _run() -> None:
            with resource_limits(limits):
                result.append("ok")

        t1 = threading.Thread(target=_run)
        t1.start()
        t1.join(timeout=2.0)
        assert not t1.is_alive()
        assert result == ["ok"]

        # And the main thread can enter immediately afterwards.
        with resource_limits(limits):
            result.append("main")


# ── 5. Graceful degradation ──────────────────────────────────────────


class TestGracefulDegradation:
    def test_can_use_signals_returns_bool(self) -> None:
        assert isinstance(_can_use_signals(), bool)

    def test_no_guards_when_limits_disabled(self) -> None:
        """With both guards disabled the context manager is a near-no-op —
        the only side effect is the single-flight lock being held and
        released, and the body runs unchanged."""
        limits = ResourceLimits(cpu_timeout_seconds=0, max_memory_mb=0)
        marker = []
        with resource_limits(limits):
            marker.append("ran")
        assert marker == ["ran"]
        assert not _guard_lock.locked()

    def test_nested_disabled_guards_still_rejected(self) -> None:
        """Even with both guards *disabled* the single-flight lock is in
        effect — there is still exactly one process-global lock state, so
        re-entry is rejected identically.  This pins down that the lock
        guards the *context manager entry*, not just the signal/tracemalloc
        setup."""
        limits = ResourceLimits(cpu_timeout_seconds=0, max_memory_mb=0)
        with (
            resource_limits(limits),
            pytest.raises(SandboxResourceError, match="re-entrant"),
            resource_limits(limits),
        ):
            pass
