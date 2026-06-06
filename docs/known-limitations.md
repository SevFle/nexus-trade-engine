# Known limitations and technical debt

Honest, specific, prioritised. The point of this doc is to make the
project's rough edges discoverable so that operators, contributors,
and integrators don't waste time finding them the hard way.

Each item is graded by *impact* — not by difficulty to fix:

- **P1** — blocks a stated product goal or correct operation in
  production.
- **P2** — degrades UX / performance / correctness but has a
  workaround.
- **P3** — code smell or deferred cleanup; not user-visible.

The roadmap (`STRATEGY.md`) tracks when each item is scheduled.

## P1 — Live trading execution backend is a scaffold

The `LiveBackend` class in
[`engine/core/execution/live.py`](../engine/core/execution/live.py)
exists, type-checks, and routes orders through the same interface as
`BacktestBackend` and `PaperBackend` — but `execute()` is hard-coded
to return `FillResult(success=False, reason="Live execution not yet
implemented")`. There is no broker client wiring; the constructor
stores credentials but never uses them.

**What this blocks:** any "go-live" workflow. The engine will accept
a live configuration, run paper-trade perfectly, and silently fail
the moment it tries to submit a real order.

**Workaround:** none. Run in paper mode until live lands.

**Related:** `engine/core/live/loop.py` is implemented (269 lines),
and the kill switch (`engine/core/live/kill_switch.py`) is wired.
The OMS, risk engine, and order manager are also in place — the gap
is purely the broker adapter.

## P1 — Multi-strategy orchestration not exposed

The strategy interface (`engine.plugins.sdk`) lets you build a
single-strategy-per-portfolio plugin. The engine has no orchestration
layer for *composing* strategies (e.g. "run mean-reversion and
momentum in parallel, weighted 60/40"). The `signal_aggregator.py`
module exists but is not wired into the live loop yet.

**Workaround:** build a wrapper strategy that internally calls
multiple sub-strategies — workable for closely-coupled ensembles,
painful for cross-asset allocation.

## P1 — WebSocket manager is in-process

`WebSocketManager` in
[`engine/api/websocket/manager.py`](../engine/api/websocket/manager.py)
keeps connection state in a process-local dict. A second replica of
the engine will not see events published on the first; clients
connected to replica A will not receive events emitted on replica B.

**What this blocks:** horizontal scaling of the engine behind a
non-sticky load balancer.

**Workaround:** sticky sessions at the LB (`nginx ip_hash`, AWS ALB
with stickiness enabled). Acceptable for now; not acceptable for a
HA story.

**Planned fix:** Valkey Pub/Sub bridge so every replica subscribes
to a fan-out channel. Not yet on the schedule.

## P1 — Backtest jobs run in-process, not on the TaskIQ worker

The route at
[`engine/api/routes/backtest.py:_run_backtest_background`](../engine/api/routes/backtest.py)
uses FastAPI's `BackgroundTasks`, not the TaskIQ broker. The worker
container in `docker-compose.yml` exists but has nothing to do for
backtests today.

**What this blocks:** horizontal scaling of compute, surviving an app
crash mid-backtest (the in-memory result map evaporates), and the
"submit 100 backtests" use case (each ties up a uvicorn worker
thread).

**Workaround:** keep backtest universe small per request. Polling
within the 1-hour TTL is reliable.

**Migration plan:** move the `_run_backtest_background` body into a
`taskiq.task` and replace `BackgroundTasks.add_task` with
`broker.kiq(...)`. The hard part is plumbing the session factory into
the worker — already done elsewhere in `engine/tasks/`.

## P1 — Backtest results kept in process memory

```python
_backtest_results: dict[str, tuple[float, str, dict[str, Any]]] = {}
_RESULTS_TTL_SECONDS = 3600
```

in `engine/api/routes/backtest.py`. Eviction runs on every poll. If
the app process restarts (or you redeploy), in-flight results are
lost.

**Workaround:** write your own backtest result to a downstream sink
(webhook + a database) if you need durability. The webhook
`backtest.completed` event carries the same payload.

## P1 — Marketplace API is stubbed

`POST /api/v1/marketplace/install` returns
`{"status": "not_implemented"}`. The marketplace route file
acknowledges this in inline `TODO` comments. There is no remote
registry, no signed-package download, no install pipeline.

**Workaround:** install strategies manually by dropping them under
`NEXUS_PLUGIN_DIR` (`./strategies`) and reloading via
`POST /api/v1/strategies/{id}/reload`.

## P1 — MCP Server missing

The MCP server (SEV-223) is on the roadmap and not started. It is
the missing integration point for LLM-driven strategy authoring and
inspection.

## P1 — React frontend is incomplete

`frontend/` exists (Vite + React 18 + Tailwind), has working routes
for login, portfolio list, and backtest submission, but the broader
dashboard (multi-strategy view, marketplace, webhook management) is
partial. Many API endpoints documented here have no UI yet.

**Workaround:** call the API directly (curl, Postman, Python) or
build the missing screens.

## P2 — Tax carry-over state is not persisted

The tax dispatcher (`engine/core/tax/reports.py`) recomputes
carry-forward from the disposals you give it. It does not persist
"realised loss carried into next year" between calls.

**What this means:** if you submit a 2024 tax report and a 2025 tax
report separately, the 2025 call will not know about losses
crystallised in 2024 unless you re-include them in the 2025 input.

**Workaround:** keep your year-end summary and prepend it to the next
year's disposals.

## P2 — Strategy sandbox is best-effort

`engine/plugins/sandbox.py` (401 lines) implements a 5-layer sandbox
(restricted importer, capped CPU, capped memory, network allowlist,
filesystem allowlist). It is enabled by `NEXUS_PLUGIN_SANDBOX_ENABLED=true`.

**Caveats:**

- It runs in the same Python process; it is *not* a security boundary
  against malicious code. A sufficiently-motivated attacker can
  escape with `ctypes` or by importing an unsafe stdlib module the
  allowlist missed.
- The CPU cap is best-effort (no SIGXCPU).
- No GPU isolation.

**Recommendation:** treat every plugin you install as part of your
trusted build. Code-review third-party plugins before they reach
`NEXUS_PLUGIN_DIR`. A WASI-based sandbox is the long-term plan.

## P2 — No PUT/PATCH for portfolios

`Portfolio` records can be created (`POST`) and deleted (`DELETE`)
but not renamed or edited. To change `name` / `description` /
`initial_capital`, the operator must delete and recreate.

**Workaround:** delete + recreate. The cascades are documented in
[`api/portfolio.md`](api/portfolio.md#cascades-on-delete) — understand
them before doing this in production.

## P2 — `installed_strategies` is not used at runtime

The `installed_strategies` table exists in the schema (and is exported
via the privacy endpoint) but the strategies API routes do not read
or write it. Activation state lives in the in-process
`PluginRegistry` instead.

**What this means:** after a process restart, no strategy is loaded
even if it was active before. You must call
`POST /api/v1/strategies/{id}/activate` again.

**Planned fix:** persist activation state to `installed_strategies`
on `/activate` and restore it at startup.

## P2 — Federated role-overwrite is opt-in only

`NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN` defaults to `false`. That means
a user first created via `/auth/register` (which gets the default
`user` role) cannot have their role *upgraded* by a federated IdP
unless an operator explicitly opts in. This is intentional
defence-in-depth (SEV-741) but surprises operators who expect SSO to
be the source of truth for roles.

**Workaround:** flip the flag (and audit the IdP claim shape) when
you want SSO-driven role assignment. Document in your runbook.

## P2 — Deletion cron does not exist

The privacy DSR `delete` request creates a `dsr_requests` row with
`sla_due_at = now() + 30 days`. After the SLA, the user's data should
be hard-deleted. **Today there is no scheduled task that performs the
deletion** — operators must run it manually.

**Workaround:** add a cron job that calls the
`engine.privacy.deletion` module to process overdue rows. The
service-layer code is implemented; only the scheduler integration is
missing.

## P2 — No per-portfolio ACL

Ownership is per-user (`Portfolio.user_id`). There is no model for
sharing a portfolio with another user (read-only or read-write).

**Workaround:** none. Wait for multi-tenant or share credentials
(not recommended).

## P2 — Backtest results do not include trade-by-trade breakdown

The in-memory result map stores `trades` but the API response schema
(`BacktestResultResponse`) does not surface them. The dashboard
cannot show the trade ledger for a run.

**Workaround:** call the strategy runner directly from a notebook.

## P2 — Reference search falls through to Yahoo without rate-limiting

`GET /api/v1/reference/suggest` queries Yahoo's public search API
when the local index has no match. There is no rate limiter between
the engine and Yahoo, so a misbehaving client could trigger Yahoo's
IP-level throttling.

**Workaround:** keep the typeahead debounce high (≥ 300 ms) in
client code.

## P3 — Backtest `composite_score` not always populated

Migration `008_evaluator_score_columns.py` added
`backtest_results.composite_score` and `score_breakdown`. The
backtest route today writes the run output into the in-memory map but
does not always persist a `BacktestResult` row with the score — that
happens only for scoring-strategy runs via the
[`/scoring/{name}/run`](api/scoring.md) endpoint. Backtests
submitted via `/backtest/run` will leave the columns NULL.

## P3 — Some legacy bigserial holdouts

Migration `001` originally used bigserial for some IDs but everything
new uses UUID. A handful of test fixtures and seeds still assume
bigserial. Cosmetic only.

## P3 — `MiniCP` / MCP plumbing missing

`MiniCP` references in commit history suggest a half-started MCP
proto; the directory does not exist today. Not user-facing.

## P3 — Worker concurrency is a global setting

`NEXUS_WORKER_CONCURRENCY` is a single integer for the whole worker
process. There is no per-task-class priority or concurrency limit.
A long-running backtest will starve a quick scheduled job.

**Workaround:** run multiple worker processes with different
`taskiq` queues and route tasks by class. The codebase doesn't yet
expose queue selection — this is a real refactor, not a config tweak.

## P3 — `.local/state/gh/device-id` was historically committed

Fixed in commit `3b44989` (`security(git): untrack gh device-id and
update audit (#734)`). The file is in `.gitignore` now. No action
required unless you're running from an old clone — verify with
`git log -- .local/state/gh/device-id`.

## Process for adding to this list

When you discover a limitation that is not already here, add a row
*in the same PR* that introduces the limitation. The reviewer is the
second line of defence — if a PR adds a new "TODO: real impl", the
limitation should land in this doc at the same time. Updating the doc
later means operators and integrators will hit the gap blindly.

When you fix a limitation, *remove* the row in the same PR. Leaving
stale limitations here is worse than not documenting them in the
first place — readers will start to discount the whole document.
