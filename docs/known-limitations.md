# Known limitations and technical debt

This is the honest list of what the engine does not yet do, in
priority order. Each entry has a concrete trigger ("when you will
hit this"), the current workaround, and the planned fix.

## P0 — production blockers

These are issues that prevent a safe production deploy for a new
operator.

### 1. Marketplace is scaffolded, not functional

**Trigger.** Any user trying to install a strategy from
`/api/v1/marketplace/install`.

**Symptom.** Every write endpoint returns
`{"status": "not_implemented"}`. Browse + category endpoints work
but return canned data, not a registry of real strategies.

**Why.** The marketplace registry is the single largest
outstanding feature. It needs: package signing, version pinning,
download + integrity verification, dependency resolution, and a
review surface. None of that is built.

**Workaround.** Operators install strategies by dropping a
directory under `strategies/<name>/` with `manifest.yaml` +
`strategy.py`. The plugin registry discovers them via filesystem
scan.

**Planned fix.** Track in [STRATEGY.md](../STRATEGY.md) — the
marketplace spec is being written.

### 2. Live trading is stubbed

**Trigger.** Switching a portfolio to live execution.

**Symptom.** `engine/core/execution/live.py` is a placeholder.
`engine/core/brokers/` has a paper broker and a registry; live
broker adapters (Alpaca, IBKR) are missing.

**Why.** Live trading has the highest bar for safety and
observability of anything in this codebase. Shipping it before
the marketplace and the kill switch are battle-tested was judged
irresponsible.

**Workaround.** Paper trading works end-to-end via
`engine/core/execution/paper.py`. Backtests work end-to-end.

**Planned fix.** Alpaca is the first target (REST + WebSocket).
The interface (`ExecutionBackend`) is stable; only the adapter is
missing.

### 3. TaskIQ scheduler is single-replica

**Trigger.** Running two worker replicas with scheduled tasks
(`@broker.task(schedule=...)`).

**Symptom.** Both replicas fire the scheduled task; the queue
gets duplicates.

**Why.** TaskIQ's `TaskiqScheduler` runs in-process and does not
implement distributed locking. Multi-replica deploys need a
single designated scheduler replica or an external trigger.

**Workaround.** Designate one worker replica as the scheduler
(via a separate compose service with a different entrypoint) and
let the others just consume.

**Planned fix.** Either add Valkey-based locking to the
scheduler or move scheduled tasks to an external cron / Cloud
Scheduler that enqueues the task via the API.

### 4. WebSocket fan-out is per-process

**Trigger.** Multi-replica deploy with active WebSocket
clients.

**Symptom.** A user connected to replica A does not receive
events emitted on replica B.

**Why.** The connection manager
([`engine/api/websocket/manager.py`](../engine/api/websocket/manager.py))
is process-local. There is no Valkey pub/sub fan-out layer.

**Workaround.** Pin WebSocket traffic to a single replica via
sticky sessions in the load balancer.

**Planned fix.** Subscribe the manager to a per-user Valkey
pub/sub channel; emit into the channel from the event bus. The
manager already exposes the shape (`broadcast_to_user`) this
work will consume.

## P1 — operational concerns

These bite you at scale or in incident response, but don't block
a safe launch.

### 5. Backtest results are not persisted

**Trigger.** A user runs a backtest, closes the browser, comes
back an hour later, and the result is gone.

**Symptom.** `GET /api/v1/backtest/results/{id}` returns 404.

**Why.** Results live in an in-process dict with a 1-hour TTL
(`engine/api/routes/backtest.py:_backtest_results`). The
`backtest_results` table exists in the schema but is only
populated by the strategy evaluator for scoring runs.

**Workaround.** Operators who want durable history persist the
JSON response client-side.

**Planned fix.** Persist every completed backtest to
`backtest_results` keyed by id; add a `GET /api/v1/backtest`
list endpoint; add a retention sweep.

### 6. `ohlcv_bars` is not yet a TimescaleDB hypertable

**Trigger.** Storage pressure after a few years of multi-symbol
history.

**Symptom.** The table grows linearly; queries on `(symbol,
timestamp)` slow down beyond a few million rows.

**Why.** The schema is plain Postgres today. The migration to
hypertable + retention policy is pending.

**Workaround.** Operators can convert manually:

```sql
SELECT create_hypertable('ohlcv_bars', 'timestamp',
  if_not_exists => TRUE,
  chunk_time_interval => INTERVAL '1 day');
```

**Planned fix.** Migration `013_ohlcv_hypertable.py` plus an
additive retention sweep in `engine/data/retention_cleanup.py`.

### 7. Rate-limit state is per-process

**Trigger.** Multi-replica deploy with abusive clients.

**Symptom.** Per-IP counters live in-process; a client that
hits multiple replicas can multiply their effective budget by
the replica count.

**Why.** `RateLimitMiddleware` uses a process-local dict.

**Workaround.** Set the rate-limit window conservatively; put
a real rate limiter (Cloud LB / nginx `limit_req`) at the edge.

**Planned fix.** Move counters to Valkey (sliding-window
counter).

### 8. No soft-delete on portfolios

**Trigger.** `DELETE /api/v1/portfolio/{id}`.

**Symptom.** Hard-deletes the row and cascades to positions,
orders, tax lots. No undo.

**Why.** The soft-delete column was deferred; the schema
doesn't have a `deleted_at` on `portfolios`.

**Workaround.** None today. Restore from backup.

**Planned fix.** Migration to add `deleted_at`; filter reads on
`deleted_at IS NULL`; expose an explicit "purge" endpoint for
GDPR deletion requests.

### 9. `nexus dev-token` CLI helper missing

**Trigger.** New operator wants to bootstrap the first admin
user without going through the register flow.

**Symptom.** No CLI today. Operators either register via the
HTTP API directly or shell-script it.

**Why.** The CLI was scoped out of ADR-0002 / 0007.

**Planned fix.** `python -m engine.cli user create` /
`python -m engine.cli user grant-role`. See ADR-0002.

## P2 — quality-of-life

These annoy contributors and operators but don't affect users.

### 10. Sandbox policy is permissive

**Trigger.** A strategy plugin tries to read `/etc/passwd` or
open a TCP socket to an arbitrary host.

**Symptom.** Today, the sandbox policy
([`engine/plugins/sandbox/core/policy.py`](../engine/plugins/sandbox/core/policy.py))
returns a hardcoded allow-list. Network and filesystem
isolation are not enforced.

**Why.** The policy infrastructure is in place; the enforcement
layer (Pyodide / Wasm / subprocess-isolated interpreter) is
not.

**Planned fix.** Two-tier trust model: trusted operators run
strategies in-process; community strategies run in a
sandboxed runtime. See `engine/plugins/trust_levels.py`.

### 11. Frontend dashboard is a skeleton

**Trigger.** Operators expecting a polished UI.

**Symptom.** The React app under `frontend/` has auth, layouts,
and the API wiring, but most pages are placeholders. The
backtest flow works end-to-end; portfolio management and
strategy marketplace views are stubs.

**Why.** The frontend was deprioritised in favour of the engine
core. It's the explicit gap called out in the project status.

**Planned fix.** Frontend MVP scoped in the next milestone.

### 12. MFA secret rotation not supported

**Trigger.** Operator rotates `NEXUS_MFA_ENCRYPTION_KEY`.

**Symptom.** Every existing encrypted TOTP secret becomes
unreadable; users with MFA enabled are effectively locked out.

**Why.** Single-key encryption; no `..._PREVIOUS` slot like
JWT.

**Planned fix.** Add `NEXUS_MFA_ENCRYPTION_KEY_PREVIOUS` and a
background sweep that re-encrypts secrets as users log in.

### 13. No CSV import / export on tax endpoint

**Trigger.** CPA workflows.

**Symptom.** `POST /api/v1/tax/report/{code}/csv` returns a
2-row summary CSV. There's no per-disposal CSV export.

**Why.** Out of scope for the initial tax release (gh#155).

**Planned fix.** `GET /api/v1/tax/report/{code}/csv?detail=1`.

### 14. Many `# noqa: PLR2004` overrides

**Trigger.** Reading the source.

**Symptom.** Lots of magic-number ignores on numeric
constants. Mostly cosmetic, but it makes the lint signal
noisier than it should be.

**Why.** The first pass shipped code fast; named constants
were deferred.

**Planned fix.** Sweep through, name the constants, drop the
ignores. Tracked in `pyproject.toml:[tool.ruff.lint.per-file-ignores]`.

## Not-on-the-roadmap

These are intentional non-goals:

- **Multi-tenant SaaS** — operators run their own deployment.
- **High-frequency / microsecond trading** — the engine is
  async + Python; sub-millisecond order routing is out of
  scope. Use a specialist system for that.
- **Strategy auto-discovery via LLMs** — strategies are
  user-authored code; we will not generate them in-engine.
  LLMs can be called from inside a strategy like any other
  HTTP API.
- **Mobile-native apps** — PWA strategy is in ADR-0003.
