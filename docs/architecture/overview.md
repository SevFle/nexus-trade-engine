# System overview

Nexus Trade Engine is a Python service that backtests algorithmic
trading strategies, runs them against live or paper broker
connections, and exposes the results via a REST API and a React
frontend. This document describes the moving pieces and how a request
flows through them.

## Components

```
┌──────────────────┐        HTTPS         ┌──────────────────┐
│   React frontend │ ───────────────────▶ │  FastAPI engine  │
│   (frontend/)    │ ◀─────────────────── │  (engine/api)    │
└──────────────────┘    JSON / WebSocket  └────────┬─────────┘
                                                   │
                          enqueue / dispatch       │
                                                   ▼
                                          ┌──────────────────┐
                                          │  TaskIQ workers  │
                                          │  (engine/tasks)  │
                                          └────────┬─────────┘
                                                   │
                                          ┌────────┴─────────┐
                                          ▼                  ▼
                                  ┌────────────┐     ┌──────────────┐
                                  │  Postgres  │     │ Valkey/Redis │
                                  │ (asyncpg + │     │ (TaskIQ      │
                                  │  TimescaleDB)    │  broker, cache)│
                                  └────────────┘     └──────────────┘
```

The full service is one Python package (`engine/`) with sub-packages
that line up with the boxes above. The frontend is a separate Vite +
React app under `frontend/`.

## Top-level layout

| Path                     | Responsibility |
|--------------------------|----------------|
| [`engine/app.py`](../../engine/app.py)            | FastAPI app factory. Wires routers, middleware, lifespan hooks. |
| [`engine/main.py`](../../engine/main.py)          | Entry point used by uvicorn (`--factory engine.app:create_app`). |
| [`engine/config.py`](../../engine/config.py)      | Pydantic settings — every env var the engine reads lives here. |
| [`engine/api/`](../../engine/api/)                | HTTP/WebSocket surface: routers, auth, rate limiting, error mapping. |
| [`engine/core/`](../../engine/core/)              | Domain logic: backtest runner, strategy evaluator, execution primitives. |
| [`engine/data/`](../../engine/data/)              | Market data providers and the registry that picks one at runtime. |
| [`engine/db/`](../../engine/db/)                  | SQLAlchemy models, async session factory, Alembic migrations. |
| [`engine/events/`](../../engine/events/)          | Event bus + outbound webhook dispatcher (gh#80). |
| [`engine/observability/`](../../engine/observability/) | Structlog wiring, lineage middleware, pluggable metrics backend (gh#34). |
| [`engine/plugins/`](../../engine/plugins/)        | Plugin SDK and runtime registry. See [plugins.md](plugins.md). |
| [`engine/tasks/`](../../engine/tasks/)            | TaskIQ worker definitions for async work (backtests, scheduled jobs). |
| [`engine/legal/`](../../engine/legal/)            | Legal-document acceptance (Terms / Privacy / etc.). |
| [`engine/reference/`](../../engine/reference/)    | Static reference data (instruments, exchanges). |
| [`frontend/`](../../frontend/)                    | React dashboard (Vite, React 18, Tailwind, react-query). |

## Key dependencies

| Concern                | Library                     |
|------------------------|-----------------------------|
| Web framework          | FastAPI                     |
| Validation / settings  | Pydantic v2 / Pydantic-Settings |
| Async DB driver        | `asyncpg` via SQLAlchemy 2 async |
| Migrations             | Alembic                     |
| Background tasks       | TaskIQ + `taskiq-fastapi` + `taskiq-redis` |
| Cache / broker         | Valkey (Redis-compatible) via the `valkey` client |
| Time-series storage    | TimescaleDB extension on Postgres |
| Logging                | `structlog` (event = reserved kwarg — pass `event_type=`) |
| Tracing / metrics      | OpenTelemetry SDK + a pluggable `MetricsBackend` |
| HTTP client (outbound) | `httpx` async client        |
| Crypto                 | `bcrypt`, `cryptography` (Fernet for MFA secrets) |

The full pinned set is in [`pyproject.toml`](../../pyproject.toml).

## Request lifecycle (HTTP)

A typical authenticated `POST /api/v1/backtest/run` does this:

1. Reverse proxy forwards the request to uvicorn (`engine.app:create_app`).
2. **CORS / security middleware** rejects disallowed origins.
3. **Lineage middleware** ([`engine/observability/lineage.py`](../../engine/observability/lineage.py))
   stamps a request id and propagates the OpenTelemetry context.
4. **Rate limiter** ([`engine/api/rate_limit.py`](../../engine/api/rate_limit.py))
   short-circuits abusive clients.
5. **Auth dependency** ([`engine/api/auth/`](../../engine/api/auth/))
   resolves the bearer token to a `User`. If the user has MFA enabled
   the request must carry a valid challenge token from `/login`.
6. **Route handler** in `engine/api/routes/backtest.py` validates the
   payload and runs the computation via FastAPI `BackgroundTasks`
   (**not** TaskIQ — see [known-limitations.md](../known-limitations.md)).
   Results land in an in-process dict keyed by `backtest_id` with a
   1-hour TTL; the `backtest_results` table exists but the REST route
   does not yet write to it.
7. The handler returns `202 Accepted` with the new id; the background
   job runs the backtest through
   [`engine/core/backtest_runner.py`](../../engine/core/backtest_runner.py)
   and writes the composite score / breakdown back into the result.
8. Listeners on `engine/events/bus.py` get notified. The
   [`EventBusBridge`](../../engine/api/ws/event_bridge.py) fans events
   out to WebSocket rooms, and the webhook dispatcher fans out to
   every active webhook config that subscribed to the relevant event.

Synchronous reads (`GET /api/v1/portfolio`, etc.) follow steps 1–5
then return the result directly without enqueueing.

## Event flow

```
domain code  ──▶  EventBus.publish(event)
                  ├──▶  in-process async handlers  (awaited in sequence)
                  ├──▶  Redis/Valkey pub/sub         (cross-replica)
                  │       └──▶  EventBusBridge on every replica  ──▶  WS rooms
                  └──▶  WebhookDispatcher  (HMAC-signed outbound HTTP)
```

The `EventBus` ([`engine/events/bus.py`](../../engine/events/bus.py))
does two things per `publish()`: it `await`s every registered
in-process handler in turn (the webhook dispatcher is one such handler),
**and** it republishes the event onto a Redis/Valkey pub/sub channel
(`nexus:<event_type>`) so consumers on other replicas see it. The
[`EventBusBridge`](../../engine/api/ws/event_bridge.py) is the key
cross-replica consumer: each replica's bridge re-delivers received
events to its local WebSocket rooms, so a portfolio update emitted on
replica A reaches WS clients connected to replica B. If Redis is
unavailable the bus falls back to in-process-only delivery (logged at
warning level).

## Configuration

Every operator-tunable lives in [`engine/config.py`](../../engine/config.py)
as a Pydantic-Settings field. The convention is:

- Field name = `nexus_<area>_<knob>` (lowercase snake-case).
- Env var = uppercase, e.g. `NEXUS_DATABASE_URL`, `NEXUS_VALKEY_URL`,
  `NEXUS_MFA_ENCRYPTION_KEY`.
- Defaults are safe-for-dev. Production values come from the
  operator's secrets vault.

`.env.example` ships the full set so operators know what knobs exist
without reading the source.

## Where to put new code

| Adding…                               | Goes in                                         |
|---------------------------------------|--------------------------------------------------|
| A new HTTP endpoint                   | `engine/api/routes/<area>.py`, registered in `engine/api/router.py` |
| A new background job                  | `engine/tasks/`                                  |
| A new strategy / data provider / executor | A plugin under `engine/plugins/<kind>/<name>/`. See [plugins.md](plugins.md). |
| A new outbound integration (webhook template) | Extend [`engine/events/webhook_dispatcher.py:render_template`](../../engine/events/webhook_dispatcher.py) and the `_VALID_TEMPLATES` set in `routes/webhooks.py`. |
| A new database table / column         | An Alembic revision in `engine/db/migrations/versions/`. See [database.md](database.md). |
| A new metric                          | Use `get_metrics()` from `engine/observability/metrics.py`. Add it to [`docs/operations/slos.md`](../operations/slos.md) **only** if it backs an SLO. |
| A new SLO                             | [`docs/operations/slos.md`](../operations/slos.md) and [`observability/prometheus/slo-rules.yaml`](../../observability/prometheus/slo-rules.yaml) in the same PR. |

## Non-goals

- This is **not** a multi-tenant SaaS by design. Operators run their
  own deployment; the codebase models a single tenant's data per
  database.
- Live trading is intentionally optional. The engine works end-to-end
  on backtests + paper trading without any broker credentials.
