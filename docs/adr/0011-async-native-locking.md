# ADR-0011: Async-native locking (`asyncio.Lock`, never `threading.Lock`)

- **Status**: Accepted
- **Date**: 2026-07-07
- **Deciders**: Lead maintainer + API reviewer
- **Tags**: concurrency, async, api, websocket

## Context and Problem Statement

The engine is an asyncio application: one uvicorn worker runs one event
loop, and every request, WebSocket, and background task is a coroutine
scheduled on that loop. A mutex in this world has a sharp footgun that is
not obvious from the threading playbook:

A `threading.Lock` (or `threading.RLock`) blocks the **OS thread** that
calls `.acquire()`. In a single-loop asyncio process, the loop *is* that
thread — so a contended `threading.Lock` freezes **every** coroutine on
the loop, not just the one waiting for the lock. Throughput collapses to
"one request at a time", and a deadlock manifests as a total stall with
no traceback (the loop thread is parked in `futex`, not raising).

This is not hypothetical: gh#1235 (`fix(api): replace threading.Lock
with asyncio.Lock`) landed precisely because a `threading.Lock` had been
dropped into an async hot path. The mistake is easy to make — `import
threading; lock = threading.Lock()` looks correct to anyone trained on
sync Python — so we make the *rule* explicit rather than relying on
review to catch the next instance.

## Decision Drivers

- **Never block the event loop.** A mutex must yield control back to the
  loop while it waits, so unrelated coroutines keep making progress.
- **Correctness under real concurrency.** The WebSocket
  `ConnectionManager` is sized for `NEXUS_WS_MAX_CONNECTIONS=5000` and is
  mutated from `register` / `disconnect` / `subscribe` / `broadcast` /
  `close_all` concurrently — compound dict mutations are not atomic even
  under the GIL once an `await` splits them.
- **Import must not require a running loop.** Test harnesses and tooling
  import `engine.*` modules without an event loop, so a lock must not be
  constructed at module top-level (an `asyncio.Lock()` created with no
  running loop raises `RuntimeError` on some interpreters / binding
  variants).
- **Per-process state is the common case.** Live socket objects, the
  in-process `EventBus` singleton, and the per-IP WS auth buckets are all
  per-process. Cross-process coordination already has a home: Valkey.

## Considered Options

1. **`threading.Lock` everywhere** — the sync default.
2. **`asyncio.Lock` per-process + Valkey for anything cross-process**.
3. **No locks — rely on the GIL.**
4. **Per-resource fine-grained locking** (one lock per connection / room).

## Decision Outcome

Chosen option: **Option 2 — `asyncio.Lock` for every in-process mutex,
Valkey for cross-process coordination.** Any code that needs to serialise
coroutine access to shared mutable state uses `asyncio.Lock` (or the
related `asyncio.Semaphore` / `asyncio.Event`). `threading.Lock` /
`multiprocessing.Lock` are **not** used in the async hot path.

### Consequences

- **Positive** — a contended lock yields back to the loop; unrelated
  coroutines (other HTTP requests, other WS connections, the heartbeat
  task) keep running. This is the whole point.
- **Positive** — the rule is mechanical to enforce in review ("does this
  module touch shared state from a coroutine? then `asyncio.Lock`") and
  pairs with the existing `async with lock:` idiom that basedpyright can
  sanity-check.
- **Positive** — lazy lock construction (see Details) keeps `import
  engine.events.bus` loop-free, so unit tests and `python -c` probes
  don't trip a `RuntimeError`.
- **Negative** — `asyncio.Lock` requires a running loop at *use* time,
  so it can't protect genuinely synchronous code or be acquired at import
  time. We accept this because **every engine hot path is async** — sync
  code paths are explicitly a non-goal (see `known-limitations.md`).
- **Negative** — `asyncio.Lock` is per-process only. For state that must
  be consistent *across* replicas we already rely on Valkey (the rate
  limiter's `ValkeyBucketBackend`, the `EventBus` pub/sub bridge), so this
  is not a new limitation — it just means `asyncio.Lock` is the wrong
  tool for cross-replica problems.
- **Neutral** — the acquire site must use `async with lock:` and the
  guard method must be `async def`. Forgetting either compiles but does
  the wrong thing silently (`with lock:` on an `asyncio.Lock` returns a
  coroutine and never actually acquires). Review + type-checking catch
  this; there is no runtime assertion today.

## Details

### Three live call sites

| Site | Source | What it guards |
|---|---|---|
| Process-wide `EventBus` singleton | [`engine/events/bus.py:get_event_bus`](../../engine/events/bus.py) | The lazy "build + connect the bus exactly once" path. Double-checked locking: a fast `if _state.bus is not None` return avoids the lock on the hot path; the re-check inside `async with lock:` closes the race where two tasks both saw `None`. |
| WebSocket connection registry | [`engine/api/ws/connection_manager.py:ConnectionManager._lock`](../../engine/api/ws/connection_manager.py) | The `_connections` / `_rooms` / `_seq_counters` dicts and the `_shutting_down` flag, mutated from `register`, `disconnect`, `subscribe`, `broadcast`, `close_all`. Every mutation runs under one `asyncio.Lock` — coarse by design (see Option 4). |
| Per-IP WS auth rate limiter | [`engine/api/ws/auth.py:AuthRateLimiter._lock`](../../engine/api/ws/auth.py) | The `ip → bucket` map mutated on every auth attempt. |

### Lazy construction (no lock at import time)

`asyncio.Lock()` binds to the running loop at construction. The
`EventBus` singleton therefore does **not** build its lock at module
top-level; it lazy-creates it on first use from inside an async context
([`_get_event_bus_lock`](../../engine/events/bus.py)):

```python
_state.lock = None            # module level — no loop needed to import

def _get_event_bus_lock() -> asyncio.Lock:
    if _state.lock is None:
        _state.lock = asyncio.Lock()   # created lazily, inside an async caller
    return _state.lock
```

`ConnectionManager` and `AuthRateLimiter` build their lock in `__init__`,
which is fine because both are constructed from `lifespan(...)` — an
async context where a loop is guaranteed.

### Where cross-process coordination goes instead

| Concern | Mechanism |
|---|---|
| Global rate-limit buckets (multi-replica) | `ValkeyBucketBackend` ([`engine/api/rate_limit.py`](../../engine/api/rate_limit.py)), enabled by `NEXUS_RATE_LIMIT_VALKEY_ENABLED=true` |
| Cross-replica WebSocket event delivery | `EventBus` → Valkey pub/sub → `EventBusBridge` on every replica (ADR-0009) |
| MCP per-principal rate limiting | in-memory only today (single-process stdio transport) — see [`mcp-server.md`](../mcp-server.md#rate-limiting) |

`asyncio.Lock` is deliberately **not** the tool for any of these; it is
correct only for state that lives in a single process.

## Pros and Cons of the Options

### Option 1 — `threading.Lock` everywhere

- **Pros:** Familiar; works in sync code.
- **Cons:** Blocks the loop thread on contention → every other coroutine
  stalls; deadlock presents as a silent hang. This is exactly the bug
  gh#1235 fixed.

### Option 2 — `asyncio.Lock` per-process + Valkey cross-process (chosen)

- **Pros:** Never blocks the loop; per-process correctness for free;
  cross-process problems already have a Valkey path.
- **Cons:** Requires a running loop at use time; per-process only;
  acquire site must remember `async with` / `async def`.

### Option 3 — No locks (rely on the GIL)

- **Pros:** Nothing to acquire.
- **Cons:** The GIL makes individual bytecode ops atomic, not
  *compound* operations across an `await`. `register()` does a
  capacity check, an insert into `_connections`, and a join into
  `_rooms`; two coroutines interleaving between those steps corrupt the
  dicts. Not viable past one connection.

### Option 4 — Fine-grained per-resource locking

- **Pros:** Maximises concurrency within the manager.
- **Cons:** Over-engineering at current scale — the registry mutations
  are fast and the lock is held for microseconds, not across I/O. One
  coarse `asyncio.Lock` is simpler to reason about and deadlocks are
  easier to avoid. Revisit only if profiling shows contention.

## Links

- Trigger fix: `dfaa318 fix(api): replace threading.Lock with asyncio.Lock` (gh#1235)
- Connection-lifecycle hardening: `7feec33 security(ws): add token auth and fix connection lifecycle` (gh#1271)
- Source: [`engine/events/bus.py`](../../engine/events/bus.py),
  [`engine/api/ws/connection_manager.py`](../../engine/api/ws/connection_manager.py),
  [`engine/api/ws/auth.py`](../../engine/api/ws/auth.py)
- Related: [ADR-0009](0009-cross-replica-eventbus-bridge.md) (cross-replica
  state lives in Valkey, not in an in-process lock)
