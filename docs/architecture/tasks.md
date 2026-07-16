# Async task pipeline (TaskIQ)

> **Code:** [`engine/tasks/`](../../engine/tasks/) ·
> **Broker:** Valkey 8 (Redis-compatible) ·
> **Library:** TaskIQ (`taskiq` + `taskiq_redis`)

The async pipeline exists to move long-running work — full backtests,
strategy evaluation, future scheduled rebalances — *off* the request
thread and onto a separately-scaled worker fleet. This page documents
the broker, the tasks that are registered on it today, and the one
non-obvious gotcha: **the public HTTP backtest route does not use this
pipeline yet**. Read the last section before assuming a submitted
backtest went through the queue.

## Why TaskIQ

Decided in [ADR 0004](../adr/0004-task-queue-taskiq.md). Short version:
we needed an async-native (no thread pool, no GIL fights), broker-backed
queue that shares the already-required Valkey instance rather than
introducing Celery + RabbitMQ. TaskIQ gives us typed `async def` tasks,
a Redis/Valkey result backend, and middleware hooks we reuse for
[correlation-ID propagation](../observability/logging.md#correlation-chain)
into worker logs.

## The broker

A **single shared** `ListQueueBroker` is constructed once in
[`engine/tasks/broker.py`](../../engine/tasks/broker.py) and imported
everywhere tasks are defined:

- **URL derivation**: the broker URL is `settings.valkey_url` with the
  scheme rewritten `valkey:// → redis://` (and `valkeys:// → rediss://`).
  `taskiq_redis` speaks the Redis wire protocol (RESP), which Valkey
  implements, so the two are interchangeable at the protocol level —
  only the scheme token differs. Any *other* scheme raises `ValueError`
  (logged `fatal` with credentials stripped, then re-raised) so a
  misconfigured URL fails loudly at construction, not silently at first
  enqueue. The same Valkey instance backs caching, rate-limiting, and
  the [event bus](overview.md), so one URL drives every subsystem.
- **Result backend**: `RedisAsyncResultBackend` on the same URL, so a
  task's return value (or error) is retrievable by `task_id` after the
  worker finishes. This is what `collect_backtest_result` polls.
- **Middleware**: [`CorrelationMiddleware`](../../engine/observability/taskiq_middleware.py)
  copies the bound `correlation_id` into `message.labels` on `pre_send`
  and restores it on `pre_execute`, so a task's log records share the id
  of the HTTP request that triggered it.
- **Lifecycle**: importing the module does **not** open a connection.
  The Redis pool is created on `await broker.startup()`, which the
  FastAPI lifespan ([`engine/app.py`](../../engine/app.py)) invokes on
  app boot, and torn down on `broker.shutdown()`. Keeping construction
  in its own module means the web process can lifecycle-manage the
  broker without importing the worker's (heavy) task definitions.

A `TaskiqScheduler` is attached with an empty `sources` list — there
are no cron tasks yet, but it preserves the `engine.tasks.worker`
surface and gives future scheduled tasks a home.

## Registered tasks

| Task | Module | Purpose |
|---|---|---|
| `run_backtest` | [`definitions.py`](../../engine/tasks/definitions.py) | Execute a full backtest end-to-end and return its metrics/equity payload. The unit of backtest work. |
| `run_strategy_evaluation` | `definitions.py` | Evaluate a strategy against a market/portfolio/cost snapshot and return its signals. Lighter than `run_backtest` — no historical loop. |
| `submit_backtest_job` | `definitions.py` | **Fire** half of the fire/collect pair (see below). |
| `collect_backtest_result` | `definitions.py` | **Collect** half — polls the result backend for a submitted job. |
| `run_backtest_task` | [`worker.py`](../../engine/tasks/worker.py) | Legacy equivalent of `run_backtest`. Kept for back-compat with the deprecated `engine.tasks` facade; prefer `run_backtest` in new code. |

`run_backtest_task` and `run_backtest` overlap on purpose during the
migration; do not assume one is wired where the other isn't — check the
caller.

## The fire/collect pattern

A backtest can run for tens of seconds to minutes. Rather than block the
caller for the whole duration, `submit_backtest_job` kicks the work onto
the broker and returns immediately with a `task_id`; the caller then
polls `collect_backtest_result` with that id:

```
caller                 submit_backtest_job            run_backtest (worker)        collect_backtest_result
 │  ──(args)──────────────▶│                              │                              │
 │                         │ validate inputs (fail fast)  │                              │
 │                         │ await run_backtest.kiq(...)  │                              │
 │  ◀──{status:"submitted", task_id}───│                  │                              │
 │                         │                              │  ◀──(dequeued)──────────────│
 │                         │                              │  run backtest, store result  │
 │  ──(task_id)────────────────────────────────────────────────────────────────────▶│
 │                         │                              │  rebuild handle, is_ready()? │
 │  ◀──{status:"pending"} (not ready) ──────────────────────────────────────────────│
 │  ──(task_id)────────────────────────────────────────────────────────────────────▶│
 │  ◀──{status:"completed", result, execution_time}─────────────────────────────────│
```

Both halves are defensive by design — every exit path returns a
JSON-serialisable dict so the caller never receives a raw exception:

- **`submit_backtest_job`** validates inputs *before* enqueue (bad
  `symbol`/dates/`initial_capital` are rejected synchronously with
  `status:"failed"`), then `run_backtest.kiq(...)`. A broker that
  accepts the enqueue but returns **no `task_id`** is surfaced as
  `failed` (an id you can never poll is worse than an error), as is any
  enqueue exception.
- **`collect_backtest_result`** consults the result backend:
  *not ready* → `status:"pending"` (poll again); *ready + success* →
  `status:"completed"` with the full `run_backtest` payload under
  `result` plus `execution_time`; *ready + error* → `status:"failed"`.
  The worker-side error is a `BaseException` (not JSON-serialisable), so
  it is stringified via `repr()` before it can reach the result backend.

Every envelope carries the `correlation_id`, so a failed job is
traceable back to the originating request in the logs.

## Input validation & fail-fast

[`_validate_backtest_inputs`](../../engine/tasks/definitions.py) runs at
submit time (and again at the worker boundary). It strips C0/C1 control
characters from free-text fields, rejects empty/oversize values, and
coerces/validates `initial_capital` as a positive number. The intent is
that a caller gets **immediate** feedback on a bad payload rather than
discovering it minutes later from a worker error. The validated values
are what get enqueued, so the worker and the validator see the same
shape.

## Retry policy

[`with_retry`](../../engine/tasks/definitions.py) wraps the
provider-touching inner work with exponential backoff + **full jitter**
(`delay = random(0, base * 2**(attempt-1))`, capped at `max_delay`).
Full jitter is deliberate: when many workers recover from a transient
data-provider outage together, jittered retries avoid a thundering herd
hitting the provider simultaneously.

Defaults: `max_retries=3`, `base_delay=0.2s`, `max_delay=5.0s`, retrying
only `_DEFAULT_RETRYABLE = (ConnectionError, TimeoutError,
asyncio.TimeoutError)`. **Non-retryable** exceptions (e.g.
`ValueError` for an unknown strategy, or a validation failure) propagate
on the first attempt — a permanent mistake should fail fast, not burn
the retry budget. When retries are exhausted, `TaskExecutionError` is
raised with the original exception attached as `__cause__`, so worker
middleware can tell task-level exhaustion apart from unrelated errors.

## Worker lifecycle

`on_worker_startup` / `on_worker_shutdown` (registered as TaskIQ
lifecycle hooks) bind/clear request-scoped observability context so the
first task in a fresh worker process isn't missing its logging context.

The worker process is the same image as the app, launched with a
different entrypoint:

```bash
python -m taskiq worker engine.tasks.worker:broker
```

Concurrency is governed by `NEXUS_WORKER_CONCURRENCY` (default `4`).
Workers are stateless and horizontally scalable; TaskIQ absorbs the
queue during a rolling restart, so a deploy needs no special drain
unless the queue depth is already climbing (see the
[task-pipeline runbook](../operations/runbooks/task-pipeline.md)).

## ⚠️ Not yet wired to the public API

The fire/collect pair and `run_backtest` are landed and unit-tested, but
**no HTTP route calls them today**. The public
[`POST /api/v1/backtest/run`](../api-reference.md#backtest) route runs
its computation as a Starlette `BackgroundTasks` job in the *web*
process, with results held in a process-local dict keyed by
`backtest_id` (1-hour TTL) — **not** on the broker and **not** in the
result backend. Consequences:

- Backtest compute competes for the web process's event loop (a slow
  backtest can tail-latency other requests on that replica).
- Results are lost on process restart and invisible to other replicas.
- `GET /api/v1/backtest/results/{id}` cannot read a result produced via
  `submit_backtest_job` — the two stores are disjoint.

Routing the HTTP backtest endpoint through `submit_backtest_job` /
`collect_backtest_result` (and persisting results to the
`backtest_results` table) is tracked as outstanding work — see
[known-limitations.md](../known-limitations.md). Until then, treat the
task surface as infrastructure that exists and is tested, but is not on
a user-reachable path.
