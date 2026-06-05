# Architecture decisions (ADRs)

This file consolidates the major architecture decisions into one
readable narrative. Individual ADR files in [`../adr/`](../adr/) are
the canonical record; this document is the indexed, digested view for
new readers.

Each entry below uses the standard ADR shape: **Context · Decision ·
Consequences · Alternatives considered**.

---

## ADR-0001 · Language and web framework

**Status:** Accepted. Locked at scaffold time. See
[`../adr/0001-scaffold-tech-choices.md`](../adr/0001-scaffold-tech-choices.md).

### Context

We needed a stack for an algorithmic trading engine that combines:

- Long-running event loops (market data feeds, strategy evaluation).
- CPU-bound numerics (metrics, cost model).
- An HTTP/WebSocket API for the UI and external automation.
- A plugin system that runs third-party Python code.

### Decision

- **Python 3.11+ with `uv`** as the package manager. `uv` gives us a
  deterministic lockfile (`uv.lock`) and 10–100× faster installs than
  pip, which matters for CI parallelism.
- **FastAPI + Pydantic v2** for the HTTP/WebSocket surface. Async-first,
  OpenAPI generation, and Pydantic validation mesh cleanly with the
  SQLAlchemy 2.0 async ORM.
- **SQLAlchemy 2.0 async + asyncpg** for the database layer. Native
  async; `asyncpg` is the fastest Python driver for Postgres.

### Consequences

- The whole stack is async; any blocking I/O in the request path is a
  bug. There is no thread pool bridge to legacy sync code.
- We depend on Pydantic v2's API (not v1). Migrating model code is
  straightforward but unmissable.
- Relying on `uv` means contributors must install it (`curl -LsSf
  https://astral.sh/uv/install.sh | sh`). Standard `pip` works for the
  SDK only.

### Alternatives considered

- **Node + TypeScript** — better fit for the WebSocket / streaming
  side, but Python dominates the quant ecosystem (NumPy, Polars,
  scikit-learn, PyTorch, the entire LLM tooling stack). Strategies are
  written in Python by audience default.
- **Go** — excellent for the hot path, but the strategy-plugin author
  audience is Python-first. Embedding Python strategies in a Go host
  costs more than it saves.

---

## ADR-0002 · Database choice

**Status:** Accepted.

### Context

The engine stores three distinct workloads:

1. Relational data (users, portfolios, orders, tax lots, audit logs).
2. Time-series OHLCV bars at second-to-day granularity.
3. Ephemeral state (rate-limit counters, broker sessions, task queue).

### Decision

- **PostgreSQL 16 + TimescaleDB extension** for both relational and
  time-series data. Hypertables on `ohlcv_bars` give compression and
  continuous aggregates without a separate TSDB.
- **Valkey 8** (Redis-compatible fork) for ephemeral state. Used by
  the rate limiter, TaskIQ broker, and result backend.
- **Alembic** for schema migrations, configured for async engine.
  All migrations live in `engine/db/migrations/versions/`.

### Consequences

- No SQLite for local dev — TimescaleDB requires Postgres. Contributors
  must `docker compose up -d db` for any real-data workflow. Pure unit
  tests use an in-memory SQLite via `aiosqlite` where the schema
  permits.
- Backup and restore must use `pg_basebackup` (physical) for
  TimescaleDB-aware restores. See
  [`../operations/backup-and-recovery.md`](../operations/backup-and-recovery.md).

### Alternatives considered

- **InfluxDB** for time-series — separate store to operate. Adds
  cross-store joins and a second backup procedure.
- **DuckDB (embedded)** — tempting for analytics, but not appropriate
  as the primary write store for an OLTP workload.

---

## ADR-0003 · Authentication model

**Status:** Accepted. Implementation tracked across SEV-233, SEV-741,
SEV-507, gh#86, gh#94, gh#126. See
[`../adr/0002-auth-rbac.md`](../adr/0002-auth-rbac.md) for the
original proposal.

### Context

The engine is intended to be deployable beyond localhost. We need
authentication that:

- Works for human users (browser + interactive login).
- Works for headless automation (CI scripts, scheduled jobs).
- Composes with enterprise SSO without locking it in.
- Enforces a sensible permission model without per-route spaghetti.

### Decision

A **pluggable auth provider registry** plus a **two-axis authorization
model** (role + scope).

#### Provider registry

`AuthProviderRegistry` (`engine/api/auth/registry.py`) holds zero or
more `AuthProvider` implementations, registered at startup from
`NEXUS_AUTH_PROVIDERS`. Shipped providers:

| Provider  | Use case                                              |
|-----------|-------------------------------------------------------|
| `local`   | Email + bcrypt; default; always available.            |
| `google`  | OAuth2 + PKCE. Requires `NEXUS_GOOGLE_CLIENT_*`.      |
| `github`  | OAuth2 + PKCE. Requires `NEXUS_GITHUB_CLIENT_*`.      |
| `oidc`    | Generic OpenID Connect (Okta, Keycloak, Auth0).       |
| `ldap`    | Bind DN + search. Requires `python-ldap[ldap]` extra. |

Adding a new provider is one module in `engine/api/auth/`.

#### Token model

- **Access token** — JWT, HS256, 1-hour TTL by default
  (`NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES`). Claims: `sub`, `email`,
  `role`, `provider`.
- **Refresh token** — opaque, 32-byte URL-safe, hashed at rest in
  `refresh_tokens`. Rotated on every refresh; reuse detection revokes
  every active session for the user
  (`engine/api/routes/auth.py:202`).
- **API key** — `nxs_<prefix>_<secret>`, bcrypt-hashed. Prefix is the
  DB lookup key; secret never stored. Issued once via
  `POST /api/v1/auth/api-keys`.
- **WebSocket auth** — first message must be `{"type": "auth", "token":
  "..."}`; query-string JWTs are explicitly **not** supported because
  they end up in proxy logs.

#### Role hierarchy

Defined in `engine/api/auth/dependency.py:27`:

```
viewer (0) → user (1) → retail_trader (2) → quant_dev (3) →
developer (4) → portfolio_manager (5) → admin (6)
```

Higher roles satisfy lower checks. `require_role("developer")` is the
common gate for write paths; admin-only routes use `require_role("admin")`.

#### API-key scopes

When a request authenticates via an API key, the scope declared on
that key is enforced:

| Scope  | Allows                                              |
|--------|-----------------------------------------------------|
| `read` | `GET` / `HEAD` only.                                |
| `trade`| `POST` / `PUT` / `PATCH` for backtest, portfolio, webhooks, market data. |
| `admin`| Equivalent to the `admin` role; supersedes both.    |

JWT-authenticated requests (interactive sessions) **bypass** scope
checks — they are gated by role instead. Both gates can fire on the
same route.

#### MFA

TOTP (RFC 6238) only. Secrets encrypted at rest with a Fernet key
(`NEXUS_MFA_ENCRYPTION_KEY`); 10 single-use backup codes per
enrollment. Login returns a challenge token (`MFARequiredResponse`)
when MFA is enabled; `POST /api/v1/auth/mfa/verify` exchanges the
challenge + a 6-digit code for the real token pair.

### Consequences

- All API consumers (frontend, SDK, CLI) must obtain a token. The
  `nexus dev-token` helper exists for first-day DX.
- JWT secret is a deployment-critical secret. Rotating it requires
  setting `NEXUS_SECRET_KEY_PREVIOUS` so outstanding tokens verify
  during the rotation window.
- Auth-related fields are scattered across the `users` table
  (`mfa_enabled`, `mfa_secret_encrypted`, `mfa_backup_codes`,
  `auth_provider`, `external_id`); future SSO work should consider a
  separate `auth_identities` table.

### Notable security fixes

- **SEV-741** (`engine/api/auth/local.py`) — fixed silent role
  escalation where federated login could overwrite a local role.
  Added `NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN` (default `False`).
- **SEV-507** — refresh-token replay detection. If a *used* refresh
  token is presented again, every active session for the user is
  revoked atomically (`engine/api/routes/auth.py:202`).

### Alternatives considered

- **Session cookies only (no JWT)** — simpler but does not compose
  with the planned MCP server or SDK use cases that need bearer auth.
- **External OAuth2 proxy (oauth2-proxy / Pomerium)** — punts auth to
  infra. Reasonable for self-hosted, but does not help embedded SDK
  / CI use cases. Plugin design lets operators wire one in later.
- **Auth0 / Clerk / Stytch** — fastest path, but introduces vendor
  lock-in for an OSS-trending project.

---

## ADR-0004 · Plugin sandbox model

**Status:** Accepted (layers 1–4). Layer 5 (process isolation) is on
the roadmap.

### Context

Strategies are third-party Python code. Even with the marketplace not
yet open, the engine must assume a strategy can be hostile or buggy.

### Decision

Five containment layers, applied in order at
`engine/plugins/sandbox.py:1`:

1. **Import restrictions** — `RestrictedImporter` blocks
   `subprocess`, `socket`, `ctypes`, `multiprocessing`, `ssl`,
   pickling, and a deny-list of dunder accesses (`__subclasses__`,
   `__globals__`, `__code__`, etc.).
2. **Network whitelist** — `SandboxedHttpClient` only allows URLs
   declared in the manifest's `network.allowed_endpoints`. Strategies
   that did not declare a network requirement cannot make outbound
   HTTP at all.
3. **Resource limits** — Linux `resource.RLIMIT_AS` enforces the
   memory cap (default 512 MB). CPU seconds are bounded by the
   per-call timeout in the manifest's `resources.max_cpu_seconds`.
4. **Filesystem isolation** — each `evaluate()` call gets a fresh
   `tempfile.TemporaryDirectory`; bundled artifacts are mounted
   read-only.
5. **Process isolation** — *not yet implemented.* The production
   target is one subprocess (or container) per strategy invocation,
   with serialised `MarketState` in and `Signal[]` out.

The sandbox is mandatory: there is no opt-out flag today.

### Consequences

- Strategies that need `pandas`, `numpy`, `polars`, or `httpx` work
  (with the httpx wrapper). Strategies that try to import `subprocess`
  or `socket` fail at import time.
- A strategy that requires network must declare it in
  `strategy.manifest.yaml`. Operators review the manifest at install
  time (the marketplace UI surfaces it).
- The eval timeout is a hard ceiling. A strategy that exceeds it is
  killed and the eval row marked `failed`. There is no "best effort"
  mode.

### Alternatives considered

- **No sandbox, trust the operator** — viable for single-tenant self
  host, but breaks the moment a marketplace lands.
- **Pure container isolation** — strongest, but adds cold-start
  latency the backtest loop cannot afford today. Plan is to add it as
  layer 5 once we have a strategy pre-warm pool.

---

## ADR-0005 · Cost model and tax integration

**Status:** Accepted.

### Context

Backtests that ignore costs are systematically misleading. The
engine's core pitch is that cost-aware backtests produce strategies
that survive contact with production.

### Decision

- **`ICostModel` is an input to `IStrategy.evaluate()`**, not a
  post-processor. Strategies receive `portfolio`, `market`, `costs`
  and decide whether a trade is worth it after friction.
- The default `DefaultCostModel` (`engine/core/cost_model.py:1`)
  covers commissions, spread, slippage (linear or square-root market
  impact), regulatory fees, and overnight financing for short
  positions.
- Tax lots are tracked per-portfolio in `tax_lot_records`. The
  `TaxMethod` enum (FIFO / LIFO / HIFO) is a portfolio-level setting.
- Wash sale rules (US) are enforced at disposal time
  (`engine/core/tax/wash_sale.py:1`). The 30-day window matches IRS
  §1091.
- Tax reports are generated by jurisdiction via a dispatcher
  (`engine/core/tax/reports/dispatcher.py:1`). Supported today: US
  (Form 8949 / Schedule D / Form 6781 / §1256 carryback), GB (HMRC
  CGT), DE (KESt), FR (PFU). Adding a jurisdiction is one module
  under `engine/core/tax/jurisdictions/` plus a report class under
  `engine/core/tax/reports/`.

### Consequences

- Strategy authors must handle a non-trivial `costs` object. The SDK
  ships a `NullCostModel` for the case where costs are explicitly
  irrelevant.
- The tax dispatcher is jurisdiction-neutral; the HTTP layer
  (`POST /api/v1/tax/report/{code}`) dispatches by two-letter code.
  Adding a country is one Python module + one dispatcher entry.
- Multi-currency is not supported. All disposals are assumed to be in
  the portfolio's base currency.

### Alternatives considered

- **Post-hoc cost deduction** — simpler but produces optimistically
  biased backtests. Rejected for that reason.
- **External tax engine (TurboTax / Avalara integration)** — out of
  scope. We model the math; filing is the user's problem.

---

## ADR-0006 · Background task queue

**Status:** Accepted.

### Context

Backtests are 5–500 second computations. Running them in the request
handler ties up a uvicorn worker and times out browser requests.

### Decision

- **TaskIQ** as the task framework — async-native, multi-broker,
  supports result backends and middlewares.
- **Valkey-backed `ListQueueBroker` + `RedisAsyncResultBackend`** as
  the broker. Already a dependency for rate limiting; one less moving
  part.
- Two execution paths:
  - **`BackgroundTasks`** (FastAPI native) — for <30 s jobs that need
    no separate worker. Used by `POST /api/v1/backtest/run` today.
  - **TaskIQ worker** — for everything else. Started via
    `taskiq worker engine.tasks.worker:broker`.

### Consequences

- The HTTP route returns `202 Accepted` immediately; the client polls
  a separate route or subscribes to a WebSocket topic to see the
  result. Frontends must handle "still running" as a first-class UI
  state.
- The TaskIQ broker is shared with the rate-limit store. A
  misbehaving worker can saturate Valkey; monitor Valkey memory and
  connection count alongside worker lag.

### Alternatives considered

- **Celery** — sync-first, requires bringing threads into an
  otherwise async stack. TaskIQ is the closer fit.
- **RQ / arq / dramatiq** — arq is async but smaller ecosystem;
  dramatiq is sync. TaskIQ's middleware API also gives us
  correlation-id propagation for free, which we'd otherwise build.

---

## ADR-0007 · Observability stack

**Status:** Accepted.

### Context

We need structured logs, distributed traces, and metrics that survive
in a self-hosted deployment without forcing a specific vendor.

### Decision

- **`structlog`** — JSON in production, console in dev. Reserved
  kwarg is `event_type=` (not `event=`) to avoid collision with
  Pydantic.
- **OpenTelemetry SDK** with OTLP exporter — vendor-neutral tracing.
  Auto-instruments FastAPI and SQLAlchemy.
- **Pluggable metrics backend** via the `MetricsBackend` interface
  (`engine/observability/metrics.py`). Default is `NullBackend`;
  production wires `PrometheusBackend`
  (`engine/observability/prometheus.py`) at app startup.
- **Sentry SDK** opt-in via `NEXUS_SENTRY_DSN`.

### Consequences

- Operators who want anything other than Prometheus can implement
  `MetricsBackend` and call `set_metrics()` after `create_app()`.
- The OTel exporter is enabled by `NEXUS_OTLP_ENDPOINT`. Setting it
  to empty disables tracing without code changes.
- SLO alert rules live in `observability/prometheus/slo-rules.yaml`,
  not in code — they're an operator concern.

### Alternatives considered

- **statsd / DogStatsD** — fine protocol, but every backend needs a
  sidecar. OTel + Prometheus keeps the moving parts inside the
  engine.
- **Datadog / New Relic** — vendor-specific. We emit OTel so anyone
  can wire these in as collectors.

---

## ADR-0008 · API versioning

**Status:** Accepted.

### Context

The HTTP API is consumed by the in-repo frontend, third-party SDKs,
and operator scripts. Breaking changes need a sane migration path.

### Decision

- All routes are prefixed `/api/v1/`. The prefix is set per-router in
  `engine/api/router.py:26`.
- A new major version (v2) ships as a parallel router. Old routers
  stay mounted until their deprecation window closes.
- Backward-compatible changes (new optional fields, new endpoints) do
  not bump the major.
- Breaking changes are advertised in `CHANGELOG.md` and via the
  `GET /api/v1/system/status` route's `engine_version` field.

### Consequences

- We live with our past mistakes in public. The v1 router has
  endpoints that return `not_implemented` stubs (marketplace install,
  uninstall, rate) — those exist so the OpenAPI surface is stable
  for SDK consumers.

### Alternatives considered

- **No version prefix** — rejected; the SDK ships at 0.1.0 and we
  cannot recall published wheels.
- **Header-based versioning** (`Accept: application/vnd.nexus.v2+json`)
  — elegant but invisible in proxy logs and harder for operators to
  test.

---

## ADR-0009 · Legal acceptance gate

**Status:** Accepted. Tracked at gh#86, gh#94.

### Context

The marketplace model only works if users have accepted the current
Terms / EULA / Risk Disclaimer. We also need an audit trail of *who
accepted what, when, from which IP*.

### Decision

- Legal documents live as Markdown files under `legal/*.md` with
  YAML front matter (`slug`, `version`, `effective_date`,
  `requires_acceptance`).
- On startup, `engine/legal/sync.py:43` upserts rows in
  `legal_documents` from the directory.
- `LegalAcceptance` rows record every acceptance event with user id,
  document slug + version, IP, user-agent, timestamp, and context
  (`onboarding`, `upgrade`, `login`).
- Routes that touch money or strategy execution are gated by
  `require_legal_acceptance` (mounted at the router level in
  `engine/api/router.py:39`). Unaccepted → `403 Forbidden`.

### Consequences

- Document versions are immutable. To update Terms, you ship a new
  Markdown file with a bumped `version` and let the sync code do its
  job.
- The `LegalAcceptance.user_id` FK uses `ondelete=RESTRICT` with
  `DEFERRABLE INITIALLY DEFERRED` — we cannot lose audit rows when a
  user is deleted. Deletion is deferred to the grace window in the
  DSR flow (`engine/privacy/deletion.py`).

---

## Pending / proposed

- **ADR-0010 (proposed): Multi-region deployment.** Currently a single
  Postgres + single Valkey. HA / multi-region is on the roadmap.
- **ADR-0011 (proposed): Strategy marketplace trust model.** The
  marketplace endpoints return stubs today; the trust model (signed
  manifests, publisher identity, operator opt-in) needs its own ADR
  before launch.
