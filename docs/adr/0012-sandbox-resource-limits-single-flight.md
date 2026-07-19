# ADR-0012: Sandbox resource limits — SIGALRM + tracemalloc + single-flight lock

- **Status**: Accepted
- **Date**: 2026-07-17
- **Deciders**: Lead maintainer + security reviewer
- **Tags**: security, plugins, sandbox

## Context and Problem Statement

[ADR-0007](0007-strategy-sandbox-allowlist-imports.md) closed the static
`import` surface, [ADR-0010](0010-static-ast-validation-toctou-loading.md)
closed dynamic-import / code-execution builtins and the loader TOCTOU
window, and [ADR-0011](0011-runtime-introspection-blocking.md) closed the
CPython object-graph escape chain. None of those, however, address a
strategy that is **syntactically benign but unbounded in cost** — a tight
`while True:` loop, a `numpy.zeros((10**9,))` allocation, or an
accidentally-exponential recursion. Such a strategy never *imports* a
forbidden module and never *traverses* a dunder; it just consumes the
host process until the OOM killer or a deploy health-check tears it down.

The host sandbox already wraps every `evaluate()` / `on_bar()` call in
an `asyncio.wait_for(...)` wall-clock timeout, but that timeout **can
only fire when the event loop regains control**. A strategy that spins
in pure-Python compute (`while True: pass`) — or in a C extension that
does not release the GIL — never `await`s, so the asyncio scheduler
never gets to run the timeout callback. From the perspective of an
operator looking at a stuck worker, the asyncio timeout is invisible.

This ADR records the decisions behind the Layer-3 resource-limits
module — [`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py)
(gh#1539, gh#1545) — because the mechanism rests on three non-obvious
invariants that are easy to break in a future refactor:

1. the **CPU guard uses `signal.SIGALRM`**, not an asyncio timeout, so
   it fires on the next bytecode boundary;
2. the **memory guard uses `tracemalloc` as a Python-visible soft-cap
   backed by a separate kernel `RLIMIT_AS`**, with a deliberate split
   in responsibility; and
3. the **single-flight `threading.Lock`** is non-reentrant *on purpose*
   and rejects re-entry rather than deadlocking.

All three invariants are pinned by regression tests in
[`tests/test_sandbox_resource_limits.py`](../../tests/test_sandbox_resource_limits.py);
the ADR exists so a future "this lock looks like an `RLock` candidate"
or "let's just catch `Exception` in the sandbox runtime" refactor has
a written record of why those changes are dangerous.

## Decision Drivers

- **Defeat-resistance over ergonomics.** A strategy author who wants to
  exceed a resource budget and *not get caught* will wrap their hot
  loop in `except Exception: pass`. The Layer-3 guards must therefore
  surface violations in a way that the canonical defeat-attempt cannot
  swallow.
- **Preempt pure-compute loops.** The asyncio wall-clock timeout is a
  fallback only; the primary CPU guard must fire without the event
  loop's cooperation.
- **No false sense of security on the memory side.** `tracemalloc`
  cannot see native allocations, and the docstring / this ADR must say
  so plainly. The kernel `RLIMIT_AS` is what actually stops a NumPy
  blow-up; tracemalloc produces a structured, attributable error
  *before* the harder `MemoryError`.
- **Don't add a new escape vector.** The resource-limits module is
  host-side code that imports modules on the sandbox deny-list
  (`signal`, `tracemalloc`, `threading`). Those imports must succeed at
  engine start-up *before* restrictions activate, and the resulting
  module objects must not be reachable from sandboxed code.
- **Fail loudly, never deadlock.** The signal handler and tracemalloc
  peak counter are process-global, so overlapping guarded regions
  would corrupt each other's teardown. Re-entrant entry must raise a
  clear error rather than block.

## Considered Options

1. **`asyncio.wait_for` as the only guard.** No `SIGALRM`, no
   `tracemalloc`, no single-flight lock.
2. **`signal.SIGALRM` CPU + `tracemalloc` memory, no single-flight
   lock.** Trust the host runtime to serialise evaluations.
3. **`signal.SIGALRM` CPU + `tracemalloc` memory + a module-level
   non-reentrant `threading.Lock` that rejects re-entry.** The
   chosen design.
4. **Run every strategy in a subprocess and `kill -9` on timeout.**
   Defer to layer 5 (process / container isolation).

## Decision Outcome

Chosen option: **Option 3 — SIGALRM CPU + tracemalloc memory +
single-flight lock**, because it is the strongest guard we can build
*inside the existing in-process sandbox* without prematurely
introducing the layer-5 subprocess boundary, and it degrades
gracefully on runtimes that cannot support one of the two primitive
guards.

### Consequences

- **Positive** — a strategy that spins in pure Python (`while True:
  pass`) is preempted on the next bytecode boundary by the
  kernel-delivered `SIGALRM`, *independently* of the asyncio loop.
- **Positive** — `SandboxResourceError` inherits directly from
  `BaseException`, so the canonical `except Exception: pass` defeat
  cannot swallow a CPU or memory violation. The single exception type
  carries `kind` (`"cpu"`, `"memory"`, `"single_flight"`), `limit`,
  and `actual`, so dashboards can tell a CPU blow-up from a memory
  blow-up from a programming-error re-entry.
- **Positive** — overlapping guarded regions cannot silently corrupt
  teardown state. Re-entrant entry (same thread) and concurrent entry
  (another thread) both raise `SandboxResourceError(kind="single_flight")`
  with a message that names the process-global resource at fault and
  tells the caller to serialise.
- **Positive** — graceful degradation. Off-main-thread, on Windows, or
  where `signal.SIGALRM` is unavailable, the CPU guard is skipped and
  the asyncio wall-clock timeout remains as the fallback; if
  `tracemalloc` cannot start (e.g. `PYTHONMALLOC` set), the memory
  guard is skipped rather than crashing the sandbox.
- **Negative** — `tracemalloc` is a Python-allocation soft-cap only.
  Native / C-extension allocations (NumPy buffers, pandas block
  storage, `mmap`, file-system page cache) are **not** seen and a
  strategy can blow past `max_memory_mb` *without* tripping the
  tracemalloc layer. The kernel `RLIMIT_AS` backstop installed by
  the host sandbox is what actually aborts such an allocation in
  real time; the tracemalloc layer produces the structured,
  attributable error *before* the harder `MemoryError`. This caveat
  is load-bearing and is repeated in the module docstring, the
  architecture note, and here.
- **Negative** — host-side callers that wish to observe
  `SandboxResourceError` must catch `BaseException` (or the specific
  type); a blanket `except Exception` will *not* see it. This is
  intentional but easy to forget.
- **Negative** — `SIGALRM` only works on the main thread of the main
  interpreter, so any future move to evaluate strategies on a worker
  thread silently loses the CPU guard. The asyncio timeout is the
  fallback; the regression test suite skips the CPU tests when
  `_can_use_signals()` returns `False`.
- **Neutral** — the host sandbox already serialises evaluations via
  its own `_eval_lock`, so in the production deployment the
  module-level `_guard_lock` is defence-in-depth for tests and ad-hoc
  tooling that call `resource_limits(...)` directly.

## Details

### Why `SIGALRM` and not `asyncio.wait_for`

`asyncio.wait_for(coro, timeout)` cancels its inner task by scheduling
a callback on the running loop and raising `CancelledError` into the
coroutine the next time the loop regains control. That is sufficient
for any strategy that `await`s — every `await` is a loop re-entry
point. It is *not* sufficient for:

- a pure-Python compute loop (`while True: x += 1`) that never `await`s;
- a C-extension call that holds the GIL for an unbounded time; or
- a pathological recursion that exhausts the C stack before the
  scheduler gets another turn.

`signal.SIGALRM` (via `signal.setitimer(ITIMER_REAL, ...)`) is
delivered by the kernel on the next bytecode boundary regardless of
whether the loop is running. The installed handler raises
`SandboxResourceError(CPU_RESOURCE, limit=...)`, which unwinds the
Python stack normally. The prior signal handler is snapshotted on
entry (`signal.getsignal(SIGALRM)`) and restored on exit, so a host
caller that already has a SIGALRM handler is not silently clobbered.

Availability is probed up front by `_can_use_signals()`, which returns
`False` off-main-thread or on a platform without `SIGALRM` /
`setitimer`. In that case the CPU guard is a no-op (`timer_armed=False`)
and the asyncio wall-clock timeout remains the backstop.

### Why `SandboxResourceError(BaseException)` and not `(Exception)`

The single most important defeat-resistance invariant in this module.
A strategy that wants to exceed its budget and *not* get caught writes:

```python
try:
    while True:
        ...   # tight compute loop, never awaits
except Exception:
    pass       # swallow the timeout, keep going
```

If `SandboxResourceError` inherited from `Exception` (or
`RuntimeError`), the SIGALRM handler would raise it, the `except
Exception` clause would match, and the strategy would silently keep
running. By inheriting directly from `BaseException`, the violation
sails past `except Exception` and propagates to the host runtime.

This was not the original shape: commit gh#1539 landed the module
with `SandboxResourceError(RuntimeError)`, and gh#1545 *deliberately
re-parented it to* `BaseException` once review caught the
defeat-attempt. The regression test
(`test_sandbox_resource_limits.py::TestPublicAPISurface`) asserts both
`issubclass(SandboxResourceError, BaseException)` and
`not issubclass(SandboxResourceError, Exception)` so the invariant
cannot regress without a failing test.

The introspection controls from [ADR-0011](0011-runtime-introspection-blocking.md)
provide additional defence-in-depth — a sandboxed strategy cannot
import `engine.plugins.sandbox.resource_limits` (the restricted
importer denies `engine.*`), cannot reach the `SandboxResourceError`
symbol via `sys.modules` or host-frame walking (frame filters strip
host frames from raised exceptions), and therefore cannot name the
type in an `except` clause even if it could spell it. The
`BaseException` parent is the *first* line of defence and is what
defeats the canonical `except Exception: pass` on its own.

### Why `tracemalloc` *and* `RLIMIT_AS`

`tracemalloc` traces allocations performed through `PyMem_*` /
`PyObject_*` — i.e. Python objects and buffers created from Python
code. It does **not** see:

- native / C-extension heap allocations (`malloc` / `calloc` — NumPy
  internal buffers, pandas block storage, extension-module working
  memory);
- memory-mapped files (`mmap`), file-system page cache, or kernel
  buffers; or
- allocations made by threads `tracemalloc` was not tracking when the
  call started.

Consequently a strategy that allocates large native buffers can blow
past `max_memory_mb` *without* tripping tracemalloc. The host sandbox
therefore installs `RLIMIT_AS` (via `resource.setrlimit` in
`StrategySandbox._apply_resource_limits`) as a separate
**kernel-level backstop** — that cap covers the whole process address
space and is what actually aborts an over-the-limit allocation in real
time (it raises `MemoryError`).

The split is deliberate:

| Layer | What it sees | When it fires | Failure mode |
|---|---|---|---|
| `tracemalloc` (this module) | Python allocations only | At context exit (peak / sustained breach) | Structured `SandboxResourceError(kind="memory")` with observed MiB |
| `RLIMIT_AS` (host sandbox) | Whole process address space | At the over-limit syscall | `MemoryError` from the allocating call |

The tracemalloc layer is the *best-effort, Python-visible* trip-wire
that produces an attributable error before the harder `MemoryError`.
Anything that reads this ADR or the module docstring and concludes
"the memory cap is enforced" has misread it: only `RLIMIT_AS` enforces,
tracemalloc reports.

Implementation notes:

- `_setup_memory_guard` calls `tracemalloc.reset_peak()` on entry so
  the high-water mark observed is attributable to *this* guarded call,
  not the cumulative process peak.
- A daemon thread (`sandbox-memory-monitor`) polls the *current*
  traced allocation every `poll_interval` seconds and sets a
  `breach_event` if it exceeds the cap; the deterministic exit-time
  peak comparison in `_finalize_memory` is what ultimately raises, so
  a transient spike that subsides before exit is still caught if the
  poller observed it.
- `daemon=True` on the monitor thread guarantees it never outlives the
  process even if `__exit__` is skipped (e.g. during interpreter
  shutdown).
- If `tracemalloc.start()` raises `RuntimeError` (e.g. `PYTHONMALLOC`
  is set), the memory guard is disabled rather than crashing.

### Why a non-reentrant `threading.Lock`

The signal-handler slot (`signal.SIGALRM`) and the tracemalloc peak
counter are **process-global**: there is exactly one of each per
interpreter. Two overlapping `resource_limits(...)` regions would
therefore corrupt each other's teardown — the inner `__exit__` would
restore the outer's signal handler (disarming the outer's timer) and
call `tracemalloc.stop()` on the outer's session mid-flight.

The module-level `_guard_lock` is a plain `threading.Lock` (not an
`RLock`) acquired *non-blockingly* on entry. A `Lock` is chosen over
an `RLock` deliberately: an `RLock` would let the same thread re-enter
silently and paper over the very corruption we are guarding against.
Failure to acquire the lock raises
`SandboxResourceError(SINGLE_FLIGHT_RESOURCE, limit=None)` — reusing
the Layer-3 exception type so callers only need to catch one type
regardless of which guard fired — with a message that names the
process-global resource and tells the caller to serialise.

The lock is released in the `finally` block of `resource_limits`, so
it is always freed even when the body raises `SandboxResourceError`.
The teardown (`_teardown_guards`) runs *before* the lock is released,
so a racing second entry cannot observe a half-torn-down state (e.g.
our SIGALRM handler still installed with its timer already disarmed).

In the production deployment the host sandbox already serialises
evaluations via its asyncio `_eval_lock`; the module-level guard is
defence-in-depth for any caller that invokes `resource_limits(...)`
directly — tests, ad-hoc tooling, a future embedding host.

### Why host-side imports of blocked modules are safe

`signal`, `tracemalloc`, and `threading` are all on the sandbox
deny-list (a sandboxed strategy cannot `import` them). This module is
imported once, at engine start-up, *before* any sandbox restrictions
are activated, so those imports succeed. The captured module objects
are aliased (`_signal`, `_tracemalloc`, `_threading`) and referenced
only via local variables inside the context manager; no module-level
binding is exposed for sandboxed code to discover, and importing
`engine.plugins.sandbox.resource_limits` is itself denied by the
restricted importer (`engine.*` is not on the allowlist). This is the
same pattern the outer `StrategySandbox` uses for `os`, `asyncio`, and
`resource` — see the security note at the top of
[`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py).

## Pros and Cons of the Options

### Option 1 — `asyncio.wait_for` as the only guard

- **Pros:** Simplest; no signal-handler state, no threads, no lock.
- **Cons:** Cannot preempt a pure-compute loop or a GIL-holding C
  call; the asyncio scheduler never runs. Defeat-attempts that simply
  avoid `await` succeed. Unacceptable for the threat model.

### Option 2 — `SIGALRM` + `tracemalloc`, no single-flight lock

- **Pros:** Smaller surface area; no re-entrancy error path to
  document.
- **Cons:** Two overlapping guarded regions silently corrupt each
  other's teardown (the inner `__exit__` disarms the outer's timer
  and stops the outer's tracemalloc session). In the production
  deployment the host `_eval_lock` happens to mask this, but tests
  and ad-hoc callers do not. A latent footgun.

### Option 3 — `SIGALRM` + `tracemalloc` + non-reentrant `Lock` (chosen)

- **Pros:** Preempts pure-compute loops; carries structured
  `kind`/`limit`/`actual` metadata; cannot be swallowed by
  `except Exception: pass`; rejects overlapping regions loudly
  rather than corrupting state; degrades gracefully on unsupported
  runtimes.
- **Cons:** `tracemalloc` is a soft-cap (see above); CPU guard is
  main-thread / POSIX only; callers must catch `BaseException`.

### Option 4 — Subprocess per strategy, `kill -9` on timeout

- **Pros:** Strongest — the host never shares an address space with
  the strategy, so a runaway allocation crashes the child, not the
  engine. Wall-clock timeouts become trivially reliable.
- **Cons:** Premature. Layer 5 (process / container isolation) is the
  stated production target of the sandbox and is not yet built
  ([`architecture/plugins.md`](../architecture/plugins.md#sandboxing)).
  Building it now would duplicate the boundary and the IPC surface
  (`MarketState` in, `Signal[]` out) would have to be redesigned when
  layer 5 lands for real. This ADR is the in-process ring layer 5
  will *replace*, not duplicate.

## Links

- Add single-flight lock to resource limits: gh#1539
- Inherit `SandboxResourceError` from `BaseException` (defeat-resistance):
  gh#1545
- Resolve Prometheus review findings + tests: gh#1553
- Source:
  [`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py)
  (`ResourceLimits`, `SandboxResourceError`, `resource_limits`,
  `_guard_lock`); host-side `RLIMIT_AS` / `RLIMIT_NOFILE` backstop in
  [`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py)
  (`StrategySandbox._apply_resource_limits`,
  `StrategySandbox._restore_resource_limits`)
- Tests:
  [`tests/test_sandbox_resource_limits.py`](../../tests/test_sandbox_resource_limits.py)
  (`TestPublicAPISurface`, `TestCpuGuard`, `TestMemoryGuard`,
  `TestSingleFlightGuard`)
- Builds on: [ADR-0007](0007-strategy-sandbox-allowlist-imports.md)
  (runtime import allowlist),
  [ADR-0010](0010-static-ast-validation-toctou-loading.md) (static AST
  validation + TOCTOU-safe loading), and
  [ADR-0011](0011-runtime-introspection-blocking.md) (runtime
  introspection blocking). All remain Accepted; this ADR is the
  resource-cost-facing companion ring.
- Related: [`architecture/plugins.md`](../architecture/plugins.md#sandboxing)
  (Layer-3 row of the sandbox table); threat-model note that only
  layer 5 is a real security boundary.
