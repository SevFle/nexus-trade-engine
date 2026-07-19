# ADR-0012: Sandbox resource limits — SIGALRM + tracemalloc + single-flight lock

- **Status**: Accepted
- **Date**: 2026-07-16
- **Deciders**: Lead maintainer + security reviewer
- **Tags**: security, plugins, sandbox

## Context and Problem Statement

[ADR-0011](0011-runtime-introspection-blocking.md) closed the dynamic
`getattr` escape class. [ADR-0007](0007-strategy-sandbox-allowlist-imports.md)
closed `import`. [ADR-0010](0010-static-ast-validation-toctou-loading.md)
closed `__import__`/`exec`/`eval`/`compile` and the loader TOCTOU window.
The 5-layer sandbox table in
[`architecture/plugins.md`](../architecture/plugins.md#sandboxing) names
**Layer 3 — Resource limits** as a best-effort in-process control with a
concrete job: stop a buggy or hostile strategy from running away with the
host process by **spinning on the CPU** or **allocating unbounded memory**.

Until Layer 3 landed, the only resource control was the asyncio
`wait_for(self._call_strategy(...), timeout=self._max_eval_seconds)` in
[`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py)
plus a kernel-level `RLIMIT_AS` / `RLIMIT_NOFILE` cap applied by the host
sandbox's `_apply_resource_limits`. That combination has two documented
holes:

1. **`wait_for` cannot preempt a non-`await`ing tight loop.** The asyncio
   timeout fires only when the event loop regains control. A strategy
   that does `while True: pass` (or the moral equivalent in a hot C
   extension call that never yields) starves the loop and the timeout
   never raises. In production this hangs the engine's evaluator task
   and, because evaluations are serialised by the `_eval_lock`, every
   subsequent strategy on that worker.
2. **`RLIMIT_AS` aborts via `MemoryError`, not a structured signal.** The
   kernel backstop is essential (it is the only thing that bounds native /
   C-extension allocations `tracemalloc` cannot see — NumPy buffers,
   `mmap`, etc.), but a raw `MemoryError` carries no `kind`/`limit`/
   `actual` metadata, gets raised from wherever the failing allocation
   happens to land, and is trivially caught and swallowed by a strategy
   that wraps its hot loop in `except Exception: pass`. Dashboards then
   cannot distinguish a CPU blow-up from a memory blow-up, and a hostile
   strategy can keep eating the cap forever.

This ADR records why Layer 3 was built as a **standalone host-side
`resource_limits(...)` context manager** in
[`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py),
why `SandboxResourceError` inherits from `BaseException` (not `Exception`),
and why a module-level single-flight lock serialises entry into the
context manager.

## Decision Drivers

- **Preempt tight compute loops the asyncio timeout cannot reach.** Only a
  kernel-delivered signal — `SIGALRM` via `setitimer(ITIMER_REAL, ...)`,
  which fires on the next bytecode boundary — can preempt a non-`await`ing
  body. The signal handler raises a structured exception so the violation
  propagates as a Python-level error rather than killing the process.
- **Defeat-resistance against the canonical `except Exception: pass`
  wrap.** A hostile strategy's first move is to wrap its hot loop in
  `try: ... except Exception: pass`. If the resource-violation signal
  derived from `Exception`, that wrap would swallow it and the cap would
  be cosmetic. The exception type is therefore the load-bearing defeat
  resistance primitive of Layer 3.
- **Bound Python-visible allocations before the kernel `MemoryError`.**
  `tracemalloc` sees `PyMem_*` / `PyObject_*` allocations, so a strategy
  that allocates large Python objects can be caught with a structured
  `SandboxResourceError(kind="memory", limit=…, actual=…)` *before* the
  harder `RLIMIT_AS` `MemoryError` would fire. Native allocations remain
  the kernel backstop's job; tracemalloc is a best-effort trip-wire, not
  the hard cap.
- **No silent corruption of process-global state.** There is exactly one
  `SIGALRM` handler slot and one `tracemalloc` peak counter per
  interpreter. Two overlapping `resource_limits(...)` regions would
  clobber each other's teardown (the inner exit restores the outer's
  handler and stops the outer's tracemalloc session mid-flight). Whatever
  we ship must make that physically impossible.
- **Honest labelling.** Layer 3 is in-process best-effort. It is not a
  security boundary — Layer 5 (process/container isolation) is. The
  design must say so plainly, and must degrade gracefully when the
  runtime cannot support a given guard rather than crashing the sandbox.

## Considered Options

1. **Do nothing in Layer 3; keep `wait_for` + `RLIMIT_AS` only.** Document
   that tight-loop CPU hangs and unstructured `MemoryError`s are the
   status quo until Layer 5 lands.
2. **`resource.setrlimit(RLIMIT_CPU, …)` for CPU.** A kernel-delivered
   `SIGXCPU` on CPU-time exhaustion. No Python-visible tracemalloc
   memory cap.
3. **A monitor thread that polls process stats (`psutil.Process.cpu_percent`,
   `memory_info().rss`) and cancels the asyncio task on breach.**
4. **SIGALRM-based CPU guard + `tracemalloc` peak/current memory cap + a
   module-level non-reentrant `threading.Lock` to serialise entry, with
   `SandboxResourceError(BaseException)` as the violation type (chosen).**

## Decision Outcome

Chosen option: **Option 4**, because it is the only option that combines
*kernel-level CPU preemption* (Option 1's gap), *structured Python-visible
memory detection before the kernel `MemoryError`* (Option 2's gap), and
*correct handling of the process-global state the two guards mutate*
(Option 3's gap). It also degrades gracefully on platforms where signals
or tracemalloc are unavailable.

The module
[`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py)
ships five things together. They are designed as a single defence unit —
picking one out in isolation would re-open one of the holes above.

1. **`resource_limits(limits: ResourceLimits)` context manager.** The
   public entry point. On entry it acquires the single-flight lock, arms
   the SIGALRM CPU timer (when usable), and (re)starts `tracemalloc`
   peak tracing. On exit it stops the memory monitor, disarms the timer,
   restores the caller's prior `SIGALRM` handler, performs the
   deterministic exit-time peak comparison, and releases the lock. A
   breach surfaces as `SandboxResourceError`.

2. **CPU guard via `signal.SIGALRM` / `signal.setitimer(ITIMER_REAL, …)`.**
   The handler raises `SandboxResourceError(CPU_RESOURCE, limit=…)` from
   the next bytecode boundary, so a strategy that spins without `await`ing
   is preempted *without* the event loop ever getting control back. The
   caller's existing handler is snapshotted on entry and restored on exit
   even if the body itself replaces `signal.SIGALRM`, so the guard is
   safe to compose with a host that has its own signal handlers.

3. **Memory cap via `tracemalloc`.** On entry we `reset_peak()` so the
   observed peak is attributable to *this* guarded call (not the
   cumulative process peak). A daemon-thread poller wakes every
   `poll_interval` (default 10 ms), reads `tracemalloc.get_traced_memory()`,
   and sets a `breach_event` on a sustained over-limit allocation. On
   exit, `_finalize_memory` reads the peak one final time and raises
   `SandboxResourceError(MEMORY_RESOURCE, limit=…, actual=peak_mb)` if
   either the `breach_event` fired or the peak exceeds the cap. The
   daemon flag on the thread guarantees it never outlives the process
   even if `__exit__` is skipped (e.g. during interpreter shutdown).

4. **`SandboxResourceError(BaseException)` — the defeat-resistance
   primitive.** This is the single most important invariant of Layer 3.
   The class inherits **directly from `BaseException`, not from
   `Exception`**, so the canonical defeat-attempt
   (`except Exception: pass` around the hot loop) **cannot** match it.
   If the inheritance were ever reverted to derive from `RuntimeError` /
   `Exception`, the CPU timeout would become trivially defeatable and
   the whole guard cosmetic. Host-side callers that wish to observe a
   violation must therefore catch `BaseException` (or the specific
   `SandboxResourceError` type) — a blanket `except Exception` will *not*
   see it. That is intended: a resource violation is a hard kill that
   should propagate past ordinary error-handling middleware, not be
   silently logged-and-swallowed as a routine strategy error.

   The same exception type carries `kind` (`"cpu"` / `"memory"` /
   `"single_flight"`), `limit`, and `actual` metadata so dashboards can
   tell a CPU blow-up from a memory blow-up without parsing the message
   string, and so the single-flight violation can reuse the same catch
   type without inventing a second exception.

5. **Module-level non-reentrant `threading.Lock` (`_guard_lock`).**
   Serialises entry into `resource_limits` because `SIGALRM` handler
   slots and the `tracemalloc` peak counter are process-global.
   `_guard_lock.acquire(blocking=False)` on entry: failure means another
   guarded call is mid-flight and we raise
   `SandboxResourceError(SINGLE_FLIGHT_RESOURCE)` rather than deadlocking
   (same thread, with a plain `Lock`) or silently corrupting the
   in-flight handler / peak (another thread). A plain `Lock` is
   deliberate — a `RLock` would let the same thread re-enter silently
   and paper over the very corruption we are guarding against. The lock
   is released in a `finally` so it is always freed, including when the
   body itself raises `SandboxResourceError`.

### Consequences

- **Positive** — a tight compute loop that never `await`s is preempted
  on the next bytecode boundary by the kernel-delivered `SIGALRM`, not
  starved until the worker is restarted.
- **Positive** — the canonical `except Exception: pass` defeat-attempt
  fails on its own: `SandboxResourceError` is a `BaseException`
  subclass and does not match the generic `except Exception` clause.
  The introspection blocks from [ADR-0011](0011-runtime-introspection-blocking.md)
  are additional defence-in-depth (a sandboxed strategy cannot name the
  `SandboxResourceError` type in an `except` clause because importing
  `engine.*` is denied and host frames are stripped from raised
  exceptions), but the `BaseException` base is the *first* line of
  defence.
- **Positive** — Python-visible allocations produce a structured
  `SandboxResourceError(kind="memory", limit=…, actual=…)` *before* the
  harder `RLIMIT_AS` `MemoryError` would fire, so dashboards get
  actionable metadata.
- **Positive** — overlapping invocations are physically impossible
  (`_guard_lock` rejects re-entrant and concurrent entry with a clear
  error), so the process-global SIGALRM handler and tracemalloc peak
  counter cannot be corrupted.
- **Positive** — graceful degradation: when `signal` is unavailable
  (off-main-thread, non-POSIX) the CPU guard is skipped and the asyncio
  `wait_for` wall-clock timeout remains as fallback; when `tracemalloc`
  cannot start (e.g. `PYTHONMALLOC`) the memory guard is skipped rather
  than crashing the sandbox; values `<= 0` for either limit disable the
  corresponding guard.
- **Negative — and stated plainly — Layer 3 is not a security boundary.**
  It is best-effort in-process, exactly like Layers 0–4. A hostile
  strategy that finds a path past the import and introspection controls
  can still attempt to escape before the guards fire (or after the timer
  is disarmed but before teardown completes — see "What this does *not*
  cover" below). Real sandboxing is Layer 5's job.
- **Negative** — `tracemalloc` is a **Python-allocation soft-cap only**.
  It traces allocations performed through `PyMem_*` / `PyObject_*`. It
  does **not** see native / C-extension heap allocations (`malloc`/
  `calloc`, NumPy internal buffers, pandas block storage, extension
  working memory), memory-mapped files, file-system page cache, or
  kernel buffers. The kernel-level `RLIMIT_AS` backstop installed by
  the host sandbox's `_apply_resource_limits` is what actually bounds
  the address space; the tracemalloc layer is the *structured*
  Python-visible trip-wire that fires before the harder `MemoryError`.
- **Negative** — `SIGALRM` only works on the main thread of the main
  interpreter and only on POSIX. Off-main-thread or on Windows the CPU
  guard is skipped; the asyncio wall-clock timeout remains in effect as
  fallback but, as noted in the Context, it cannot preempt a
  non-`await`ing loop. On those runtimes a hostile tight loop is still
  uncatchable in-process.
- **Negative** — the single-flight lock serialises every
  `resource_limits(...)` call across the whole interpreter. In the
  production deployment this is fine (the host sandbox already
  serialises evaluations via `_eval_lock`), but ad-hoc tooling and
  tests that try to nest or parallelise guarded regions get a
  `SandboxResourceError(kind="single_flight")` instead. That is the
  intended behaviour — see "Why a hard error, not a queue or reentrant
  lock" below.

## Details

### Why `SIGALRM` over `RLIMIT_CPU` / `SIGXCPU`

`RLIMIT_CPU` counts *consumed CPU seconds* and delivers `SIGXCPU` (which
by default core-dumps) on exhaustion. We rejected it for three reasons:

- **It is a *consumed* counter, not a wall-clock budget.** A strategy
  that does `time.sleep(60)` consumes ~0 s of CPU and would not trip
  the limit, but the asyncio `wait_for` covers that case fine. The gap
  Layer 3 needs to close is the *non-yielding CPU-bound* loop, so
  `setitimer(ITIMER_REAL, …)` (wall-clock) is the right timer.
- **`SIGXCPU`'s default disposition is to terminate.** Layer 3 wants a
  Python-level exception, not a process kill. We can install our own
  `SIGXCPU` handler, but the handler is process-global too, so we would
  be paying the single-flight complexity this ADR already pays for
  `SIGALRM` and gaining nothing.
- **`SIGALRM` / `setitimer` compose with the host's existing signal
  handlers.** The prior handler is snapshotted on entry and restored on
  exit, so a host that already uses `SIGALRM` (e.g. for its own
  timeouts) is not silently overwritten.

### Why a `BaseException`, not an `Exception`

The class docstring spells it out, but it bears repeating because the
inheritance choice is load-bearing and easy to "tidy up" by accident.

A strategy can wrap its hot loop in `try: ... except Exception: pass`.
This is the *canonical* defeat-attempt because every Python tutorial
teaches `except Exception` as the polite way to catch "real" errors. If
`SandboxResourceError` derived from `Exception`, that wrap would match
and swallow the violation; the strategy would keep running, the cap
would be cosmetic, and dashboards would show nothing.

`SandboxResourceError` therefore inherits directly from `BaseException`.
The generic `except Exception` clause does not match a `BaseException`
subclass, so the canonical defeat-attempt fails on its own. The
introspection blocks from [ADR-0011](0011-runtime-introspection-blocking.md)
mean a sandboxed strategy cannot even *name* `SandboxResourceError` in an
`except` clause (importing `engine.*` is denied; host frames are stripped
from raised exceptions), but the `BaseException` base is the *first*
line of defence and is what makes the guard robust against a strategy
that somehow does manage to introspect host types.

Host-side callers that wish to observe a violation must catch
`BaseException` (or the specific `SandboxResourceError` type). This is
documented on the class and tested by
`test_sandbox_resource_error_inherits_from_baseexception` /
`test_cpu_timeout_kills_strategy_that_swallows_exception` in
[`tests/test_sandbox_resource_limits.py`](../../tests/test_sandbox_resource_limits.py).

This choice landed in #1545 (`fix(sandbox): inherit
SandboxResourceError from BaseException`). The earlier implementation
inherited from `RuntimeError`; the fix is what made the guard actually
defeat-resistant.

### Why a single-flight lock, not a queue or a reentrant lock

The SIGALRM handler slot and the tracemalloc peak counter are
process-global. Two overlapping `resource_limits(...)` regions would
corrupt each other's teardown:

- The *inner* `__exit__` would restore the *outer's* snapshotted signal
  handler while the outer body is still running — disarming the outer's
  CPU guard prematurely.
- The *inner* `__exit__` would call `tracemalloc.stop()` (if the inner
  call was what started tracing), throwing away the peak the outer is
  about to read.
- The *inner* teardown's deterministic peak comparison would read a
  peak contaminated by the outer body's allocations.

We considered three synchronisation strategies:

- **`threading.RLock` (reentrant).** Would let the same thread re-enter
  silently and paper over the very corruption above. Rejected.
- **A queue of pending guarded regions.** Would make re-entrant calls
  *block* until the outer region exits. This looks polite but is almost
  always wrong: a re-entrant call from inside a strategy body would
  deadlock (the outer body is waiting for the inner call, the inner
  call is waiting for the outer body to return), and a concurrent call
  from another thread would block indefinitely in a code path that
  almost certainly did not expect to wait. Rejected — fail fast is
  better than fail silent.
- **Non-reentrant `threading.Lock`, `acquire(blocking=False)`, raise on
  failure (chosen).** Re-entrant entry (same thread) and concurrent
  entry (another thread) both fail fast with a clear
  `SandboxResourceError(kind="single_flight")` whose message names the
  process-global resource that motivates the lock and tells the caller
  how to fix the call site (serialise guarded regions, e.g. via the
  host sandbox's `_eval_lock`). In the production deployment the host
  sandbox already serialises evaluations, so the single-flight lock is
  defence-in-depth for callers that invoke `resource_limits` directly
  (tests, ad-hoc tooling).

### Teardown ordering

The `finally` in `__exit__` does four things in a deliberate order:

1. **Stop the memory monitor thread** (`stop_event.set()` + bounded
   `join`). The monitor only sets a flag; it does not raise across
   threads (the deterministic exit-time check is what raises).
2. **Disarm the CPU timer and restore the prior `SIGALRM` handler.**
   `setitimer(ITIMER_REAL, 0)` disarms; `signal(SIGALRM, prior_handler)`
   restores. Both are wrapped in `contextlib.suppress(ValueError,
   OSError)` so a racing thread-context change cannot crash teardown.
3. **Finalise memory** — read the peak one last time, stop tracemalloc
   if we started it, and raise `SandboxResourceError(MEMORY_RESOURCE)`
   on breach.
4. **Release the single-flight lock** in the outer `finally` so it is
   freed even if step 3 raised.

Teardown runs *before* the single-flight lock is released, so a racing
second entry cannot observe a half-torn-down state (e.g. our SIGALRM
handler still installed with its timer already disarmed).

### What this does *not* cover

- **Native / C-extension allocations.** `tracemalloc` does not see
  `malloc`/`calloc`, NumPy internal buffers, `mmap`, file-system page
  cache, or kernel buffers. The kernel-level `RLIMIT_AS` backstop
  installed by the host sandbox's `_apply_resource_limits` is what
  bounds the address space; a strategy that allocates huge native
  buffers can blow past the `max_memory_mb` cap *without* tripping
  tracemalloc, and the harder `MemoryError` is what aborts it. The
  module docstring states this plainly.
- **CPU consumption inside C extensions that block signal delivery.**
  `SIGALRM` is delivered on the next bytecode boundary, but a C
  extension that enters a `Py_BEGIN_ALLOW_THREADS` region and spins
  there will not be preempted until it returns to Python. This is a
  fundamental limitation of in-process signal-based preemption; Layer
  5 (process kill) is the only complete answer.
- **Off-main-thread or non-POSIX runtimes.** `_can_use_signals()`
  returns `False` and the CPU guard is skipped. The asyncio wall-clock
  timeout remains in effect but cannot preempt a non-`await`ing loop
  (see Context). On those runtimes a hostile tight loop is still
  uncatchable in-process.
- **Reaching the `SandboxResourceError` type from sandboxed code.** The
  `BaseException` base is the *first* line of defence; the introspection
  blocks from [ADR-0011](0011-runtime-introspection-blocking.md) are
  the second. Together they make the canonical defeat-attempt fail
  twice over, but Layer 5 is the only complete answer.

### Integration status (important caveat)

As of this ADR, the `resource_limits` module is **landed, tested, and
documented but not yet wired into the active host sandbox.** The host
sandbox in
[`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py)
still uses its own `_apply_resource_limits` (`RLIMIT_AS` /
`RLIMIT_NOFILE` via `resource.setrlimit`) and the asyncio `wait_for`
wall-clock timeout. The `resource_limits(...)` context manager is a
reusable, fully unit-tested primitive (30 tests in
[`tests/test_sandbox_resource_limits.py`](../../tests/test_sandbox_resource_limits.py))
that is **defence-in-depth waiting to be plugged in**. Wiring it into
`_evaluate_inner` around the `_call_strategy` body is tracked as a
follow-up; the ADR records the *design* so the wiring PR does not need
to re-litigate any of the choices above.

This status is mirrored in the Layer-3 row of the sandbox table in
[`architecture/plugins.md`](../architecture/plugins.md#sandboxing) and in
[`known-limitations.md`](../known-limitations.md).

## Pros and Cons of the Options

### Option 1 — Do nothing in Layer 3; keep `wait_for` + `RLIMIT_AS`

- **Pros:** Simplest; zero new code; the asyncio timeout is honest about
  its limits.
- **Cons:** A non-`await`ing tight loop hangs the worker indefinitely;
  `RLIMIT_AS` aborts via unstructured `MemoryError`; the canonical
  `except Exception: pass` defeat-attempt swallows it. We would be
  shipping a known trivial DoS with no speed bump.

### Option 2 — `RLIMIT_CPU` / `SIGXCPU` for CPU

- **Pros:** Kernel-delivered signal; no monkeypatching of `signal`.
- **Cons:** Counts *consumed* CPU seconds, not wall-clock, so a
  `time.sleep` loop is exempt (the asyncio timeout already covers that
  case, so we gain nothing in the gap Layer 3 needs to close). `SIGXCPU`'s
  default disposition is to terminate; we would need our own handler
  anyway, paying the single-flight complexity for no benefit. Rejected.

### Option 3 — Monitor thread polling `psutil` process stats

- **Pros:** Cross-platform; no signal handlers; no `tracemalloc` quirks.
- **Cons:** Polling latency is inherent — by the time the monitor sees a
  breach and cancels the asyncio task, the strategy has already burned
  the budget plus a poll interval. Cancelling an asyncio task does not
  preempt a non-`await`ing body (the same gap as Option 1). `psutil`
  adds a heavy dependency and is not on the sandbox allowlist, so it
  would have to be host-side only. Rejected.

### Option 4 — SIGALRM + tracemalloc + single-flight lock, `SandboxResourceError(BaseException)` (chosen)

- **Pros:** Kernel-level CPU preemption on the next bytecode boundary;
  structured Python-visible memory detection before the kernel
  `MemoryError`; defeat-resistant via `BaseException`; correct handling
  of process-global state; graceful degradation on unsupported
  runtimes; no new dependencies (all stdlib).
- **Cons:** `SIGALRM` requires POSIX main thread; `tracemalloc` is a
  Python-allocation soft-cap only (native allocations are the kernel
  backstop's job); single-flight lock serialises guarded regions
  interpreter-wide; not a security boundary — Layer 5 is. All stated
  plainly in the module docstring and in the Consequences above.

## Links

- Single-flight lock + initial implementation: gh#1539
  (`fix(sandbox): add single-flight lock to resource limits`)
- `BaseException` inheritance (defeat-resistance fix): gh#1545
  (`fix(sandbox): inherit SandboxResourceError from BaseException`)
- Source:
  [`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py)
  (`resource_limits`, `ResourceLimits`, `SandboxResourceError`,
  `_guard_lock`, `_arm_cpu_guard`, `_setup_memory_guard`,
  `_teardown_guards`)
- Tests:
  [`tests/test_sandbox_resource_limits.py`](../../tests/test_sandbox_resource_limits.py)
  (30 tests covering the public API, CPU guard, memory guard,
  single-flight guard, graceful degradation, and teardown correctness)
- Builds on: [ADR-0007](0007-strategy-sandbox-allowlist-imports.md)
  (runtime import allowlist) and
  [ADR-0011](0011-runtime-introspection-blocking.md) (runtime
  introspection blocking). Both remain Accepted; this ADR is the
  resource-limiting companion ring.
- Related: [`architecture/plugins.md`](../architecture/plugins.md#sandboxing)
  (threat model and the layer table) and
  [`known-limitations.md`](../known-limitations.md) (integration status
  caveat).
