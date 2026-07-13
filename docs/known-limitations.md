# Known Limitations & Technical Debt

An honest inventory, ranked by impact. Each item is something an
operator or new contributor will trip over — and what they should do
about it today.

Priority legend:

- **P0** — incorrect or unsafe behaviour. Fix before exposing to
  untrusted input or live money.
- **P1** — operational fragility or missing core feature on the
  published roadmap. Fix in the next quarter.
- **P2** — ergonomics / polish. Fix when next touching the area.

---

<a id="rebalancer-merge-conflict"></a>
## P2 — No CI guard against committed merge-conflict markers

**Background**: an uncommitted merge conflict in
[`engine/portfolio/rebalancer.py`](../engine/portfolio/rebalancer.py)
once left an unterminated string literal that made the whole
`engine.portfolio` package `SyntaxError` on import. The conflict was
**never committed** — `HEAD` was clean and CI ran green — and the
working tree has since been resolved (`engine.portfolio` now imports
cleanly, the file is 441 lines, zero conflict markers). The incident
is worth a durable guard because the failure mode is severe:
`engine.api.router` imports `engine.portfolio` at app build time, so a
single committed `<<<<<<<` / `=======` / `>>>>>>>` block would
`SyntaxError` every web **and** worker process at startup — the app
would not boot, and CI would only catch it *after* the bad commit
landed.

**Fix path**: add a one-line gate so merge markers can never ship on
`main` silently — drop it into the lint stage of
`.github/workflows/ci.yml`:

```bash
grep -rnE '^(<<<<<<< |=======|>>>>>>> )' engine/ && exit 1 || exit 0
```

Zero false positives, and it would have caught the working-tree hazard
before a careless `git add` could commit it. (This is the same class of
"invisible until a fresh prod deploy" drift the [no-Alembic-check P2](#no-alembic-check)
below guards against — small CI jobs that assert reality against the
source of truth.)

---

## P0 — Backtest results are not persisted

**Where**: [`engine/api/routes/backtest.py:22`](../engine/api/routes/backtest.py#L22)

The `POST /api/v1/backtest/run` route stores results in an
**in-process Python dict** (`_backtest_results`) with a 1-hour TTL.
The `BacktestResult` table exists (migration 002) and is used by the
strategy evaluator, but the REST endpoint does not write to it.

**Impact**: every process restart loses in-flight and completed
backtests. Multi-replica deploys cannot share results. The GDPR export
gracefully handles orphaned rows (gh#157) but the rows aren't being
created in the first place.

**Workaround today**: poll `GET /backtest/results/{id}` within 1 hour
of submission, from the same replica. Operators running behind a load
balancer need sticky sessions on `/api/v1/backtest/*`.

**Fix path**: switch `_run_backtest_background` to write to
`backtest_results` and the GET handler to query by `id` + `user_id`.
The `BackgroundTasks` call should become a TaskIQ enqueue.

---

<a id="privacy-tables-no-migration"></a>
## P0 — `consent_records` & `deletion_schedules` have models but no migration

**Where**: [`engine/db/models.py`](../engine/db/models.py) defines
`ConsentRecord` and `DeletionSchedule`; [`engine/privacy/deletion.py`](../engine/privacy/deletion.py)
reads/writes both from the account-deletion path.

Both tables are created by **no** revision in
[`engine/db/migrations/versions/`](../engine/db/migrations/versions/).
`alembic upgrade head` against an empty Postgres produces a schema that
is missing both, so the first `POST /api/v1/delete` (and any consent
write) fails at runtime with `asyncpg.exceptions.UndefinedTableError`.

This is exactly the drift class that migration
[`013_user_processing_restricted`](../engine/db/migrations/versions/013_user_processing_restricted.py)
just closed for the `users.processing_restricted` column (gh#157 / #984)
— its docstring explicitly calls out the `deletion.py` references.
`consent_records` and `deletion_schedules` are the remaining half of
that hole.

**Why it ships green**: the test suite runs against SQLite and calls
`Base.metadata.create_all`, which builds every model table directly and
bypasses Alembic. The drift is invisible until a fresh Postgres is
provisioned via migrations alone.

**Impact**: the GDPR Art. 17 account-deletion flow — a regulated path
— is broken on any database provisioned from `alembic upgrade head`.
Existing dev/CI DBs created via `create_all` are unaffected, which is
why it has gone unnoticed.

**Workaround today**: after `alembic upgrade head`, materialize both
tables from the ORM metadata once against the prod DSN (idempotent,
adds only the missing tables):

```python
import asyncio
from engine.db.models import Base
from engine.db.session import engine

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
asyncio.run(main())
```

**Fix path**: add `014_consent_and_deletion_tables.py` that
`create_table`s both with their indexes
(`ix_consent_user_purpose_time`, `ix_deletion_schedule_status_due`),
then land the CI job from the P2 ["No Alembic check in CI"](#no-alembic-check)
entry so a model-without-migration can't recur silently.

---

## P1 — Three Execution Modes (Roadmap: partial)

The execution layer is split across two packages — both land on disk
but neither is wired to a public run route:

- [`engine/core/execution/`](../engine/core/execution/) holds the
  `ExecutionBackend` ABC, the concrete `BacktestBackend` and
  `PaperExecutionBackend`, the **scaffold** `LiveBackend`
  (`_is_scaffold = True`, talks to no broker), and the
  `create_backend(...)` factory.
- [`engine/execution/`](../engine/execution/) (SEV-223) holds the
  **concrete** `LiveExecutionBackend` — an Alpaca-compatible REST
  adapter over an injectable `httpx.AsyncClient` with broker-direct
  `submit_order` / `cancel_order` / `get_order_status` helpers, typed
  broker-error translation, and a uuid `client_order_id` on every
  submit for idempotent retries (gh#49eec71). It is unit-tested
  ([`tests/test_live_backend.py`](../tests/test_live_backend.py)) but
  **not** registered in the factory and **not** mounted by any route.
- A second concrete Alpaca adapter — `AlpacaTradingClient` in
  [`engine/core/brokers/alpaca/`](../engine/core/brokers/alpaca/)
  (gh#136) — targets the `BrokerClient` Protocol (clock / account /
  positions) rather than the `ExecutionBackend` the order manager
  calls. The two adapters are not yet unified.
- [`engine/core/live/`](../engine/core/live/) (`loop.py`,
  `kill_switch.py`) is scaffolded; the live loop has no route entry,
  no worker task, and no LB / health integration.
- Read-only `AlpacaDataProvider` market data is the only shipped
  Alpaca surface that *is* reachable from a route today.

**Workaround today**: the engine is a **backtest engine** for
production purposes. Treat every live/paper code path as an internal
preview. The concrete pieces (`LiveExecutionBackend`,
`AlpacaTradingClient`) are callable as a library, but there is no
`POST /.../live/run` (or paper) route to drive them.

**Fix path**: register `LiveExecutionBackend` with the execution
factory, then expose a `POST /api/v1/.../run` route guarded by the
strategy-lifecycle `live` gate and the `RiskEngine` kill-switch. The
broker-direct helpers, error mapping, and `client_order_id` idempotency
are already in place — the missing piece is the route + worker glue.

---

## P1 — Strategy Marketplace is a stub

**Where**: [`engine/api/routes/marketplace.py`](../engine/api/routes/marketplace.py)

`browse`, `install`, `uninstall`, `rate` all return
`{"status": "not_implemented"}`. The categories endpoint returns a
hardcoded list. There is no marketplace registry (local or remote)
behind the routes.

**Workaround today**: install strategies by placing them under
`engine/plugins/<kind>/<name>/` and reloading the plugin registry.
The marketplace routes exist purely to lock the public API shape.

---

<a id="react-dashboard"></a>
## P1 — React dashboard is real but not production-built

**Where**: [`frontend/`](../frontend/) — a Vite + React 18 + Tailwind +
react-query SPA with 12 routed screens, a component library, and a
Vitest suite.

The shell and the read paths are genuinely wired to the engine:

- **Auth** — `Login`, `Register`, `OAuthCallback`, `ProtectedRoute`,
  and `AuthContext` ([`frontend/src/auth/`](../frontend/src/auth/))
  hit the real `POST /api/v1/auth/*` routes, store the JWT, and
  refresh it.
- **Legal gate** — `LegalContext` + `ConsentModal` react to the `451`
  status the engine returns for missing acceptances
  ([`frontend/src/api/client.js`](../frontend/src/api/client.js) raises
  `ConsentRequiredError` and emits a `legal:consent-required` event).
- **Onboarding** + `ErrorBoundary` (per-page and app scope) POST
  browser errors to `POST /api/v1/client/errors`.
- **`MarketWatch`** is the one analytical screen backed by live data
  ([`frontend/src/api/marketData.js`](../frontend/src/api/marketData.js)).

What is **not** wired is the core trading surface. These screens render
hardcoded `MOCK_*` fixtures instead of engine responses:

- [`Dashboard.jsx`](../frontend/src/screens/Dashboard.jsx) — `MOCK_PORTFOLIO`.
- [`Backtest.jsx`](../frontend/src/screens/Backtest.jsx) — `MOCK_RESULTS`,
  `MOCK_CONFIG`, a synthetic `EQUITY_CURVE` (it never calls
  `POST /api/v1/backtest/run`).
- [`Positions.jsx`](../frontend/src/screens/Positions.jsx),
  [`Strategies.jsx`](../frontend/src/screens/Strategies.jsx),
  [`Marketplace.jsx`](../frontend/src/screens/Marketplace.jsx) — mock lists.

There is also **no production artifact**:

- [`frontend/Dockerfile`](../frontend/Dockerfile) `CMD`s `npm run dev`
  (the Vite dev server) and `EXPOSE`s 3000 — there is no `vite build`
  + static-serve step, so the image is not ship-able as-is.
- [`docker-compose.yml`](../docker-compose.yml) (the production
  compose) has no `frontend` service — only `app`, `worker`, `db`,
  `valkey`. The frontend appears only in
  [`docker-compose.dev.yml`](../docker-compose.dev.yml) (HMR dev server).

**Impact**: the React app is a usable development / preview surface and
a real integration client for auth / legal / market-data, but it is not
a deployed product. Treat the engine REST API as the only production UI
surface today.

**Workaround today**: run the frontend locally with `cd frontend &&
npm install && npm run dev` (or `make docker-dev`) pointed at a local
engine. Do not serve `frontend/Dockerfile` in production.

**Fix path**: (1) replace the `MOCK_*` fixtures in the five analytical
screens with `useQuery` calls against the existing engine routes;
(2) convert `frontend/Dockerfile` to a multi-stage build (`vite build` →
`nginx`/`Caddy` static serve, or `vite preview`); (3) add a `frontend`
service (static host) to `docker-compose.yml`. None of (1)–(3) require
engine-side changes — the API surface already exists.

---

## P1 — Data provider registry has no first-class credentials store

**Where**: [`engine/data/providers/config.py`](../engine/data/providers/config.py),
[`config/data_providers.example.yaml`](../config/data_providers.example.yaml).

Provider secrets (Polygon API key, Alpaca key/secret, Binance) are
read from the YAML at `NEXUS_DATA_PROVIDERS_CONFIG`. That YAML is
expected to live on disk and be readable by the engine process.

**Impact**: secret rotation requires a process restart; no integration
with Vault / AWS Secrets Manager / GCP Secret Manager; no per-tenant
secrets.

**Workaround today**: render the YAML at deploy time from your secrets
manager of choice (envsubst, Helm, etc.) and ship it as a bind-mounted
read-only file.

---

## P2 — WebSocket connection registry is process-local (events are cross-replica)

**Where**: [`engine/api/ws/connection_manager.py`](../engine/api/ws/connection_manager.py).

The live `WebSocket` objects themselves live in a per-process dict, so a
client must reconnect to the replica it originally hit. **Event delivery is
already cross-replica**, however: the
[`EventBusBridge`](../engine/api/ws/event_bridge.py) subscribes to the
[`EventBus`](../engine/events/bus.py), which publishes over Redis/Valkey
pub/sub, so events emitted on replica A reach local connections on every
replica.

The remaining gap is that there is no shared connection registry or sticky
sessioning, so a client whose replica dies must reconnect. There is also no
back-pressure signal back to the `EventBus` if a room has no local
subscribers — the bridge fans out unconditionally.

**Workaround today**: deploy behind a load balancer that supports
connection draining, or accept that a replica restart drops its in-flight WS
sessions. Event correctness (via the bridge) does not depend on a single
replica.

---

## P2 — WebSocket does not accept API keys

**Where**: [`engine/api/ws/auth.py`](../engine/api/ws/auth.py#L158)

The active WS authenticator calls `decode_token` (JWT only). It does
**not** run the `is_engine_token` / `find_active_by_token` path that the
REST `get_current_user` dependency uses, so a `nxs_*` API key cannot open
a WS connection. The legacy `routes/websocket.py` did support API keys;
that code is no longer mounted.

**Workaround today**: headless clients mint a short-lived JWT via
`POST /api/v1/auth/login` (or the API-key → JWT exchange if added) and
use that for WS. If long-lived WS access for automation is needed, port
the API-key branch from the legacy endpoint into `ws/auth.py`.

---

## P1 — TaskIQ plumbing incomplete

`engine/tasks/worker.py` defines the broker; the compose file runs
it. But:

- `POST /backtest/run` uses FastAPI `BackgroundTasks`, not TaskIQ —
  so backtests run in the *web* process, not the worker. A long
  backtest stalls the uvicorn worker pool.
- The task-pipeline SLO in [`operations/slos.md`](operations/slos.md)
  is defined but has no emitter yet — `nexus.task.runs_total` is the
  intended metric name.

**Workaround today**: run uvicorn with `--workers N` and tune
`NEXUS_WORKER_CONCURRENCY` high enough to absorb long backtests. Do
not depend on the worker process for backtest isolation today.

---

<a id="mcp"></a>
## P1 — MCP server is a library, not a runnable process

**Where**: [`engine/mcp/`](../engine/mcp/)

The MCP module ships every component an MCP server needs — declarative
[`tool_definitions.py`](../engine/mcp/tool_definitions.py) (11 tools,
including the portfolio `get_position` / `get_unrealized_pnl` inspection
tools and the per-portfolio ownership model in `adapters/__init__.py`),
[`handlers.py`](../engine/mcp/handlers.py) dispatch with schema validation +
cursor pagination + result-size guards, [`auth.py`](../engine/mcp/auth.py)
(JWT + static API-key table, reusing the REST `ROLE_HIERARCHY`), a
per-principal [`rate_limiter.py`](../engine/mcp/rate_limiter.py),
[`resources.py`](../engine/mcp/resources.py), [`config.py`](../engine/mcp/config.py)
under the `NEXUS_MCP_` prefix, and [`observability.py`](../engine/mcp/observability.py).

What is **missing** is the transport-binding entry point
`engine/mcp/server.py` — the module that instantiates the `mcp.server`
`Server`, wires `tools/list` · `tools/call` · `resources/list` ·
`resources/read` to `dispatch_tool` / `read_resource` / `list_resources`,
threads `extract_principal` + `RateLimiter` through every call, and runs
the `stdio`/`http` transport. There is no `Server(...)` instantiation,
no `__main__`, and no `[project.scripts]` entry anywhere in the repo.
`pyproject.toml` still references the file
(`"engine/mcp/server.py" = ["PLR0911"]`), which is why CI/lint expect
it.

**Impact**: the MCP surface cannot be started today. The tool/resource/
auth contract is implemented and unit-tested, but no client (Claude
Desktop, a custom agent, …) can connect to it. (The `NEXUS_MCP_*` knobs
**are** now documented in [`.env.example`](../.env.example) under the
`MCP server (engine/mcp)` block, so operators have the inventory; only
the runnable entry point is missing.)

**Workaround today**: none at runtime. To exercise the components,
instantiate `EngineServices` (online or `for_testing`) and call
`dispatch_tool(...)` / `read_resource(...)` directly from a script or
test — exactly what [`tests/mcp/`](../tests/mcp/) does. See
[`mcp-server.md`](mcp-server.md) for the contract a future `server.py`
must bind.

**Fix path**: write `engine/mcp/server.py` that binds the transport to
the existing `dispatch_tool` / `read_resource` / `extract_principal` /
`RateLimiter`, and add a `[project.scripts]` entry (e.g.
`nexus-mcp = engine.mcp.server:main`). The `NEXUS_MCP_*` block is
already in `.env.example`, so no env-doc change is needed. The PLR0911
ignore already in `pyproject.toml` anticipates the multi-branch
transport dispatcher.

---

## P2 — Per-route ignore list in `pyproject.toml` is large

**Where**: [`pyproject.toml`](../pyproject.toml) lines 73–130.

`[tool.ruff.lint.per-file-ignores]` has accumulated many specific
overrides (PLR2004, PLC0415, E501, etc.). This is "correct" in that
each ignore was added deliberately, but it makes the lint signal
weaker overall and hides real issues.

**Fix path**: chip away — every PR that touches one of these files
should try to drop the ignore. Don't fix in one mega-PR.

---

## P2 — Marketplace `MarketplaceEntry` Pydantic model is unused

Defined in [`routes/marketplace.py:15`](../engine/api/routes/marketplace.py#L15)
but never returned by any handler. Remove when the marketplace stub
is replaced.

---

<a id="no-alembic-check"></a>
## P2 — No Alembic check in CI

There is no automated check that `alembic upgrade head` against an
empty DB matches the SQLAlchemy models. Drift is caught only when a
human reads `models.py` and the migration side-by-side — or, worse,
when a fresh-Postgres deploy throws `UndefinedTableError` in
production (see the [`consent_records` / `deletion_schedules`
P0](#privacy-tables-no-migration) above, and the now-fixed
`users.processing_restricted` column that #984 backfilled).

**Fix path**: add a CI job that boots an empty Postgres service,
runs `alembic upgrade head`, then asserts each model table exists
(reflect over `Base.metadata.sorted_tables` and query
`information_schema.tables`). ~30 lines of bash. This single job
would have caught both the `processing_restricted` regression and
the two-table drift this cycle surfaced.

---

## P2 — Test coverage gate at 70% (`make test`), 80% in `pyproject.toml`

`Makefile` runs `pytest --cov-fail-under=70`; `pyproject.toml`
declares `fail_under = 80`. The Makefile is the canonical entry
point. The mismatch is unintentional drift; the project actually
passes the 80% gate (per recent CI runs and `LAST_AUDIT.md`).

**Fix path**: align both at 80%.

---

## P2 — SLO metric coverage is incomplete

`set_metrics(PrometheusBackend())` runs in the lifespan and
`/metrics` is exposed, so the scrape path works. But the **intended
SLI metric contract is only partially emitted**: the rules file in
[`observability/prometheus/slo-rules.yaml`](../observability/prometheus/slo-rules.yaml)
is written against `nexus_*` names that several code paths do not yet
produce. Concretely:

- No `auth_attempts_total`, `backtest_submissions_total`, or
  `task_runs_total` counter exists at the call site, so the auth /
  backtest-submit / task-pipeline SLOs can never fire.
- The HTTP metrics are emitted as `http.request.count` /
  `http.request.duration_ms` tagged `method`+`status_class`, not the
  `route`+`status_code` shape the rules expect (see the coverage table
  in [`operations/slos.md`](operations/slos.md)).

**Workaround today**: the API availability/latency SLOs are
approximately observable (histogram exists). Treat the auth, webhook,
backtest, and task SLOs as uninstrumented until the matching counters
land. Wire Prometheus against `/metrics` and Alertmanager against the
rule file regardless — the alerts that *can* fire will.

---

## P2 — Live trading routes not yet SLO'd

The SLO table in `operations/slos.md` calls out that live trading
will need its own SLOs when #109/#111 land. Today there's nothing to
SLO because live isn't shipped.

---

## P2 — Some decisions still lack an ADR

[`docs/adr/`](adr/README.md) now captures ten decisions: scaffold
(0001), auth/RBAC (0002), mobile/PWA (0003), TaskIQ (0004), Valkey
(0005), bcrypt+Fernet (0006), the strategy sandbox allowlist import
model (0007), the pluggable `MetricsBackend` Protocol (0008), the
cross-replica `EventBus` WebSocket bridge (0009), and the static AST
validation + TOCTOU-safe strategy loader (0010). A handful of smaller
decisions are still recorded only as PR descriptions or commit
messages — e.g. the in-process backtest result store (this is tech
debt to be fixed, P0 above, rather than an accepted architecture
decision). Use [`adr/template.md`](adr/template.md) to capture
remaining decisions as they come up in code review; don't batch them
into one mega-ADR.

---

## Not-a-limitation: intentional non-goals

These come up enough in code review that they're worth listing:

- **Not multi-tenant SaaS.** One operator per database. Multi-org is
  out of scope for the foreseeable future (see
  [`architecture/overview.md`](architecture/overview.md)).
- **No embedded DB for production.** SQLite is supported in tests
  only; production needs Postgres (TimescaleDB optional but
  recommended).
- **No sync DB sessions in handlers.** Async-only. Don't add sync
  code paths even for "quick scripts" — they block the event loop.
- **No first-class mobile app.** ADR-0003 covers this; the API is
  the public surface.
