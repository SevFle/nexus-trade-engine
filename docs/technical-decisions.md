# Technical Decisions

This document summarizes the major architectural decisions and the reasoning
behind them. Full ADRs live in [adr/](adr/) — this page provides a condensed
view with cross-references.

---

## TD-01: Python + FastAPI for the Engine

**Decision:** Python 3.11+ with FastAPI as the web framework.

**Why:** Algorithmic trading demands rapid iteration on strategy logic. Python's
ecosystem (NumPy, Polars, scikit-learn, LLM APIs) gives strategy developers
the broadest surface area. FastAPI was chosen over Flask/Django because:

- Native `async/await` with zero callback spaghetti — critical for concurrent
  market data streams and WebSocket fan-out.
- Pydantic v2 integration gives request/response validation and OpenAPI docs
  with no manual schema maintenance.
- Dependency injection system (`Depends`) composes naturally for auth, DB
  sessions, and legal-gate checks.

**Trade-off:** Python's GIL limits CPU-bound parallelism within a single
process. We accept this because the hot path (order pipeline) is I/O-bound
(network calls to brokers/data providers). CPU-heavy work (backtests) is
offloaded to TaskIQ workers that can scale horizontally.

**See also:** [ADR-0001](adr/0001-scaffold-tech-choices.md)

---

## TD-02: SQLAlchemy 2.0 Async + asyncpg

**Decision:** SQLAlchemy 2.0's async ORM with asyncpg driver, PostgreSQL 16
with TimescaleDB extension.

**Why:**
- SQLAlchemy 2.0's `mapped_column` + `DeclarativeBase` provides type-safe
  models without legacy `Column()` boilerplate.
- asyncpg is the fastest PostgreSQL driver for Python — benchmarks show
  2-3x throughput vs psycopg3 in async mode.
- TimescaleDB gives us hypertables for OHLCV bars and portfolio snapshots
  with automatic compression and retention policies, without leaving the
  Postgres ecosystem.

**Trade-off:** No SQLite for local development. TimescaleDB requires Docker.
This is acceptable because `docker compose up db` is a one-command operation
and the dev compose file handles it automatically.

**See also:** [ADR-0001](adr/0001-scaffold-tech-choices.md), [database.md](architecture/database.md)

---

## TD-03: Pluggable Auth with JWT Default

**Decision:** `AuthProviderRegistry` pattern with 7 pluggable providers
(Local, Google, GitHub, OIDC, LDAP) behind a single `Depends(get_current_user)`
FastAPI dependency. Default backend is JWT with bcrypt password hashing.

**Why:** Different deployments need different auth: hobbyists want email/password,
enterprises want OIDC/LDAP, and some operators want to put an OAuth2 proxy
in front and have the engine trust headers. The registry pattern
(`engine/api/auth/registry.py`) lets operators enable providers via the
`NEXUS_AUTH_PROVIDERS` env var without code changes.

JWT was chosen over session cookies because:
- Token-based auth composes with the planned SDK and CLI clients.
- No server-side session store needed — stateless verification.
- Refresh tokens are stored hashed in `refresh_tokens` table with atomic
  rotation to detect token replay.

**Trade-off:** JWT revocation requires a DB lookup per request. For v1 this
overhead is acceptable; if it becomes a bottleneck, we'll cache revocation
checks in Valkey with a short TTL.

**See also:** [ADR-0002](adr/0002-auth-rbac.md)

---

## TD-04: TaskIQ Over Celery

**Decision:** TaskIQ with `taskiq-redis` broker for background tasks.

**Why:** Celery is the traditional choice but carries significant baggage:
- Celery's prefetch/pool model doesn't natively support `asyncio`.
- TaskIQ is async-native, integrates with FastAPI via `taskiq-fastapi`,
  and uses a cleaner broker abstraction.
- The Valkey broker (`taskiq-redis`) reuses our existing Valkey instance
  — no need for a separate RabbitMQ/Redis broker.

**Trade-off:** TaskIQ is a younger project with a smaller community. The API
surface we use (enqueue task, check result) is simple enough that migration
to an alternative ( Dramatiq, ARQ) would be straightforward if needed.

---

## TD-05: Cost Model as First-Class Input

**Decision:** The `ICostModel` interface is passed into every strategy's
evaluate cycle. Strategies check costs *before* emitting signals.

**Why:** Most backtesting frameworks deduct costs from returns after the fact.
This creates a dangerous gap: a strategy that looks profitable pre-cost may
be unprofitable post-cost, but the developer doesn't discover this until
production. By injecting the cost model as input:

1. Strategies can reject trades where `costs.estimate_pct() > threshold`.
2. The backtest runner produces `cost_drag_pct` — the percentage of returns
   consumed by costs.
3. Cost-aware strategies are more likely to survive the backtest-to-live
   transition.

**Trade-off:** Strategy code is slightly more complex. We accept this because
the alternative (surprise cost drag in production) is worse.

---

## TD-06: Plugin Filesystem Discovery

**Decision:** Strategies are discovered by scanning `./strategies/*/manifest.yaml`
at startup. No central registry or package index required for local use.

**Why:**
- Zero-friction onboarding: drop a directory with `manifest.yaml` + `strategy.py`
  and the engine picks it up on next reload.
- The marketplace route (`/api/v1/marketplace`) provides the social layer
  for sharing strategies, but local discovery doesn't depend on it.
- Each manifest declares permissions (network, filesystem, resources) that
  the sandbox enforces.

**Trade-off:** No versioning or dependency resolution for plugins. Two strategies
that depend on conflicting library versions cannot coexist in the same process.
This is acceptable for v1 — the sandbox's import restrictions mitigate the
blast radius.

---

## TD-07: Multi-Jurisdiction Tax Engine

**Decision:** Separate tax summarizers per jurisdiction behind a `TaxDispatcher`,
invoked via a single API endpoint (`POST /api/v1/tax/report/{code}`).

**Why:** Tax rules are deeply jurisdiction-specific (wash sales in the US,
bed-and-breakfast in the UK, PFU flat tax in France). A single unified model
would be impossibly complex. Instead:

- Each jurisdiction implements a `summarize()` method that returns a
  frozen dataclass with jurisdiction-specific fields.
- The dispatcher routes by two-letter code — one endpoint instead of one
  per jurisdiction.
- Wash sale detection is US-specific and lives in the US summarizer.

**Trade-off:** Adding a new jurisdiction requires a new module + registration.
The dispatcher pattern keeps the surface area manageable.

---

## TD-08: Valkey (Redis-Compatible) Over Redis

**Decision:** Valkey 8 as the cache/event bus broker, via the `valkey` Python
client.

**Why:** Valkey is a drop-in Redis fork maintained by the Linux Foundation.
It provides:
- Redis-compatible API (same protocol, same client libraries).
- No proprietary module dependencies.
- Active community governance.

The engine uses Valkey for three purposes:
1. **TaskIQ broker** — task queue for backtests.
2. **Event bus pub/sub** — cross-process event delivery.
3. **Result cache** — short-lived backtest result storage with automatic expiry.

**Trade-off:** Some Redis-specific features (Redis Modules, Redis Stack) may
not be available. We don't use any of them.

---

## TD-09: Structured Logging with structlog

**Decision:** `structlog` with JSON output in production, console (human-readable)
in development. All log events use `event_type=` instead of `event=` (reserved
by structlog).

**Why:**
- JSON logs parse cleanly into ELK/Loki/Datadog without regex grokking.
- Bound context (request_id, user_id, strategy_id) propagates automatically.
- The `event_type` convention avoids structlog's reserved `event` parameter
  while maintaining semantic clarity.

**Trade-off:** Developers must remember to use `event_type=` not `event=`.
Lint rules catch this in CI.

---

## TD-10: Legal Gates on Sensitive Routes

**Decision:** Routes for backtesting, scoring, market data, and portfolio
management require legal document acceptance via `Depends(require_legal_acceptance)`.

**Why:** Trading platforms carry legal obligations (risk disclaimers, terms
of service, privacy policy). Rather than checking acceptance on every request,
we use a FastAPI dependency that:
1. Checks the `legal_acceptances` table for the current user.
2. Returns 403 with the list of unsigned documents if any required doc is
   missing.
3. Caches the acceptance check per-request to avoid N+1 queries.

This ensures new users complete the legal flow before accessing trading
features, without scattering acceptance checks across route handlers.

**See also:** [ADR-0002](adr/0002-auth-rbac.md), [legal/processors.md](legal/processors.md)
