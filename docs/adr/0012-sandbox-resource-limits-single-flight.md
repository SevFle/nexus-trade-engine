# ADR-0012: Sandbox resource limits (SIGALRM + tracemalloc + single-flight)

- **Status**: Accepted
- **Date**: 2026-07-15
- **Deciders**: Lead maintainer + security reviewer
- **Tags**: security, plugins, sandbox

## Context and Problem Statement

[ADR-0007](0007-strategy-sandbox-allowlist-imports.md),
[ADR-0010](0010-static-ast-validation-toctou-loading.md), and
[ADR-0011](0011-runtime-introspection-blocking.md) closed the *import*,
*code-exec*, *loader-TOCTOU*, and *introspection* escape classes inside
the layered strategy sandbox. None of them bound **resource use**. A
strategy that legitimately imports only allowlisted modules can still:

1. **Spin in a tight compute loop** that never `await`s — the asyncio
   `wait_for` wall-clock timeout can only fire when the event loop
   regains control, so a CPU-bound hot loop hangs the engine
   indefinitely.
2. **Allocate unbounded memory** until the process OOMs and the kernel
   kills it — taking the engine, every other strategy evaluation in
   flight, and every WebSocket client down with it.

The host sandbox wraps every `evaluate()` / `on_bar()` call in an
`asyncio.wait_for(...)` timeout, but that timeout only fires on loop
re-entry. It is **not** a CPU guard. Without a real resource limit,
"install a strategy" is a self-DoS primitive.

The additional wrinkle — and the thing that drove this ADR rather than
a one-line patch — is that every plausible in-process mechanism for
enforcing these limits relies on **process-global state**:

- `signal.SIGALRM` is the only POSIX way to preempt a non-`await`ing
  Python hot loop, and there is exactly **one** `SIGALRM` handler slot
  per interpreter.
- `tracemalloc` has exactly **one** peak-counter per interpreter
  (`tracemalloc.reset_peak()` resets it globally).

Two overlapping resource-limit regions would clobber each other's
teardown — the inner exit restores the outer's handler and stops the
outer's tracemalloc session mid-flight. So whatever mechanism we pick
has to either reject re-entrancy outright or coordinate entry.

This ADR records the **Layer-3** design in
[`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py):
what guards we ship, why those specific primitives, why
`SandboxResourceError` inherits from `BaseException`, and why a
module-level non-reentrant `threading.Lock` serialises entry rather
than relying on the host's `_eval_lock` alone.

It is **not** an ADR claiming a security boundary. The threat-model
note in [`architecture/plugins.md`](../architecture/plugins.md#sandboxing)
is explicit: layers 0–4 are in-process best-effort; **only layer 5
(process / container isolation) is a real security boundary**. This
ADR narrows the accidental-blow-up surface and the casual-misuse
surface — it does not narrow the determined-attacker surface.

## Decision Drivers

- **Preempt non-`await`ing hot loops.** The asyncio timeout cannot, by
  construction, do this. The guard must fire on the next bytecode
  boundary whether or not the loop is in control.
- **No new attack surface.** Whatever module we add runs in the same
  address space as the sandboxed strategy. It must not become a
  treasure map of dangerous references (mirrors the discipline
  ADR-0011 imposed on the introspection guard).
- **Defeat the canonical `except Exception: pass` swallower.** A
  resource violation is a hard kill, not a recoverable error — if a
  strategy can swallow it with the bog-standard `except Exception`
  clause, the guard is cosmetic.
- **Don't break legitimate native allocators.** NumPy buffers, pandas
  block storage, and `mmap` allocations are not visible to Python's
  allocation tracer. The guard must not pretend otherwise.
- **Honest labelling.** The threat-model note in `plugins.md` already
  says layers 0–4 are best-effort; the resource-limits layer must
  inherit that label rather than oversell itself.

## Considered Options

1. **`asyncio.wait_for` only — ship no Layer-3 guard.** Document that
   CPU-bound strategies can hang the engine until layer 5 lands.
2. **`resource.setrlimit(RLIMIT_CPU, …)` and `RLIMIT_AS` only.** Pure
   kernel limits, no Python-visible structured error.
3. **`threading.Timer` raising in a background thread.** Fire a timer
   thread that raises `SandboxResourceError` into the strategy's frame.
4. **`signal.SIGALRM` (CPU) + `tracemalloc` peak (memory) + a separate
   `RLIMIT_AS` backstop + a single-flight lock, with
   `SandboxResourceError(BaseException)` (chosen).**

## Decision Outcome

Chosen option: **Option 4**, because it is the only combination that
preempts a non-`await`ing hot loop *and* produces a structured,
Python-visible violation *and* degrades gracefully where a primitive
is unavailable, while honestly labelling its own gaps. Five mechanisms
ship together in
[`resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py):

1. **`signal.SIGALRM` / `setitimer` for CPU time.** The handler raises
   `SandboxResourceError(kind="cpu")`. Signals are delivered by the
   kernel on the next bytecode boundary, so a hot loop that never
   `await`s is preempted. The prior handler is snapshotted on entry
   and restored on exit (even if the body replaces `signal.SIGALRM`
   itself). Probed via `_can_use_signals()` rather than caught —
   off-main-thread, non-POSIX, or absent-`SIGALRM` platforms skip the
   CPU guard cleanly and fall back to the asyncio wall-clock timeout.
2. **`tracemalloc` peak snapshot for memory.** `reset_peak()` on entry;
   on exit, the observed peak is compared to `max_memory_mb`. A daemon
   thread additionally polls the *current* traced allocation every
   `poll_interval` seconds so a sustained breach is flagged even if
   the post-hoc peak check would somehow miss it. Stated plainly in
   the module docstring: this is a **Python-allocation soft-cap only**
   — native / C-extension heap, `mmap`, file-system page cache, and
   untracked-thread allocations are invisible to tracemalloc.
3. **`RLIMIT_AS` kernel backstop** — installed by the **host sandbox**
   (not this module) — covers the whole process address space and is
   what actually aborts an over-the-limit native allocation. The
   tracemalloc trip-wire fires *before* the harder `MemoryError` so
   callers see a structured `SandboxResourceError(kind="memory")`
   rather than a raw `MemoryError` from `malloc`.
4. **`SandboxResourceError(BaseException)`** — *not* `Exception`. This
   is the single most important defeat-resistance invariant of Layer 3:
   a strategy that wraps its hot loop in `except Exception: pass`
   cannot swallow the `SIGALRM`-raised violation, because the generic
   `except Exception` clause does not match a `BaseException`
   subclass. Layer-3 introspection blocks (restricted importer denying
   `engine.*`, frame filters stripping host frames) provide additional
   defence-in-depth so a sandboxed strategy cannot name the type in an
   `except` clause — but the `BaseException` base is the *first* line
   of defence. The class docstring flags this as load-bearing.
5. **Module-level non-reentrant `threading.Lock` (`_guard_lock`).**
   `resource_limits(...)` acquires it with `blocking=False` on entry;
   failure means another call is already mid-flight (re-entrant from
   this thread or concurrent from another) and raises
   `SandboxResourceError(kind="single_flight")` rather than
   deadlocking (same thread, plain `Lock`) or silently racing (another
   thread). A non-reentrant `Lock` is deliberate — an `RLock` would
   let the same thread re-enter silently and paper over the very
   corruption the guard exists to catch.

### Consequences

- **Positive** — a non-`await`ing CPU-bound strategy is preempted on
  the next bytecode boundary, not when (if) the loop re-enters.
- **Positive** — a Python-allocation blow-up raises a structured
  `SandboxResourceError(kind="memory", limit=…, actual=…)` *before*
  the harder `RLIMIT_AS` `MemoryError`, so dashboards can tell a CPU
  blow-up from a memory blow-up.
- **Positive** — the canonical `except Exception: pass` defeat fails
  on its own; the violation propagates past ordinary error-handling
  middleware.
- **Positive** — graceful degradation: an off-main-thread call, a
  non-POSIX platform, or a `PYTHONMALLOC`-broken tracemalloc falls
  back to "no Layer-3 guard, asyncio timeout still in effect" rather
  than crashing.
- **Negative — and stated plainly — this is still not a security
  boundary.** A determined attacker with native allocations available
  can blow past the tracemalloc cap; the `RLIMIT_AS` backstop aborts
  the *process*, which is a denial-of-service, not a containment.
  Layer 5 is the production target; this layer narrows the
  accidental-blow-up surface only.
- **Negative** — `resource_limits(...)` is **not re-entrant**. Callers
  must serialise guarded regions; the host sandbox's `_eval_lock`
  already does this for evaluations, but any ad-hoc caller (tests,
  tooling) must do it themselves or accept the
  `single_flight` violation.
- **Negative** — `SIGALRM` is main-thread-of-main-interpreter only.
  Worker-thread evaluations fall back to the asyncio timeout, which
  does not preempt a non-`await`ing loop. The host sandbox schedules
  evaluations on the main thread today; if that ever changes, the
  guard silently degrades and the limitation must be re-documented.

## Details

### Why SIGALRM over `RLIMIT_CPU` / a timer thread

`RLIMIT_CPU` is a kernel CPU-second budget, but it's measured in
*seconds of CPU time*, not wall-clock — and it `SIGKILL`s the process
on exhaustion (no Python-visible error, no structured cleanup). A
`threading.Timer` raised in a background thread cannot inject an
exception into the strategy's frame (Python offers no
`PyThreadState_SetAsyncExc`-equivalent that's safe under the GIL for
a tight loop). `signal.SIGALRM` is the only primitive that delivers
a synchronous, Python-visible exception on the next bytecode
boundary — that's why it's the chosen CPU mechanism despite its
main-thread constraint.

### Why `tracemalloc` at all if it misses native allocations

Because the structured `SandboxResourceError(kind="memory")` is what
operators and dashboards want — the `RLIMIT_AS` `MemoryError` is a
hard process abort with no metadata. The tracemalloc trip-wire fires
*before* that and carries `limit` / `actual` MiB. It's a soft-cap,
honestly labelled; `RLIMIT_AS` is the hard cap. The two-layer model
mirrors how the asyncio timeout (soft) and `SIGALRM` (hard) layer on
the CPU side.

### Why a module-level lock and not just the host `_eval_lock`

The host sandbox already serialises evaluations via an asyncio
`_eval_lock`. That's the **production** coordination. The module-level
`_guard_lock` exists as **defence-in-depth for any caller that
invokes `resource_limits(...)` directly** — tests, ad-hoc tooling, a
future executor that doesn't go through the host sandbox. Without it,
those callers would silently corrupt each other's teardown on the
first concurrent use. The `single_flight` violation is a loud failure
in a place where the silent alternative is process-global state
corruption.

### What the guard does *not* cover

- **Native / C-extension heap.** NumPy, pandas, extension modules,
  `mmap` — invisible to tracemalloc. Bounded only by `RLIMIT_AS`
  aborting the process on exhaustion.
- **File-system page cache.** A strategy that reads a terabyte off
  disk doesn't trip tracemalloc; only the explicit Python-level
  buffers it materialises are visible.
- **Untracked threads.** A strategy that spawns a thread (if the
  import allowlist were ever to permit it — it doesn't today) would
  escape the tracemalloc accounting for allocations on that thread.
- **`asyncio`-side hangs.** A strategy that `await`s an `asyncio.sleep`
  in a tight loop defeats `SIGALRM` only in the sense that it yields
  back to the loop — but in that case the asyncio wall-clock timeout
  fires, which is the *correct* layer to handle it.

## Pros and Cons of the Options

### Option 1 — `asyncio.wait_for` only

- **Pros:** Simplest; no signal / tracemalloc state; no serialisation
  lock; honest about the gap.
- **Cons:** A CPU-bound strategy can hang the engine indefinitely. The
  wall-clock timeout is fiction for the very class of bug it most
  needs to catch. Rejected.

### Option 2 — `RLIMIT_CPU` / `RLIMIT_AS` only

- **Pros:** Pure kernel limits; no Python-side monkeypatching; no
  process-global state to coordinate.
- **Cons:** `RLIMIT_CPU` measures CPU time (not wall-clock) and
  `SIGKILL`s on exhaustion — no structured error, no cleanup, the
  whole engine dies. `RLIMIT_AS` covers native allocators but
  produces an opaque `MemoryError`, not a structured violation, and
  it kills the process too. Neither gives dashboards the metadata
  they need to tell CPU from memory blow-ups. `RLIMIT_AS` **is**
  retained as the backstop — just not as the only mechanism.

### Option 3 — `threading.Timer` raising in a background thread

- **Pros:** No signal-handler slot consumed; works on any thread.
- **Cons:** Python has no safe way to inject an exception into a
  running frame from another thread under the GIL — the available
  primitives (`PyThreadState_SetAsyncExc`) are explicitly unsafe for
  tight loops and have been the source of CPython crashes
  historically. Rejected on correctness grounds.

### Option 4 — SIGALRM + tracemalloc + `RLIMIT_AS` + single-flight + `BaseException` (chosen)

- **Pros:** Preempts non-`await`ing loops; structured, Python-visible
  violations; degrades gracefully where primitives are unavailable;
  `BaseException` base defeats `except Exception: pass`; single-flight
  lock prevents process-global state corruption; honest labelling in
  the module docstring and the threat-model note.
- **Cons:** `SIGALRM` is main-thread-only; tracemalloc misses native
  allocations; not re-entrant; still not a security boundary.

## Links

- Sandbox resource limits (SIGALRM + tracemalloc + single-flight): gh#1539
- `SandboxResourceError` inherits from `BaseException`: gh#1545
- Source:
  [`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py)
  (`resource_limits`, `ResourceLimits`, `SandboxResourceError`,
  `_guard_lock`, `_arm_cpu_guard`, `_setup_memory_guard`,
  `_finalize_memory`)
- Tests:
  [`tests/test_sandbox_resource_limits.py`](../../tests/test_sandbox_resource_limits.py),
  [`tests/test_sandbox_single_flight.py`](../../tests/test_sandbox_single_flight.py)
- Builds on: [ADR-0007](0007-strategy-sandbox-allowlist-imports.md)
  (runtime import allowlist) and
  [ADR-0010](0010-static-ast-validation-toctou-loading.md) (static AST
  validation + TOCTOU-safe loading) and
  [ADR-0011](0011-runtime-introspection-blocking.md) (runtime
  introspection blocking). All remain Accepted; this ADR is the
  resource-facing companion ring.
- Related: [`architecture/plugins.md`](../architecture/plugins.md#sandboxing)
  (threat model and the layer table)
