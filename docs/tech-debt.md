# Known limitations and technical debt

An honest list of the things we know are wrong, ordered by impact.
Each entry says **what** is broken, **why** it has not been fixed
yet, and the **cost** of leaving it broken. Use this list when
prioritising a sprint or evaluating whether a feature is safe to
launch against.

Priority tiers:

- **P0 — launch-blocking.** Anything that prevents a sane production
  deploy or has known safety / correctness implications.
- **P1 — operational drag.** Causes incidents or slows development.
  Fix when capacity allows.
- **P2 — long-tail.** Cosmetic, code-quality, or non-blocking UX
  issues.

---

## P0 — Launch-blocking

### 1. Plugin sandbox layer 5 (process isolation) is not implemented

`engine/plugins/sandbox.py:1` documents five containment layers. The
fifth — per-strategy subprocess or container isolation — is the
production target but is not yet wired. Today, a hostile strategy can
still crash the engine process or saturate worker resources.

**Why not fixed yet.** Cold-start latency for a fresh container per
eval is 100–500 ms — too much for a backtest loop. We need a
pre-warmed strategy pool.

**Cost of leaving broken.** The marketplace cannot launch against
untrusted publishers. Strategies from the operator's own authors are
acceptable; third-party is not.

### 2. Live trading is opt-in and partially implemented

`engine/core/execution/live.py` exists but the live broker
integrations are stubs. The kill-switch
(`engine/core/live/kill_switch.py`) is wired; the order placement is
not. There is no production-readiness review for live trading yet.

**Why not fixed yet.** Live money is a regulatory surface. We are
deliberately not shipping it until audit + insurance + broker
paperwork are complete.

**Cost of leaving broken.** Engine is "paper-trade only" in
production. Anyone using it for live trading today is doing so
without support.

### 3. Single-replica, single-DB topology

No HA. A restart drops in-flight requests and loses the in-process
`ConnectionManager` state for WebSockets. A Postgres outage is a
full outage.

**Why not fixed yet.** HA requires cross-replica broadcast for
WebSocket fan-out (the `ConnectionManager` already exposes the shape
that work will consume) and session affinity for rate limit counters.

**Cost of leaving broken.** SLOs reflect this — 99.5% availability
over 28d ≈ 3.6 h/month of downtime tolerance.

### 4. No backup verification for TimescaleDB specifically

The backup scripts (`scripts/ops/pg_basebackup.sh`,
`pg_logical_backup.sh`) handle plain Postgres. PITR via WAL archiving
is supported in principle but the restore path has not been
exercised against a TimescaleDB-enabled instance.

**Why not fixed yet.** The DR drill checklist (`operations/dr-drill-checklist.md`)
has not been run with TimescaleDB.

**Cost of leaving broken.** Operators relying on TimescaleDB
hypertables may discover at restore time that the hypertable
definitions are missing.

---

## P1 — Operational drag

### 5. Backtest results are stored in-process

`engine/api/routes/backtest.py:22` keeps results in `_backtest_results`,
a module-level dict with a TTL of 1 hour. This survives only as long
as the engine process. A restart drops all in-flight results.

**Why not fixed yet.** The TaskIQ result backend already has the data;
the route just needs to read from there instead of the local dict.
~1 day of work.

**Cost of leaving broken.** Polling for a backtest result returns
404 after a deploy. Users have to re-submit.

### 6. Marketplace is stubbed

`engine/api/routes/marketplace.py` returns `not_implemented` for
install, uninstall, and rate. The data model (`installed_strategies`)
exists; the routing does not.

**Why not fixed yet.** Depends on the trust model (signed manifests,
publisher identity) which needs its own ADR.

**Cost of leaving broken.** Strategies can only be installed by
operators editing `strategies/` on disk.

### 7. Multi-currency is unsupported

All disposals in the tax route (`engine/api/routes/tax.py:90`) are
assumed to be in the portfolio's base currency. Mixed-currency
portfolios will produce wrong tax reports.

**Why not fixed yet.** FX rates are a data-provider concern; the
report dispatcher would need an injected rate source.

**Cost of leaving broken.** Non-USD portfolios are not first-class.
Operators in EU / UK need to either restrict to single-currency
portfolios or compute taxes off-engine.

### 8. Refresh-token revocation is per-request

The auth dependency at `engine/api/auth/dependency.py:102` does a DB
hit on every authenticated request to load the user. There is no
caching layer for active sessions. At high QPS this becomes the
bottleneck.

**Why not fixed yet.** Caching invalidation on logout / revoke is
subtle. The right answer is probably a Valkey-backed session cache
with a short TTL.

**Cost of leaving broken.** Auth accounts for ~20% of DB load in
profiling. Scales linearly with authenticated QPS.

### 9. WebSocket manager is single-process

`engine/api/websocket/manager.py:11` explicitly notes the limitation:
broadcasts only reach connections on the same engine replica. A
multi-replica deploy today will miss events for users connected to
other replicas.

**Why not fixed yet.** Needs a Valkey pubsub bridge. The
`ConnectionManager` interface is already shaped for this work.

**Cost of leaving broken.** Force the topology to a single replica,
which is a constraint on item #3 above.

### 10. Per-route rate-limit overrides are hardcoded

`engine/app.py:175` has per-route overrides embedded in the call.
Adding a new override is a code change and a deploy.

**Why not fixed yet.** The cleaner factoring (config-driven
overrides) is a small refactor but not a priority.

**Cost of leaving broken.** Operators cannot tune rate limits without
a code change.

### 11. OAuth role overwrite default is conservative

`NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN` defaults to `false` (SEV-741).
This is safe but means federated IdP role changes do not propagate
without operator opt-in. The result: an IdP-driven promotion from
`user` to `developer` requires the operator to manually update the
DB or flip the flag.

**Why not fixed yet.** The default is intentional. The follow-up is
to expose a one-shot "reconcile roles from IdP" admin endpoint.

**Cost of leaving broken.** SSO-driven role changes need a DBA.

### 12. Alembic downgrade paths are not tested

`engine/db/migrations/versions/*.py` define `downgrade()` functions
but CI never runs them. Operators attempting to roll back via
`alembic downgrade` may hit schema inconsistencies.

**Why not fixed yet.** Forward-fix is preferred; the test
infrastructure to verify downgrades against realistic data does not
exist.

**Cost of leaving broken.** Downgrades are a known unknown. Document
this in the deployment guide.

---

## P2 — Long-tail

### 13. Frontend does not exercise the full API surface

The React dashboard covers auth, portfolio list, backtest submission,
and live data. Marketplace, scoring, webhooks, and DSR routes have no
UI; operators must use the API directly.

### 14. MCP server is missing

The MCP server (referenced in SEV-223 and ADR-0002 alternatives) is
not implemented. The auth model was designed with it in mind
(bearer-token friendly), but the server itself is not built.

### 15. Multi-strategy orchestration is partial

The `installed_strategies` table supports multiple strategies per
portfolio, but the engine runs one strategy at a time per evaluation.
Aggregating signals across strategies is on the roadmap
(`engine/core/signal_aggregator.py` is the start).

### 16. Reference search falls back to Yahoo unconditionally

`engine/api/routes/reference.py:107` queries Yahoo Finance if the
local index returns no matches. There is no operator toggle. If
Yahoo changes its search API or rate-limits aggressively, this
silently degrades.

### 17. Body-size limit is hardcoded at 1 MiB

`engine/app.py:195` sets a global cap. Strategies with very large
config payloads (e.g. pre-trained model weights) cannot be uploaded
through the engine; they must be loaded out-of-band.

### 18. No structured audit log of admin actions

`engine/core/audit_log.py` exists but is used in only a few places.
Promotion / demotion, role grant, legal document update, and DSR
handling are not consistently audited.

### 19. Test coverage pockets below the gate

The 70% CI gate is met in aggregate but several modules sit below
50% (some plugins, the live execution path, options pricing). These
are tagged in `tests/test_coverage_canary.py` so they don't quietly
slip further.

### 20. SDK is in-repo

`sdk/nexus_sdk/` ships as a separate pip-installable wheel
(`sdk/setup.py`) but is versioned and released in lockstep with the
engine. Decoupling the release cycle would let strategy authors
upgrade independently.

### 21. No protobuf / gRPC surface

The engine speaks HTTP/JSON only. Internal high-throughput paths
(strategy ↔ sandbox) are in-process today, but the future
subprocess boundary (item #1) would benefit from protobuf.

### 22. Tasks not retried on transient DB errors

TaskIQ jobs that fail with a transient DB error (connection
dropped mid-job) go straight to the dead-letter state. The
backtest task at `engine/tasks/worker.py:27` catches exceptions
but does not retry.

### 23. Logging volume in DEBUG mode

`NEXUS_LOG_LEVEL=DEBUG` is unusable in production — the SQLAlchemy
engine logger alone produces ~10 MB/min. The sampling defaults
(DEBUG at 1%) are a workaround; the right answer is finer-grained
log namespace control.

---

## Things we considered and rejected

For posterity — choices that came up and were ruled out so future
contributors don't relitigate them:

- **MongoDB for OHLCV.** Worse query planner for time-range joins;
  Postgres + TimescaleDB wins on operational simplicity.
- **Celery.** Sync-by-default; bringing it into an otherwise async
  stack costs more than it saves.
- **GraphQL.** The API surface is small and stable; GraphQL's
  flexibility is not worth the schema-management overhead.
- **HTTP/2 server push.** No measurable benefit for our request
  patterns.
- **Per-tenant DBs (multi-tenant via schema-per-customer).** The
  product is single-tenant-by-deployment; the schema does not model
  tenants.

---

## How to prioritise

- Anything that blocks the marketplace launch → P0 #1, P1 #6.
- Anything that blocks HA → P0 #3, P1 #9, P1 #8.
- Anything that improves incident MTTR → P1 #5, #11, #12.
- Anything that improves operator DX → P1 #10, P2 #18, #23.

When in doubt, the SLO document
([`operations/slos.md`](operations/slos.md)) is the contract. Items
that threaten an SLO move up; items that don't, don't.
