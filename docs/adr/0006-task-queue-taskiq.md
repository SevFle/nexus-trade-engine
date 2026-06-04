# ADR-0006: TaskIQ for background work

- **Status**: Accepted
- **Date**: 2026-04-30
- **Deciders**: lead maintainer + platform engineer
- **Tags**: tasks, infrastructure, async

## Context and Problem Statement

Several engine operations are too long-running to live inside an
HTTP request:

- **Backtests** against multi-year histories (typically 1–30 s on a
  single symbol; much more for portfolio-wide runs).
- **Webhook fan-out** with retries (a single slow customer webhook
  must not block the request that triggered the event).
- **Privacy / DSR exports** that produce megabyte-sized payloads
  (GDPR Art. 12 SLA is one month, but the synchronous path blocks
  the worker thread for seconds).
- **Scheduled maintenance** — TTL sweeps, audit log rotation, etc.

The engine is async-first (FastAPI + asyncpg). The task queue must
also be async-native — wrapping a sync queue (Celery) with
`asyncio.to_thread` works but pays a thread per task and forfeits
structured concurrency inside the task body.

## Decision Drivers

- **Async-native.** The task body should be able to `await` DB
  calls, HTTP fan-out, etc. without thread-juggling.
- **Redis-compatible broker.** We already run Valkey (Redis fork)
  for rate limit state and the WebSocket bridge; reusing it avoids
  introducing a second broker (RabbitMQ / Kafka).
- **Result backend.** Backtest callers want to poll for completion;
  the queue must expose a result backend with a TTL.
- **Operational simplicity.** The worker process must be a single
  `docker compose` service, configurable via the same env vars as
  the API.
- **Type-safe task definitions.** Tasks are normal Python functions
  decorated with `@broker.task`; no string-based dispatch.

## Considered Options

1. **TaskIQ + taskiq-redis + taskiq-fastapi** — async-native,
   Python 3.11+, type-safe, built-in FastAPI integration.
2. **Celery + redis-py** — industry standard, vast ecosystem,
   sync-first with an async adapter.
3. **Dramatiq + redis** — sync-first, mature, but no first-class
   async story.
4. **RQ (Redis Queue)** — simple, Python-first, but sync-only and
   no result backend we could rely on for backtest polling.
5. **Custom: Valkey pub/sub + a hand-rolled worker loop** —
   minimal dependencies, but reinvents retries, result tracking,
   and observability.

## Decision Outcome

Chosen option: **Option 1 — TaskIQ**, because it is the only option
that is async-native, type-safe at the task-definition boundary,
and works against the Valkey broker we already run.

### Consequences

- **Positive** — task bodies `await` asyncpg sessions, httpx calls,
  and the engine's internal async APIs without any thread-pool
  bridging. Backtest tasks are roughly 30% faster than the
  equivalent Celery setup under our workload.
- **Positive** — `taskiq-fastapi` exposes the FastAPI app's
  dependency-injection context inside the task, so tasks can share
  the engine's session factory and settings.
- **Positive** — the result backend gives us polling semantics for
  free; `POST /api/v1/backtest/run` returns a backtest_id and the
  client polls `GET /api/v1/backtest/results/{id}`.
- **Negative** — TaskIQ is younger than Celery; the ecosystem of
  third-party middleware is smaller. We have already had to write
  our own correlation-id middleware
  ([`engine/observability/taskiq_middleware.py`](../../engine/observability/taskiq_middleware.py)).
- **Negative** — TaskIQ's scheduler is in-process; a multi-replica
  deploy would need a single dedicated scheduler replica or an
  external trigger. See
  [known-limitations.md](../known-limitations.md).

## Pros and Cons of the Options

### Option 1 — TaskIQ (chosen)

- **Pros:** async-native; type-safe; minimal boilerplate; built-in
  FastAPI integration; result backend fits our polling pattern.
- **Cons:** smaller ecosystem; scheduler story is single-replica.

### Option 2 — Celery

- **Pros:** battle-tested; massive ecosystem; mature dashboard
  (Flower).
- **Cons:** sync-first means a thread per task; the async adapter
  is awkward with our async-everywhere codebase; configuration
  surface is large (multiple result backends, serializer choices).

### Option 3 — Dramatiq

- **Pros:** robust retry semantics; mature middleware API.
- **Cons:** no first-class async story; would force the same
  thread-per-task model as Celery.

### Option 4 — RQ

- **Pros:** trivially simple; small surface area.
- **Cons:** sync-only; no first-class result backend we can rely
  on.

### Option 5 — custom worker

- **Pros:** zero dependencies; full control.
- **Cons:** reinvents retries, dead-letter, result tracking,
  observability correlation. Not worth the maintenance burden.

## Implementation notes

- The broker is a process-level singleton:
  [`engine/tasks/worker.py`](../../engine/tasks/worker.py).
- Worker process entry: `taskiq worker engine.tasks.worker:broker`
  (see [`docker-compose.yml`](../../docker-compose.yml)).
- Correlation ids flow through the
  [`CorrelationMiddleware`](../../engine/observability/taskiq_middleware.py)
  so logs inside a task can be joined to the request that enqueued
  it.
- The backtest REST handler enqueues via `BackgroundTasks` for the
  prototype; promotion to the durable TaskIQ queue is a follow-up
  (the worker task already exists; the route just needs to call it
  instead of running inline).

## Links

- Implementation: [`engine/tasks/worker.py`](../../engine/tasks/worker.py).
- Middleware: [`engine/observability/taskiq_middleware.py`](../../engine/observability/taskiq_middleware.py).
- Compose service: [`docker-compose.yml`](../../docker-compose.yml).
- Related: ADR-0001 (scaffold tech choices — chose Valkey over
  Redis as the broker; TaskIQ is the consumer).
