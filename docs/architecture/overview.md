# System overview

Nexus Trade Engine is a Python service that backtests algorithmic
trading strategies, runs them against live or paper broker
connections, and exposes the results via a REST API and a React
frontend. This document describes the moving pieces and how a request
flows through them.

## Components

```mermaid
flowchart LR
    FE["React frontend<br/>(frontend/)"] -->|HTTPS · JSON · WS| API
    AGENT["LLM agent<br/>(MCP client)"] -.->|JSON-RPC<br/>stdio / HTTP| MCP["engine/mcp/<br/>tools · resources"]
    MCP -.->|backtest · cost model| CORE
    subgraph engine["engine/ (FastAPI app)"]
        API["api/<br/>routers · auth · WS"]
        CORE["core/<br/>backtest · OMS · risk"]
        EV["events/<br/>EventBus"]
    end
    API <--> CORE
    CORE -->|publish| EV
    EV -->|pub/sub<br/>(cross-replica)| WSB["ws/event_bridge"]
    EV -->|fan-out| WH["webhook dispatcher<br/>(HMAC)"]
    WSB --> WS["WS clients"]
    WH --> EXT["external endpoints"]
    CORE -.->|enqueue| WORK["TaskIQ workers<br/>(engine/tasks)"]
    WORK <--> PG[("Postgres<br/>+ TimescaleDB")]
    API <--> PG
    WORK <--> VK[("Valkey / Redis")]
    API <--> VK
    EV <-->|broker| VK
```

Plain-text fallback (same topology):

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
| [`engine/app.py`](../../engine/app.py)            | FastAPI app factory (`create_app`). Wires routers, middleware, lifespan hooks. **This is the uvicorn entrypoint** (`uvicorn engine.app:create_app --factory`). |
| [`engine/main.py`](../../engine/main.py)          | Legacy minimal app module kept for `python -m engine.main`. It mounts only portfolio/strategies/backtest/marketplace and is **not** what `create_app()` produces — do not extend it; add routers to [`engine/api/router.py`](../../engine/api/router.py) instead. |
| [`engine/config.py`](../../engine/config.py)      | Pydantic settings — every env var the engine reads lives here. |
| [`engine/api/`](../../engine/api/)                | HTTP/WebSocket surface: routers, auth, rate limiting, error mapping. |
| [`engine/core/`](../../engine/core/)              | Domain logic: backtest runner, OMS, cost/risk models, analytics. The *capability* map of this tree is [`core-domains.md`](core-domains.md). |
| [`engine/execution/`](../../engine/execution/)    | Concrete broker execution adapter: `LiveExecutionBackend` (SEV-223) — an Alpaca-compatible REST-backed `ExecutionBackend`. Sits **on top of** the `engine/core/execution/` ABC/factory rather than inside it; see [`core-domains.md`](core-domains.md#execution-backends). |
| [`engine/brokers/`](../../engine/brokers/)        | **Broker-direct REST facades** — thin per-broker adapters (`AlpacaBrokerAdapter`, `IBKRBrokerAdapter` #1346) that expose each broker's native order/account surface. `AlpacaBrokerAdapter` delegates to `LiveExecutionBackend`; `IBKRBrokerAdapter` owns its own request pipeline. Neither is wired to a route yet — see [`core-domains.md`](core-domains.md#execution-backends). |
| [`engine/orchestration/`](../../engine/orchestration/) | Multi-strategy `StrategyOrchestrator` (priority / net-position conflict resolution). See [`core-domains.md`](core-domains.md). |
| [`engine/portfolio/`](../../engine/portfolio/)    | Cross-strategy capital allocation (immutable `CapitalAllocation` value object). |
| [`engine/data/`](../../engine/data/)              | Market data providers and the registry that picks one at runtime. |
| [`engine/db/`](../../engine/db/)                  | SQLAlchemy models, async session factory, Alembic migrations. |
| [`engine/events/`](../../engine/events/)          | Event bus + outbound webhook dispatcher (gh#80). |
| [`engine/mcp/`](../../engine/mcp/)                  | Model Context Protocol server: exposes a read-only tool/resource surface to LLM agents over stdio or HTTP. Not mounted in the FastAPI app — it runs as a separate process. See [mcp-server.md](../mcp-server.md). |
| [`engine/observability/`](../../engine/observability/) | Structlog wiring, lineage middleware, pluggable metrics backend (gh#34). |
| [`engine/plugins/`](../../engine/plugins/)        | Plugin SDK and runtime registry. See [plugins.md](plugins.md). |
| [`engine/tasks/`](../../engine/tasks/)            | TaskIQ worker definitions for async work (backtests, scheduled jobs). |
| [`engine/legal/`](../../engine/legal/)            | Legal-document acceptance (Terms / Privacy / etc.). |
| [`engine/privacy/`](../../engine/privacy/)        | GDPR/CCPA surface: `deletion.py` (30-day grace + anonymize), `dsr.py` (request ledger), `export.py`. |
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

1. Reverse proxy forwards the request to uvicorn
   (`engine.app:create_app`).
2. The **middleware stack** (see [Middleware stack](#middleware-stack))
   runs in fixed order, outermost first: `HttpMetricsMiddleware` times
   the whole request, `CorrelationIdMiddleware`
   ([`engine/observability/middleware.py`](../../engine/observability/middleware.py)
   — the raw-ASGI correlation-id middleware; *not* `lineage.py`, which is
   the data-lineage DAG) stamps/resumes the `X-Correlation-ID`,
   `BodySizeLimitMiddleware` rejects bodies over 1 MiB,
   `RateLimitMiddleware`
   ([`engine/api/rate_limit.py`](../../engine/api/rate_limit.py))
   short-circuits abusive clients, `CORSMiddleware` enforces the origin
   allow-list, and `SecurityHeadersMiddleware` stamps HSTS/CSP/etc.
3. **Auth dependency** ([`engine/api/auth/`](../../engine/api/auth/))
   resolves the bearer token to a `User`. If the user has MFA enabled
   the request must carry a valid challenge token from `/login`.
4. **Route handler** in `engine/api/routes/backtest.py` validates the
   payload and runs the computation via FastAPI `BackgroundTasks`
   (**not** TaskIQ — see [known-limitations.md](../known-limitations.md)).
   Results land in an in-process dict keyed by `backtest_id` with a
   1-hour TTL; the `backtest_results` table exists but the REST route
   does not yet write to it.
5. The handler returns `202 Accepted` with the new id; the background
   job runs the backtest through
   [`engine/core/backtest_runner.py`](../../engine/core/backtest_runner.py)
   and writes the composite score / breakdown back into the result.
6. Listeners on `engine/events/bus.py` get notified. The
   [`EventBusBridge`](../../engine/api/ws/event_bridge.py) fans events
   out to WebSocket rooms, and the webhook dispatcher fans out to
   every active webhook config that subscribed to the relevant event.

Synchronous reads (`GET /api/v1/portfolio`, etc.) follow steps 1–3
(proxy → middleware → auth) then return the result directly without
enqueueing.

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

## Middleware stack

`create_app()` ([`engine/app.py`](../../engine/app.py)) registers
middleware with `app.add_middleware(...)`. Starlette wraps such that the
**last** middleware added is the **outermost** (it runs first on the
request, last on the response). Added order and resulting execution
order:

| Added (#) → runs | Middleware | Role |
|---|---|---|
| 1 → innermost | `SecurityHeadersMiddleware` | HSTS, CSP, `X-Content-Type-Options`, frame-options, referrer-policy. Tunable via `SecurityHeadersConfig`. |
| 2 | `CORSMiddleware` | Origin allow-list from `NEXUS_CORS_ORIGINS`; `allow_credentials=True`. |
| 3 | `RateLimitMiddleware` | Token bucket per IP + per-role tiers. Valkey-backed when `NEXUS_RATE_LIMIT_VALKEY_ENABLED`, else in-process. Per-route override caps `/api/v1/client/errors` at 30/min. |
| 4 | `BodySizeLimitMiddleware` | Hard 1 MiB request cap (Starlette ships no default). |
| 5 | `CorrelationIdMiddleware` | Raw-ASGI: stamps/resumes `X-Correlation-ID` into the structlog context. A class-identity assertion in `create_app()` guards against re-pointing this at the `BaseHTTPMiddleware` variant, which leaks/unbinds correlation ids across streaming responses and `BackgroundTasks`. |
| 6 → outermost | `HttpMetricsMiddleware` | Prometheus latency / counters / in-flight gauges. Added last so it times the entire stack, including `/metrics` itself. |

> `engine/observability/lineage.py` is **not** a middleware. It is the
> data-lineage DAG (`provider → bar → signal → backtest → report`). The
> request-id/correlation surface lives in
> [`engine/observability/middleware.py`](../../engine/observability/middleware.py).

## Process lifecycle (lifespan)

`create_app()` installs an async `lifespan`
([`engine/app.py`](../../engine/app.py)). Startup runs, in order:

1. **`_init_observability`** — structlog setup; installs the OTel
   `TracerProvider` (a graceful no-op without a collector) and
   instruments FastAPI + the async SQLAlchemy engine; initialises
   Sentry; flips the global metrics singleton to `PrometheusBackend`.
2. **`_check_secret_key`** — fails fast if `NEXUS_SECRET_KEY` is unset
   outside the test environment (tests are exempt so the suite runs
   without a vault).
3. **`_init_app_state`** — opens the Valkey client; builds the
   `AuthProviderRegistry` from `NEXUS_ENABLED_PROVIDERS`
   (`local`/`google`/`github`/`oidc`/`ldap`); bootstraps data providers
   from the YAML registry (falls back to a default Yahoo adapter in
   dev); seeds the reference search index; syncs legal docs from
   `legal/`.
4. **`_init_websockets_and_events`** — builds the `ConnectionManager`,
   the `EventBus` (Valkey pub/sub), and **two** bridges: the room-based
   `EventBusBridge`, plus a user-scoped order/signal bridge that stamps
   `user_id`/`tenant_id` and routes to `user:<id>` rooms so one user's
   events never land on another user's socket.
5. **`_init_taskiq_broker`** — opens the TaskIQ broker pool so the API
   can enqueue tasks. A broker outage degrades task submission without
   aborting startup.

Shutdown (`_shutdown`) runs each teardown in its own `try/except`
(ws bridges → ws manager → event bus → TaskIQ broker → Valkey → DB
engine → Sentry flush) so a failure in one step cannot block the rest.

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
| A new MCP tool / resource             | `engine/mcp/tool_definitions.py` (+ adapter in `engine/mcp/adapters/`); see [mcp-server.md](../mcp-server.md) |
| A new background job                  | `engine/tasks/`                                  |
| A new strategy / data provider / executor | A strategy package under [`strategies/<name>/`](../../strategies/) (manifest + `strategy.py`); a data provider via `engine/data/providers/` + the YAML registry. See [plugins.md](plugins.md). |
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
