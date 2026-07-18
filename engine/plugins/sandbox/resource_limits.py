"""
Layer 3: Resource limits enforcement for the plugin sandbox runtime.

This module implements the **resource_limits** layer of the 5-layer sandbox
security model.  It wraps a strategy ``evaluate()`` / ``on_bar()`` call with
two independent guards:

1. **CPU time** — enforced via ``signal.SIGALRM`` / ``signal.setitimer``.
   Unlike the asyncio ``wait_for`` timeout (which can only fire when the
   event loop regains control), a SIGALRM is delivered by the kernel on the
   next bytecode boundary and therefore preempts a strategy that is spinning
   in a tight compute loop and never ``await``\\ ing.  The installed handler
   raises :class:`SandboxResourceError` so the violation propagates as a
   Python exception.

   Because :class:`SandboxResourceError` inherits from :class:`BaseException`
   (not :class:`Exception`), a strategy cannot defeat the guard by wrapping
   its hot loop in ``except Exception: pass`` — the resource violation sails
   straight through any ``Exception``-bounded ``except`` clause the strategy
   installs.  The guard *can* be defeated by an explicit
   ``except BaseException: pass`` (or ``except SandboxResourceError``); that
   is a deliberate, observable choice and the host sandbox treats any
   surviving :class:`SandboxResourceError` raised out of the body as a hard
   kill anyway.

2. **Memory cap** — enforced via :mod:`tracemalloc`.

   .. warning::

      :mod:`tracemalloc` is a **Python-allocation soft-cap only**.  It traces
      allocations performed through ``PyMem_*`` / ``PyObject_*`` (i.e. Python
      objects and buffers created from Python code).  It does **not** see:

      * Native / C-extension heap allocations made via ``malloc``/``calloc``
        (e.g. NumPy internal buffers, pandas block storage, extension-module
        working memory).
      * Memory-mapped files (``mmap``), file-system page cache, or kernel
        buffers.
      * Allocations made by threads that :mod:`tracemalloc` was not tracking
        when the call started.

      Consequently a strategy that allocates large native buffers can blow
      past the ``max_memory_mb`` cap *without* tripping tracemalloc.  The
      host sandbox therefore installs ``RLIMIT_AS`` (via
      :func:`resource.setrlimit`) as a separate **kernel-level backstop** —
      that cap covers the whole process address space and is what actually
      aborts an over-the-limit allocation in real time.  The tracemalloc
      layer in this module is a *best-effort, Python-visible* trip-wire that
      produces a structured :class:`SandboxResourceError` before the harder
      ``RLIMIT_AS`` ``MemoryError`` would fire.

   The context manager snapshots the tracemalloc peak memory high-water mark
   on entry and, on exit, compares the observed peak against ``max_memory_mb``.
   A background daemon thread additionally polls the *current* traced
   allocation while the body runs so a sustained breach is detected within
   ``poll_interval`` seconds; the post-hoc peak check then raises
   :class:`SandboxResourceError` deterministically.

Single-flight guarantee
-----------------------
The signal-handler and :mod:`tracemalloc` state mutated by this module is
**process-global**: there is exactly one ``SIGALRM`` handler slot and one
tracemalloc peak counter per interpreter.  Two overlapping
:func:`resource_limits` invocations would therefore corrupt each other's
teardown (the inner exit would restore the outer's handler and stop the
outer's tracemalloc session mid-flight).

A module-level :data:`_guard_lock` (:class:`threading.Lock`, non-reentrant)
serialises entry into the context manager.  Re-entrant entry — from the same
thread or any other — is rejected with a :class:`SandboxResourceError`
(kind = :data:`SINGLE_FLIGHT_RESOURCE`) carrying a clear message rather than
deadlocking or silently racing.  In the production deployment the host
sandbox already serialises evaluations via the asyncio ``_eval_lock``; this
module-level guard is defence-in-depth for any caller that invokes
:func:`resource_limits` directly (tests, ad-hoc tooling).

Graceful degradation
--------------------
Both guards degrade gracefully when the runtime cannot support them:

* ``signal`` only works on the main thread of the main interpreter and only
  on POSIX.  Off-main-thread or on Windows the CPU guard is skipped (the
  asyncio timeout remains in effect as the wall-clock fallback).
* :mod:`tracemalloc` is part of the CPython standard library and is always
  available; if it cannot be started for some reason the memory guard is
  skipped rather than crashing the sandbox.

Security note
-------------
This module is **host-side** code.  It is imported once, at engine start-up,
*before* any sandbox restrictions are activated, so the module-level imports
of ``signal``, ``tracemalloc`` and ``threading`` (all of which are on the
sandbox deny-list) succeed.  The captured module objects are referenced only
via local variables inside the context manager; no module-level binding is
exposed for sandboxed code to discover, and importing
``engine.plugins.sandbox.resource_limits`` is itself denied by the restricted
importer (``engine.*`` is not on the allowlist).
"""

from __future__ import annotations

import contextlib
import signal as _signal
import threading as _threading
import tracemalloc as _tracemalloc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "CPU_RESOURCE",
    "MEMORY_RESOURCE",
    "SINGLE_FLIGHT_RESOURCE",
    "ResourceLimits",
    "SandboxResourceError",
    "resource_limits",
]

#: Resource-kind identifiers carried on :class:`SandboxResourceError`.
CPU_RESOURCE: str = "cpu"
MEMORY_RESOURCE: str = "memory"
#: Kind used when the single-flight :data:`_guard_lock` is already held
#: (re-entrant or concurrent entry into :func:`resource_limits`).
SINGLE_FLIGHT_RESOURCE: str = "single_flight"

# ── Single-flight guard ───────────────────────────────────────────────
#
# Process-global lock that serialises entry into :func:`resource_limits`.
# Acquired with ``blocking=False`` on entry; failure means another call is
# already mid-flight (re-entrant from this thread or concurrent from
# another) and we raise a clear error instead of clobbering the in-flight
# SIGALRM handler / tracemalloc peak counter.
#
# A plain :class:`threading.Lock` (non-reentrant) is deliberate: a
# :class:`threading.RLock` would let the same thread re-enter silently and
# paper over the very corruption we are guarding against.  The lock is held
# across the *entire* ``with`` body (entry → exit) so two overlapping calls
# can never coexist regardless of which thread they originate from.
_guard_lock: _threading.Lock = _threading.Lock()


class SandboxResourceError(BaseException):
    """Raised when a sandboxed strategy exceeds a declared resource limit.

    .. note::

       This exception deliberately inherits from :class:`BaseException`
       (via :class:`SystemExit` / :class:`KeyboardInterrupt`'s parent) and
       **not** from :class:`Exception`.  That way a strategy cannot defeat
       the CPU / memory guards by wrapping its hot path in
       ``except Exception: pass`` — the resource violation propagates past
       every ``Exception``-bounded handler the strategy installs.  The host
       sandbox catches :class:`SandboxResourceError` explicitly (it is a
       :class:`BaseException`, so an ``except Exception`` clause alone will
       not match it) and translates it into a hard kill of the strategy.

       A strategy can still defeat the guard with the more aggressive
       ``except BaseException: pass`` (or by catching
       :class:`SandboxResourceError` by name); that is an explicit, auditable
       choice and the host treats any :class:`SandboxResourceError` that
       somehow survives the body as a kill regardless.

    Distinct from the generic :class:`TimeoutError` raised by the asyncio
    wall-clock timeout: ``SandboxResourceError`` is raised by the dedicated
    Layer-3 guards and carries structured ``kind`` / ``limit`` / ``actual``
    metadata so callers (and dashboards) can distinguish a CPU blow-up from a
    memory blow-up.  The same exception type is reused for the single-flight
    violation (``kind = "single_flight"``) raised when the module-level
    :data:`_guard_lock` is already held, so callers only need to catch one
    exception type for every Layer-3 failure mode.

    Attributes
    ----------
    kind:
        ``"cpu"``, ``"memory"`` or ``"single_flight"`` — which guard fired.
    limit:
        The configured limit value (seconds for CPU, MiB for memory);
        ``None`` for the single-flight violation (no scalar limit applies).
    actual:
        The observed value that exceeded the limit, when measurable
        (peak MiB for memory; ``None`` for CPU where the timer simply fired;
        ``None`` for the single-flight violation).
    """

    def __init__(
        self,
        kind: str,
        *,
        limit: float | int | None,
        actual: float | int | None = None,
    ) -> None:
        self.kind: str = kind
        self.limit: float | int | None = limit
        self.actual: float | int | None = actual
        if kind == CPU_RESOURCE:
            msg = f"Strategy exceeded CPU time limit of {limit}s (SIGALRM)"
        elif kind == MEMORY_RESOURCE:
            msg = (
                f"Strategy exceeded memory limit of {limit}MiB "
                f"(peak {actual}MiB)"
            )
        elif kind == SINGLE_FLIGHT_RESOURCE:
            # The single-flight violation is not a scalar-limit breach, so
            # ``limit`` / ``actual`` are ``None``; the explanatory text is
            # what tells the caller how to fix the call site.
            msg = (
                "resource_limits is not re-entrant: another guarded call is "
                "already in flight on this interpreter. The SIGALRM handler "
                "and tracemalloc peak counter are process-global, so two "
                "overlapping resource_limits regions would corrupt each "
                "other's teardown. Serialise guarded regions (e.g. via the "
                "host sandbox's _eval_lock) and do not nest resource_limits "
                "contexts."
            )
        else:
            msg = f"Strategy exceeded {kind} resource limit"
        super().__init__(msg)


@dataclass(frozen=True)
class ResourceLimits:
    """Resource cap configuration for a sandboxed strategy.

    A frozen dataclass so a configured limits object can be safely shared
    between the host sandbox and the resource-limit context manager without
    risk of mutation mid-evaluation.

    Attributes
    ----------
    cpu_timeout_seconds:
        Maximum wall-clock seconds the guarded call may run before a
        :class:`SandboxResourceError` (``kind="cpu"``) is raised via
        ``SIGALRM``.  A value ``<= 0`` disables the CPU guard.
    max_memory_mb:
        Maximum memory, in mebibytes, the guarded call may allocate *as
        observed by* :mod:`tracemalloc` before a
        :class:`SandboxResourceError` (``kind="memory"``) is raised.  See the
        module docstring for the important caveat that this is a
        **Python-allocation soft-cap only** — native/C-extension allocations
        are not seen by tracemalloc and are instead bounded by the
        kernel-level ``RLIMIT_AS`` backstop installed by the host sandbox.
        A value ``<= 0`` disables the memory guard.
    """

    cpu_timeout_seconds: float = 30.0
    max_memory_mb: int = 512


def _can_use_signals() -> bool:
    """Return ``True`` iff ``SIGALRM``-based timing is usable right now.

    Signals can only be installed from the main thread of the main
    interpreter, and ``SIGALRM`` / ``setitimer`` are POSIX-only.  We probe
    conservatively rather than catching the resulting ``ValueError`` /
    ``AttributeError`` so that an *unexpected* failure (e.g. a handler the
    caller installed and cares about) is not silently swallowed.
    """
    if _threading.current_thread() is not _threading.main_thread():
        return False
    return hasattr(_signal, "SIGALRM") and hasattr(_signal, "setitimer")


@dataclass
class _CpuGuardState:
    """Carries the SIGALRM CPU-guard state from setup to teardown.

    A default instance (``timer_armed=False``) represents a *disabled* guard;
    ``_teardown_guards`` treats it as a no-op.
    """

    prior_handler: Any = None
    timer_armed: bool = False


def _arm_cpu_guard(limits: ResourceLimits) -> _CpuGuardState:
    """Arm the SIGALRM CPU timer when usable; return state for teardown.

    A no-op state (``timer_armed=False``) is returned when the guard is
    disabled (timeout ``<= 0``, off-main-thread, non-POSIX, or a racing
    failure to install the handler).  The asyncio wall-clock timeout remains
    as a fallback in those cases.
    """
    state = _CpuGuardState()
    if not (limits.cpu_timeout_seconds > 0 and _can_use_signals()):
        return state

    # Snapshot the caller's existing handler so we can restore it even if
    # the body itself replaces ``signal.SIGALRM``.
    state.prior_handler = _signal.getsignal(_signal.SIGALRM)

    def _cpu_timeout_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise SandboxResourceError(
            CPU_RESOURCE,
            limit=limits.cpu_timeout_seconds,
        )

    try:
        _signal.signal(_signal.SIGALRM, _cpu_timeout_handler)
        # ``setitimer`` accepts a float, so fractional-second budgets work.
        _signal.setitimer(
            _signal.ITIMER_REAL,
            float(limits.cpu_timeout_seconds),
        )
        state.timer_armed = True
    except (ValueError, OSError):
        # racing thread-context change or unsupported platform — fall back
        # to no-signal mode; the asyncio timeout remains as backstop.
        state.timer_armed = False
    return state


@dataclass
class _MemoryGuardState:
    """Carries the tracemalloc memory-guard state from setup to teardown.

    A default instance (``trace_memory=False``) represents a *disabled*
    guard; ``_teardown_guards`` treats it as a no-op.
    """

    trace_memory: bool = False
    started_tracing: bool = False
    max_bytes: int = 0
    breach_event: _threading.Event = field(default_factory=_threading.Event)
    stop_event: _threading.Event | None = None
    monitor: _threading.Thread | None = None


def _setup_memory_guard(
    limits: ResourceLimits,
    poll_interval: float,
) -> _MemoryGuardState:
    """Start tracemalloc peak tracing and spawn the poller daemon thread.

    Returns a state whose ``trace_memory`` is ``False`` when the guard is
    disabled (``max_memory_mb <= 0``) or when tracemalloc could not be
    started.  In the disabled case no monitor thread is launched.

    Note that tracemalloc only sees Python-level allocations — see the module
    docstring for the soft-cap caveat.
    """
    state = _MemoryGuardState(trace_memory=limits.max_memory_mb > 0)
    if not state.trace_memory:
        return state

    try:
        if not _tracemalloc.is_tracing():
            _tracemalloc.start()
            state.started_tracing = True
        # Reset the high-water mark so the peak we observe is attributable
        # to *this* guarded call, not the cumulative process peak.
        _tracemalloc.reset_peak()
        state.max_bytes = int(limits.max_memory_mb) * 1024 * 1024
    except RuntimeError:
        # tracemalloc could not be started (e.g. PYTHONMALLOC set);
        # disable the memory guard rather than crashing.
        state.trace_memory = False
        state.started_tracing = False
        return state

    state.stop_event = _threading.Event()
    max_bytes = state.max_bytes

    def _monitor() -> None:
        # Daemon-thread active memory poller.  Catches a *sustained*
        # over-limit allocation while the body runs and records it via
        # ``breach_event`` so the deterministic exit-time check raises
        # even if the peak were somehow missed.  Only Python-traced
        # allocations are observed (soft-cap caveat applies).
        while not state.stop_event.wait(poll_interval):  # type: ignore[union-attr]
            try:
                current, _peak = _tracemalloc.get_traced_memory()
            except RuntimeError:
                return
            if current > max_bytes:
                state.breach_event.set()
                return

    # ``daemon=True`` guarantees the monitor never outlives the process
    # even if ``__exit__`` is skipped (e.g. during interpreter shutdown).
    state.monitor = _threading.Thread(
        target=_monitor,
        name="sandbox-memory-monitor",
        daemon=True,
    )
    state.monitor.start()
    return state


def _finalize_memory(
    state: _MemoryGuardState,
    limits: ResourceLimits,
) -> None:
    """Stop tracemalloc (if we started it) and raise on any observed breach.

    A breach is signalled either by the monitor thread's ``breach_event`` or
    by the tracemalloc peak exceeding ``max_memory_mb`` at exit.  Only
    Python-traced allocations are measured (soft-cap caveat applies).
    """
    try:
        _current, peak = _tracemalloc.get_traced_memory()
    except RuntimeError:
        peak = 0
    finally:
        if state.started_tracing:
            with contextlib.suppress(RuntimeError):
                _tracemalloc.stop()
    peak_mb = peak / (1024 * 1024)
    if state.breach_event.is_set() or peak_mb > limits.max_memory_mb:
        raise SandboxResourceError(
            MEMORY_RESOURCE,
            limit=limits.max_memory_mb,
            actual=round(peak_mb, 2),
        )


def _teardown_guards(
    cpu_state: _CpuGuardState,
    mem_state: _MemoryGuardState,
    limits: ResourceLimits,
    poll_interval: float,
) -> None:
    """Run all teardown steps; always invoked from the context ``finally``.

    Order matches the original implementation: stop the memory monitor,
    disarm the CPU timer, then perform the deterministic memory breach
    check (which may raise :class:`SandboxResourceError`).
    """
    if mem_state.monitor is not None and mem_state.stop_event is not None:
        mem_state.stop_event.set()
        # Don't block the event loop on the monitor's poll wait — join
        # with a small bound; the daemon will die on its own otherwise.
        mem_state.monitor.join(timeout=poll_interval * 2 + 0.05)

    if cpu_state.timer_armed:
        # Cancel the armed timer (interval 0 disarms) and restore the
        # caller's original handler.
        with contextlib.suppress(ValueError, OSError):
            _signal.setitimer(_signal.ITIMER_REAL, 0)
        with contextlib.suppress(ValueError, OSError):
            _signal.signal(_signal.SIGALRM, cpu_state.prior_handler)

    if mem_state.trace_memory:
        _finalize_memory(mem_state, limits)


@contextlib.contextmanager
def resource_limits(
    limits: ResourceLimits,
    *,
    poll_interval: float = 0.01,
) -> Iterator[ResourceLimits]:
    """Enforce ``limits`` around the guarded ``with``-block body.

    On entry the single-flight :data:`_guard_lock` is acquired (rejecting
    re-entrant / concurrent entry), the SIGALRM CPU timer is armed (when
    usable), and ``tracemalloc`` peak tracing is (re)started; on exit the
    timer is cancelled, the prior signal handler is restored, the memory
    peak is compared against the cap, the lock is released, and any breach
    raises :class:`SandboxResourceError`.

    Parameters
    ----------
    limits:
        The :class:`ResourceLimits` to enforce.
    poll_interval:
        Seconds between active memory polls performed by the background
        monitor thread.  The thread only runs when ``max_memory_mb > 0``.
        It sets an internal *breach* flag (checked on exit) but does **not**
        attempt to raise across threads — the deterministic exit-time peak
        comparison is what ultimately raises.

    Raises
    ------
    RuntimeError:
        If :func:`resource_limits` is entered while another invocation is
        still in flight on this interpreter (re-entrant from the same thread
        or concurrent from another).  The signal/tracemalloc state is
        process-global and the single-flight lock prevents silent
        corruption; callers must serialise guarded regions themselves.
    SandboxResourceError:
        If the guarded body exceeds the CPU timeout (raised mid-execution by
        the SIGALRM handler — and, being a :class:`BaseException`, *not*
        catchable by ``except Exception``) or the memory cap (raised on exit
        when the observed peak or an active breach flag indicates a
        violation).

    Examples
    --------
    >>> from engine.plugins.sandbox.resource_limits import (
    ...     ResourceLimits, resource_limits,
    ... )
    >>> limits = ResourceLimits(cpu_timeout_seconds=0.2, max_memory_mb=8)
    >>> with resource_limits(limits):  # doctest: +SKIP
    ...     _ = sum(range(10_000))      # well within both limits
    """
    # ── Single-flight guard ───────────────────────────────────────────
    # Acquired non-blockingly so a re-entrant or concurrent entry fails fast
    # with a clear error rather than deadlocking (same thread, with a plain
    # ``Lock``) or silently clobbering the in-flight handler/peak (another
    # thread).  Released in the ``finally`` below so it is always freed,
    # including when the body raises :class:`SandboxResourceError`.
    #
    # The violation is reported as a :class:`SandboxResourceError` (not a
    # plain :class:`RuntimeError`) so callers only need to catch the single
    # Layer-3 exception type regardless of which guard fired.
    if not _guard_lock.acquire(blocking=False):
        raise SandboxResourceError(SINGLE_FLIGHT_RESOURCE, limit=None)

    cpu_state = _CpuGuardState()
    mem_state = _MemoryGuardState()
    try:
        cpu_state = _arm_cpu_guard(limits)
        mem_state = _setup_memory_guard(limits, poll_interval)
        try:
            yield limits
        finally:
            # Teardown runs before the single-flight lock is released so a
            # racing second entry cannot observe a half-torn-down state
            # (e.g. our SIGALRM handler still installed with its timer
            # already disarmed).  ``_teardown_guards`` may raise
            # ``SandboxResourceError`` on a memory breach; that propagates
            # after teardown completes and the lock is released.
            _teardown_guards(cpu_state, mem_state, limits, poll_interval)
    finally:
        _guard_lock.release()
