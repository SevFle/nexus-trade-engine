# ADR-0004: Task queue — TaskIQ over Celery / RQ / arq

- **Status**: Accepted
- **Date**: 2026-05-12
- **Deciders**: Lead maintainer + 1 reviewer
- **Tags**: tasks, async, broker, backtest

## Context and Problem Statement

Backtests, scoring sweeps, and retention cleanups all take longer than
a single HTTP request should hold. We need a background task queue so
the API can enqueue work and return `202`, while a worker pulls jobs
off a broker, runs them with the asyncio event loop the rest of the
engine already lives in, and persists results back to Postgres.

The engine is async-first (FastAPI + SQLAlchemy 2 async + asyncpg),
and the broker is shared with the rate limiter / WebSocket pubsub,
which already need a Valkey/Redis-compatible in-memory server. The
queue has to fit that shape: native `async def` workers, small
dependency footprint, and a broker protocol we already speak.

## Decision Drivers

- **Native asyncio.** The backtest runner and scoring executor are
  `async def` end-to-end. Wrapping them in a sync framework means
  thread pools and event-loop thrash — a footgun we'd rather avoid.
- **Small dependency surface.** Celery pulls in a config layer, a
  result-backend shim, beat, signals, and a CLI that all need
  pinning. The fewer moving parts the easier it is to operate in a
  single-node self-hosted deploy.
- **Shared broker with rate limit / pubsub.** Whatever we pick has to
  run happily against the Valkey instance the rest of the engine
  already talks to (see ADR-0005).
- **FastAPI ergonomics.** The API layer wants `taskiq-fastapi`-style
  dependency injection so a route handler can `.kiq()` a task with the
  request-scoped objects it already has. Out-of-the-box FastAPI
  integration matters.
- **Result backend for status polling.** The dashboard polls
  `/api/v1/backtest/{id}` while a job runs. We need first-class async
  result backend support so workers can report progress / failure
  without us hand-rolling a status table.

## Considered Options

1. **TaskIQ** — async-native Python task queue. Broker-pluggable
   (`taskiq-redis`, `taskiq-aio-pika`, …), first-class FastAPI
   integration via `taskiq-fastapi`, supports `async def` tasks
   natively.
2. **Celery** — the industry default. Mature, huge ecosystem, but the
   worker model is sync/threaded (Celery 5 added asyncio support but
   it's still second-class) and the config surface is large.
3. **RQ (Redis Queue)** — small, simple, Pythonic. Sync-only; asyncio
   support lives in third-party forks (`arq`-style) that lag behind.
4. **arq** — asyncio + Redis. Lightweight, but the maintainer cadence
   is slow and there's no FastAPI integration story out of the box.

## Decision Outcome

Chosen option: **Option 1 — TaskIQ**, because it is the only option
that is async-native today, ships a `taskiq-fastapi` shim that drops
into our existing route handlers, and supports `taskiq-redis` against
the same Valkey broker we already depend on. That keeps the
operational surface to one extra worker process (`nexus worker`) and
one extra Python package.

The first workload on the queue is `run_backtest_task` in
[`engine/tasks/worker.py`](../../engine/tasks/worker.py); scoring
sweeps, retention sweeps, and DSR export pipelines will follow the
same shape.

### Consequences

- **Positive**
  - `async def` tasks compose with the rest of the engine — no thread
    bridges, no `asyncio.run` inside a sync entry point.
  - `taskiq-fastapi` lets a route `.kiq()` with a Pydantic payload and
    poll the result backend without us writing glue.
  - One broker (Valkey) serves rate limiting, WebSocket pubsub, and
    the task queue. One backup, one secret, one place to monitor.
- **Negative**
  - TaskIQ's ecosystem is younger than Celery's. Fewer Stack Overflow
    answers; the project is the most likely of the four to ship a
    breaking change in a minor version.
  - Operators who already run Celery for *other* workloads have to
    operate a second queue. We accept this — the alternative is
    wrapping every backtest in a sync adapter.
- **Neutral**
  - We pin to the `taskiq>=0.11` line in `pyproject.toml` and follow
    the project's upgrade guide on every minor bump.

## Pros and Cons of the Options

### Option 1 — TaskIQ (chosen)

- **Pros**
  - Async-native: `@broker.task` decorators work on `async def`
    directly.
  - `taskiq-fastapi` gives a request-scoped startup/shutdown hook
    that wires the broker into the FastAPI app cleanly.
  - Pluggable brokers — we use `taskiq-redis` against Valkey today and
    can swap to NATS / RabbitMQ later without rewriting tasks.
  - Small core; the entire public surface is the `broker` object, the
    task decorator, and the result backend.
- **Cons**
  - Smaller community than Celery. Fewer blog posts, fewer runbooks.
  - The CLI / scheduler story is thinner than Celery Beat; we'll need
    to write our own periodic-task table when cron-style scheduling
    lands.

### Option 2 — Celery

- **Pros**
  - Battle-tested. Almost every Python shop has run it.
  - Celery Beat is a known quantity for periodic tasks.
  - Mature monitoring tooling (Flower, etc.).
- **Cons**
  - Sync-first worker model. Running async backtest code means
    `asyncio.run` per task or a thread pool — neither is great.
  - Larger dependency surface (Kombu transports, result backends,
    Beat, signals).
  - Configuration is ini/yaml-driven and duplicates what's already in
    our pydantic settings.

### Option 3 — RQ

- **Pros**
  - Smallest API surface. Easy to read the source.
  - Sync Python is its native mode and works well there.
- **Cons**
  - Sync-only. Same asyncio gap as Celery without Celery's maturity.
  - No first-class FastAPI integration; we'd write the glue.

### Option 4 — arq

- **Pros**
  - Async-native and Redis-backed — closest to TaskIQ on paper.
  - Small, opinionated, easy to read.
- **Cons**
  - Maintainer cadence has been slow; the project has stalled in the
    past.
  - No FastAPI integration shim; we'd write the request → task
    handoff ourselves.
  - Smaller ecosystem than TaskIQ, which itself is smaller than
    Celery.

## Links

- Related issue: gh#22 (background task runner for backtests).
- Related code: [`engine/tasks/worker.py`](../../engine/tasks/worker.py).
- Supersedes: —
- Superseded by: —
- External references:
  - [TaskIQ docs](https://taskiq-python.github.io/)
  - [taskiq-fastapi](https://github.com/taskiq-python/taskiq-fastapi)
  - [taskiq-redis](https://github.com/taskiq-python/taskiq-redis)
