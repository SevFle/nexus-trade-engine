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

## P1 — Three Execution Modes (Roadmap: partial)

Live and paper execution land in `engine/core/execution/`, but the
public surface only exposes backtest. Specifically:

- `engine/core/execution/paper.py` and `live.py` exist but are wired
  to nothing on the API surface.
- `engine/core/live/loop.py` and `kill_switch.py` are scaffolded; the
  live loop has no route entry, no worker task, and no LB / health
  integration.
- The README lists "Live broker integration (Alpaca, IBKR)" as a
  roadmap item; only `AlpacaDataProvider` (read-only market data) is
  shipped.

**Workaround today**: the engine is a **backtest engine** for
production purposes. Treat the live execution code as an internal
preview.

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
[`tool_definitions.py`](../engine/mcp/tool_definitions.py) (9 tools),
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
Desktop, a custom agent, …) can connect to it. `.env.example` also does
not list the `NEXUS_MCP_*` vars, so operators have no inventory of the
knobs without reading [`config.py`](../engine/mcp/config.py).

**Workaround today**: none at runtime. To exercise the components,
instantiate `EngineServices` (online or `for_testing`) and call
`dispatch_tool(...)` / `read_resource(...)` directly from a script or
test — exactly what [`tests/mcp/`](../tests/mcp/) does. See
[`mcp-server.md`](mcp-server.md) for the contract a future `server.py`
must bind.

**Fix path**: write `engine/mcp/server.py` that binds the transport to
the existing `dispatch_tool` / `read_resource` / `extract_principal` /
`RateLimiter`, add a `[project.scripts]` entry (e.g.
`nexus-mcp = engine.mcp.server:main`), and add the `NEXUS_MCP_*` block
to `.env.example` in the same PR. The PLR0911 ignore already in
`pyproject.toml` anticipates the multi-branch transport dispatcher.

---

## P2 — Dead duplicate WebSocket package (`engine/api/websocket/`)

**Where**: [`engine/api/websocket/`](../engine/api/websocket/)
(`manager.py`, `bridge.py`) + [`engine/api/routes/websocket.py`](../engine/api/routes/websocket.py).

The **live** `/api/v1/ws` route is [`engine/api/ws/router.py`](../engine/api/ws/router.py)
(SEV-275: channel-based pub/sub, JWT auth, scope/owner permissions, see
[`api-reference.md`](api-reference.md#websocket)). It is what
[`engine/api/router.py`](../engine/api/router.py) includes.

A second, older implementation still ships in `engine/api/websocket/`
(`UserTopicManager`, topic-based: `portfolio`/`backtest`/`order`/`alert`).
Its only consumer is `engine/api/routes/websocket.py`, and that route
file is **not imported by `router.py`** — so the whole package backs an
endpoint that is never mounted. (The known-limitations entry above on
WS not accepting API keys already notes this route is "no longer
mounted"; the package itself is the remaining debt.)

This is actively misleading because commit `6011b56` ("security(websocket):
Deny-list sensitive namespaces") hardened `engine/api/websocket/manager.py`
against a HIGH-severity ACL bypass — i.e. **a security fix landed on
dead code**. The live `engine/api/ws/` path is safe by construction
(`VALID_CHANNELS` is an allow-list of `portfolio`/`orders`/`strategies`
plus the permission matrix in `ws/permissions.py`), so there is no
live vulnerability, but a reader who greps for the SEV fix lands on the
dead module.

**Workaround today**: none needed for correctness. When extending WS
behaviour, work in `engine/api/ws/` only.

**Fix path**: delete `engine/api/routes/websocket.py`, then
`engine/api/websocket/` and its references in
`engine/data/streaming/__init__.py`'s docstring. The
`data/streaming/` primitives (`BoundedBuffer`, `ReplayLog`) are
unaffected — they import nothing from the dead package at runtime.

---

## P2 — Stale `engine/orchestration/` package (source removed)

**Where**: [`engine/orchestration/`](../engine/orchestration/).

The multi-strategy orchestrator source lives at
[`engine/core/strategy_orchestrator.py`](../engine/core/strategy_orchestrator.py)
(see [`architecture/strategy-orchestration.md`](architecture/strategy-orchestration.md)).
An older `engine/orchestration/` package was removed, but its
`__pycache__/orchestrator.cpython-311.pyc` and `__init__.cpython-311.pyc`
remain on disk. There is no `.py` source under `engine/orchestration/`
anymore — only compiled bytecode.

**Impact**: none at runtime (nothing imports the package — the live
class is `engine.core.strategy_orchestrator.StrategyOrchestrator`). The
risk is purely navigational confusion: a contributor searching for
"orchestrator" finds a stale package dir and may edit the wrong place,
or assume the compiled bytecode is loadable.

**Workaround today**: ignore `engine/orchestration/`; the real code is
in `engine/core/strategy_orchestrator.py`.

**Fix path**: `rm -rf engine/orchestration/` and add the directory to
`.gitignore`-adjacent cleanup. No code references it.

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

## P2 — No Alembic check in CI

There is no automated check that `alembic upgrade head` against an
empty DB matches the SQLAlchemy models. Drift is caught only when a
human reads `models.py` and the migration side-by-side.

**Fix path**: add a CI job that boots an empty Postgres service,
runs `alembic upgrade head`, then asserts each model table exists.
~30 lines of bash.

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

[`docs/adr/`](adr/README.md) now captures nine decisions: scaffold
(0001), auth/RBAC (0002), mobile/PWA (0003), TaskIQ (0004), Valkey
(0005), bcrypt+Fernet (0006), the strategy sandbox allowlist import
model (0007), the pluggable `MetricsBackend` Protocol (0008), and the
cross-replica `EventBus` WebSocket bridge (0009). A handful of smaller
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
