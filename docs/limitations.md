# Known limitations and technical debt

Honest inventory of what's incomplete, what's known to be wrong, and
what is deliberately deferred. Priority uses a coarse 3-tier scale:

- **P0** — security, correctness, or data-loss risk. Fix before next
  release.
- **P1** — blocks a stated roadmap item or hurts users daily. Fix
  within the next 1-3 sprints.
- **P2** — real but not urgent. Backlog.

This list is *not* a replacement for issue tracking. Every item should
either have a tracking issue or an ADR; the doc points at them where
they exist.

## P0 — security & data integrity

None known at the time of writing. The most recent P0-class items
were closed:

- **SEV-741** (silent role escalation in `map_roles`) — fixed in
  `a81578f`. Default behaviour is now `NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN=false`.
- **SEV-507** (security headers + body-size limit + binding to
  `127.0.0.1`) — fixed across `5a065a4` and the security-headers
  middleware. See [`engine/api/security_headers.py`](../engine/api/security_headers.py).
- **SEV-264** (zero-coverage modules) — closed via targeted tests
  (`51f605d` and the suite under `tests/test_coverage_*`).

## P1 — blocks a roadmap item or hurts users daily

### Backtests are in-process, not queued

`POST /api/v1/backtest/run` enqueues onto `BackgroundTasks` (FastAPI's
in-process executor) rather than the TaskIQ broker. Results live in a
module-level dict (`_backtest_results`) with a 1-hour TTL.

**Impact:** backtests cannot survive an API pod restart; horizontal
scaling is impossible; the result endpoint is bound to whichever pod
the user happens to land on next. This is the single biggest
correctness gap in the trading surface.

**Fix direction:** persist `BacktestResult` rows on completion (the
model already exists at
[`engine/db/models.py:BacktestResult`](../engine/db/models.py)) and
enqueue via TaskIQ so the worker pool handles execution. The
`BacktestResultResponse` shape already includes `evaluation` — wire it
up.

### Marketplace is stubbed

`/api/v1/marketplace/install`, `/uninstall/{id}`, and
`/{id}/rate` all return `{ status: "not_implemented" }`. See
[`engine/api/routes/marketplace.py`](../engine/api/routes/marketplace.py).

**Impact:** the React dashboard's marketplace UI cannot function end
to end. Strategies must be installed by editing the filesystem under
`engine/plugins/` and restarting the engine.

**Fix direction:** design the strategy packaging format (zip + manifest
+ signature), wire the sandbox's verification hook
([`engine/plugins/sandbox.py`](../engine/plugins/sandbox.py)) into
install, and implement rating storage. The sandbox is partial — see
the dedicated entry below.

### Plugin sandbox is partial

`engine/plugins/sandbox.py` exists and is used by the scoring
executor, but the broader strategy sandbox does not yet enforce:

- Network egress policy (declared `requires_network` in the manifest
  but not enforced in the runtime).
- CPU / memory caps on the strategy evaluation.
- Filesystem write restrictions.

**Impact:** any strategy installed from the marketplace has implicit
full trust. Operators must treat installed strategies as privileged
code until the sandbox is finished. There is no in-engine warning
about this — the assumption is "operator-only install".

**Fix direction:** finish the policy enforcer in
`engine/plugins/sandbox/core/policy.py` (currently `PLR0911`-ignored
because of the depth of the dispatch) and hook it into
`PluginRegistry.load_strategy`.

### Webhook delivery is in-process

The `WebhookDispatcher` is a synchronous subscriber on the `EventBus`.
A delivery does not go through the TaskIQ broker; a slow receiver
blocks the publishing thread.

**Impact:** a single misbehaving webhook can stall backtest
completion events for every user. The 99% SLO
([operations/slos.md](operations/slos.md)) is held today by the
`max_retries` ceiling, not by real isolation.

**Fix direction:** move delivery to TaskIQ tasks. The `WebhookDelivery`
row already carries every field the worker needs; the dispatcher
becomes an enqueue step.

### Soft-delete on `webhook_configs`

`DELETE /api/v1/webhooks/{id}` is a hard delete with `ON DELETE
CASCADE` to `webhook_deliveries`. Compliance retention requirements
(DSCC, SOX-adjacent) usually need the delivery audit trail to survive
webhook retirement.

**Fix direction:** change the route to set `is_active=false` and add
a `revoked_at` column (it already exists on `api_keys` — mirror that
shape). Cascade stays, but rows are kept until a separate purge job
runs.

### No async tarball export

`POST /api/v1/privacy/export` runs synchronously and returns a single
JSON blob. For a user with thousands of backtests this is too slow
to be a reasonable UX.

**Fix direction:** enqueue a task, write a tarball to object storage,
return a signed download URL via email or the DSR row. GDPR Art. 20
mandates portability but does not mandate synchronous — one month
SLA applies.

## P2 — backlog

### React dashboard MVP

The React dashboard exists under `frontend/` and builds cleanly, but
major flows (live trading, marketplace browsing, DSR management) are
incomplete. Tracked as the "React Frontend / Dashboard" missing
feature in the project audit.

### Tax carry-over state for GB / DE / FR

Only US persists carry-over rows (`engine/core/tax/reports/cgt_carryover.py`).
GB / DE / FR callers must re-submit prior-year summaries to flow loss
forward. Tracked in [api/privacy-legal.md](api/privacy-legal.md).

### Multi-symbol backtests

`BacktestRequest.symbol` is a single string. The SDK contract
(`IStrategy.evaluate` receives a `Portfolio`) supports multi-symbol;
the API does not.

### No live trading route

Live execution backends exist
([`engine/core/execution/live.py`](../engine/core/execution/live.py),
[`engine/core/brokers/`](../engine/core/brokers/)) but no HTTP route
triggers them. Paper broker (`engine/core/brokers/paper.py`,
[`engine/core/execution/paper.py`](../engine/core/execution/paper.py))
is similarly disconnected from the API. The whole "live trading"
capability is built but unwired.

### No admin UI for DSRs

DSRs that arrive out of band (postal, support email) have to be
inserted as rows by hand. See [api/privacy-legal.md](api/privacy-legal.md).

### Engine startup does not enforce schema version

The convention is "migrate before rolling the new image", but
[`engine/app.py:lifespan`](../engine/app.py) does not check that the
schema head matches the code's expected head. A skipped migration
fails at the first query that hits a missing column.

**Fix direction:** add a `schema_version` table written by Alembic and
check it at startup; refuse to serve if the code's expected head is
newer than the DB's.

### `S101` / `PLR0911` ignores in production code

Lint baseline carries targeted ignores for several modules
(`engine/plugins/sandbox/core/policy.py`, `engine/core/risk_engine.py`,
`engine/core/oms/order.py`, …). Most are intentional (state-machine
shape), but a few (`PLR0911` on the sandbox policy) mark genuinely
deep dispatch tables that would benefit from a rewrite.

### Backtest result row is unused by the API

`BacktestResult` exists in the schema, has migration 008 columns
(`composite_score`, `score_breakdown`), but `POST /backtest/run` never
inserts into it. The evaluator produces the data; it just doesn't get
persisted. Related to the P1 "backtests are in-process" item.

### No replay protection on webhook `test` endpoint

`POST /api/v1/webhooks/{id}/test` is not rate-limited beyond the
global default. An attacker with a valid token could amplify outbound
traffic through this endpoint. Mitigated by the global
`NEXUS_RATE_LIMIT_PER_MINUTE`, but a per-route override (like the one
on `/client/errors`) would be defensible.

### Conventional-commits CI gate

`release-please` parses PR titles to decide version bumps. We do not
have a CI check that rejects titles not matching the conventional
commits grammar; mis-titled PRs silently fall into "no release"
buckets. Add a `amannn/action-semantic-pull-request` step to the CI
workflow.

## Limitations by design (non-goals)

These are not bugs and will not be "fixed":

- **Single-tenant per database.** The engine is designed to be run by
  one operator per deployment. Multi-tenant SaaS would require
  row-level security throughout, which is out of scope.
- **Sync TaskIQ dispatch from domain code.** Tasks are enqueued when
  the route handler returns; the domain layer never blocks on a
  worker. This is a deliberate layering choice, not a perf bug.
- **No SSR for the frontend.** The React app is a static SPA; we do
  not run Node.js in production.
- **No mobile app.** ADR-0003 covers the mobile strategy; the short
  version is "responsive web app now, native later if needed".

## Updating this list

- Promote items as their priority changes. A P1 that gets a PR opened
  against it stays P1 until merged.
- When you close an item, **delete it from this doc** in the same PR
  that fixes it. Do not leave "fixed in #xyz" stubs — the git history
  is the record.
- Add new items as you discover them. The list grows; that's healthy.
  What is not healthy is discovering in an incident that something was
  "well-known to be broken" but not written down.
