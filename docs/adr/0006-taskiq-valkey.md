# ADR-0006: TaskIQ + Valkey for background work

**Status:** Accepted
**Date:** 2026-04-18
**Refines:** [ADR-0001](0001-scaffold-tech-choices.md) (which
mentioned TaskIQ without justifying the choice).

## Context

The engine has two classes of work that can't block the request
loop:

- **Long-running computations** — backtests (seconds to minutes),
  parameter sweeps, multi-jurisdiction tax exports.
- **Scheduled jobs** — daily legal-doc sync, retention cleanup,
  provider health probes.

We evaluated four candidates: Celery, RQ, Dramatiq, TaskIQ.

## Decision

Use **TaskIQ** with `taskiq-redis` broker backed by Valkey (the
Redis fork we already use as a cache).

The broker choice is deliberate — Valkey speaks Redis protocol,
so any Redis-compatible client (including Celery's) works against
it. We pick TaskIQ over Celery because:

| Concern             | Celery                | TaskIQ                  |
|---------------------|-----------------------|-------------------------|
| Async-native        | bolted on (gevent)    | yes (`async def` first) |
| Broker support      | Redis / RabbitMQ / SQS | Redis / many via plugins |
| Result backend      | required for chord    | optional                |
| Cold-start latency  | high (warm worker pool) | low                   |
| Type-checker story  | weak (decorators)     | works with basedpyright |
| Footprint           | heavy (kombu + amqp)  | light                   |

The killer reason was **async-native**. The engine is async
top-to-bottom — Celery's sync-first worker would force `asyncio.run()`
wrappers at every task boundary, which loses traceback fidelity
and adds 5–10 ms per task. TaskIQ calls our async functions
directly.

## Consequences

**Positive**
- Tasks are `async def` functions; no marshalling friction.
- One broker (Valkey) doubles as cache + task queue + rate-limit
  store. One fewer moving part to operate.
- TaskIQ's scheduler plugin handles cron-shaped jobs without a
  separate process.

**Negative**
- Smaller community than Celery. Some Celery-built operational
  tooling (Flower, Celery Insight) does not work; we use
  `taskiq monitor` and Prometheus metrics from
  [`engine/observability/taskiq_middleware.py`](../../engine/observability/taskiq_middleware.py).
- Valkey is a SPOF. Broker outage stalls every queued task. Plan
  a sentinel or cluster deploy if you care about > 99.9 % task
  pipeline availability (see
  [SLOs](../operations/slos.md)).
- No first-class task chaining (chord / group primitives are
  weaker than Celery's). The pattern we use today is to enqueue
  the follow-up task at the end of the predecessor; works fine
  but loses automatic retry-of-the-chain.

## Wire-up

The broker is configured in
[`engine/tasks/worker.py`](../../engine/tasks/worker.py):

```python
broker = RedisIQBroker(url=settings.valkey_url)
```

Tasks are decorated with `@broker.task`. The FastAPI app uses
`taskiq-fastapi` to inject the broker into the request scope so
handlers can call `await my_task.kick(...)`.

## Alternatives we rejected

- **Celery** — sync worker model. The async wrapping cost is the
  real issue; Celery's plugin ecosystem (Flower etc.) is nice
  but not load-bearing for us.
- **Dramatiq** — better than Celery on async, still not async-
  native. Same fork-out concern.
- **RQ** — sync only, simpler feature set, no scheduler.
- **In-process asyncio tasks** — works for short jobs but loses
  durability across process restarts. We use this for
  `_run_backtest_background` in the `/backtest/run` handler as a
  stopgap; the production path is TaskIQ.
