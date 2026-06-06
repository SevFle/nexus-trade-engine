# Technical decisions

This is the index of architectural decisions for Nexus Trade Engine.
Each entry is a short summary; the full ADR lives in
[`/docs/adr/`](../adr/) when one exists. New entries go at the bottom.

The format follows Michael Nygard's ADR template: **Context**, **Decision**,
**Consequences**. Read the [`adr/template.md`](../adr/template.md) before
adding a new one. Decisions that are obvious ("we use SQL") do not need
an ADR; decisions where reasonable engineers would disagree do.

For dates, use the quarter the decision landed in `main`, not the date
someone wrote the doc — that's what reviewers care about when reading
history.

---

## Summary table

| # | Decision | Status | ADR |
|---|---|---|---|
| 1 | Python 3.11+, FastAPI, asyncpg, TimescaleDB, TaskIQ | Accepted | [0001](../adr/0001-scaffold-tech-choices.md) |
| 2 | Local + OAuth (Google/GitHub) + OIDC + LDAP, JWT + API keys, RBAC | Accepted | [0002](../adr/0002-auth-rbac.md) |
| 3 | Web-first; mobile via PWA, no native app in the near term | Accepted | [0003](../adr/0003-mobile-app-strategy.md) |
| 4 | Plugin-first strategy architecture with a five-layer sandbox | Accepted | — |
| 5 | Three execution modes (Backtest / Paper / Live) via swappable backend | Accepted | — |
| 6 | Cost model passed into `evaluate()` as an `ICostModel` | Accepted | — |
| 7 | Single Postgres database + TimescaleDB; no sharding, no MongoDB | Accepted | — |
| 8 | Webhooks signed with HMAC-SHA256, retries with capped exponential backoff | Accepted | — |
| 9 | Per-strategy event-sourced `Order` in `core/oms/`, separate from DB row | Accepted | — |
| 10 | JSONB for semi-structured fields; no JSON column type | Accepted | — |
| 11 | Strategy scoring via z-score normalisation + weighted composite | Accepted | — |
| 12 | Defense-in-depth: no implicit role escalation from upstream IdPs (SEV-741) | Accepted | — |

---

## 1. Python 3.11+, FastAPI, asyncpg, TimescaleDB, TaskIQ

**Context.** Backtest and live-trading workloads are I/O-bound (market
data, broker APIs) interleaved with CPU bursts (Pandas/Polars-style
analytics). We wanted async end-to-end so a slow broker doesn't block
other requests, but we also wanted strong typing and a single language
for both the engine and the SDK.

**Decision.** Python 3.11+ as the implementation language. FastAPI on
the boundary, asyncpg + SQLAlchemy 2.0 async for persistence, TaskIQ
over Valkey for the async job queue, TimescaleDB for OHLCV hypertables.
Ruff + basedpyright enforce style and types in CI.

**Consequences.**
- No blocking I/O is allowed in route handlers; reviewers reject
  `requests.get`, `time.sleep`, sync `psycopg2`, etc. on sight.
- TimescaleDB requires Postgres — there is no SQLite-fallback mode for
  prod; the dev compose has to bring up Postgres.
- TaskIQ was chosen over Celery specifically because Celery's async
  story is half-built; TaskIQ is async-native but newer, with a smaller
  community.

## 2. Local + OAuth + OIDC + LDAP, JWT + API keys, RBAC

**Context.** Operators want one of three deployment shapes: (a) local
accounts only (self-hosted personal), (b) SSO via an existing IdP
(company), (c) mixed. Strategies and webhooks need machine credentials.

**Decision.** Five pluggable auth providers (`local`, `google`, `github`,
`oidc`, `ldap`), selected at startup via `NEXUS_AUTH_PROVIDERS`. JWTs
for human sessions (HS256, rotating key support via `secret_key_previous`),
`nxs_<env>_<32hex>` API keys for machines. Authorisation is RBAC
(`viewer → user → retail_trader → quant_dev → developer → portfolio_manager → admin`)
plus an orthogonal scope hierarchy for API keys (`read < trade < admin`).
See `engine/api/auth/dependency.py:27-35,160`.

**Consequences.**
- JWT users are gated by role only; the scope hierarchy does not apply.
  API keys are gated by scope. This means a JWT-logged-in `user` can do
  anything a `trade`-scoped key can — by design, since the UI flow
  needs to call those endpoints.
- MFA is opt-in per user; TOTP secrets are Fernet-encrypted at rest
  with `NEXUS_MFA_ENCRYPTION_KEY`. If that key is empty at startup,
  MFA enrollment is disabled but the rest of auth still works.
- Refresh tokens are stored as a SHA-256 hash (intentionally fast —
  the threat model is DB exfiltration, not online brute force; see
  `engine/api/auth/jwt.py:62-68` for the security note).

## 3. Web-first; mobile via PWA

**Context.** The dashboard needs to render charts (Recharts) and tables
across desktop and tablet. A native mobile app would mean doubling the
API surface, the auth flow, the build pipeline.

**Decision.** React 18 + Vite + Tailwind on the web. Add PWA manifest
+ service worker when mobile is a real ask. No React Native, no
Flutter, no native SDK.

**Consequences.** Mobile parity is a CSS / responsive-design problem,
not a release-engineering problem. Native-only features (push, biometric
auth) are deferred until someone asks for them with a budget.

## 4. Plugin-first strategy architecture with a five-layer sandbox

**Context.** Strategies are arbitrary code; if we let them call
`subprocess.run` or hit random endpoints, every strategy author
becomes a supply-chain attack surface. But we also want strategies to
be able to call LLMs and other HTTP services when their author
declares them.

**Decision.** Every strategy is a directory under `strategies/` with
a `strategy.manifest.yaml` (or legacy `manifest.yaml`) and a `strategy.py`
exposing `Strategy`. The engine loads it via `PluginRegistry`, then
runs `evaluate()` inside a `StrategySandbox` with five layers
(`engine/plugins/sandbox.py`):

1. **Imports** — `RestrictedImporter` blocks ~50 dangerous stdlib
   modules (filesystem, networking primitives, introspection,
   persistence, debugger).
2. **Network** — `SandboxedHttpClient` whitelists only the manifest's
   `network.allowed_endpoints`.
3. **Resources** — `RLIMIT_AS` for memory cap, `RLIMIT_NOFILE ≤ 64`.
4. **Filesystem** — temp working dir; `builtins.open` intercepted;
   read-only inside the workdir; writes blocked.
5. **Process isolation** — *not yet implemented* (see
   [`operations/known-issues.md`](../operations/known-issues.md)).

**Consequences.**
- Concurrent evaluations are serialised through a global
  `asyncio.Lock` (`engine/plugins/sandbox.py:65`) because the import
  hook is process-wide. This is fine for backtests; it's a bottleneck
  for live trading that we will revisit.
- Strategies that genuinely need a blocked module (e.g. `pickle` for
  model weights) cannot run in the sandbox. The workaround is to load
  the model in the strategy's `initialize()` and pass artefacts as
  in-memory tensors — the sandbox allows `initialize` to run before
  the import hook is installed.
- Layer 5 (process isolation) is the long-term plan. Until then, a
  malicious strategy can in principle escape via C extensions or
  filesystem race conditions; we accept the risk for a single-operator
  deployment but it's a blocker for multi-tenant hosting.

## 5. Three execution modes via swappable backend

**Context.** A strategy that backtests well should not need a
re-write to paper-trade or go live. The differences between modes are
fill semantics, latency, and broker-specific quirks — not strategy
logic.

**Decision.** `ExecutionBackend` ABC with three implementations
(`engine/core/execution/`): `BacktestBackend` (deterministic when
seeded, configurable fill probability), `PaperBackend` (live prices +
random slippage), `LiveBackend` (**stub** — returns failure with a
"not yet implemented" message). The `Order` passed in is the
event-sourced one from `engine/core/oms/order.py`, not the legacy one.

**Consequences.**
- Live trading is not yet shippable. The `LiveBackend.execute` stub
  at `engine/core/execution/live.py:55-59` is the gating item.
- The legacy `Order` type from `engine.core.order_manager.Order` is
  still imported in `execution/base.py` type hints. Cleaning this up
  is a tracked item (see known-issues).
- Paper backend uses `random.uniform` for slippage jitter; that's fine
  for UX but it means paper-trade results are not reproducible across
  runs without seeding.

## 6. Cost model passed into `evaluate()` as an `ICostModel`

**Context.** Cost-aware backtesting requires the strategy to know
*current* costs at decision time. The alternative — letting strategies
call `broker.estimate_fees()` directly — couples them to the broker
and prevents backtesting against historical cost regimes.

**Decision.** Every `evaluate(portfolio, market, costs)` receives an
`ICostModel` instance (`engine/core/cost_model.py:107`). The default
implementation (`DefaultCostModel`) is parameterised for US equities;
jurisdictions and asset classes are handled by passing a different
implementation. Tax-aware methods (`estimate_tax`, `check_wash_sale`,
`estimate_dividend_tax`) are part of the interface, not bolted on.

**Consequences.**
- The cost model is **injected, not imported** — strategies that
  hard-code `DefaultCostModel()` defeat the abstraction.
- Live mode will need a broker-specific cost model that reads
  actual fee schedules; that class doesn't exist yet.
- Slippage scales with `quantity / avg_volume`
  (`engine/core/cost_model.py:226-230`); for low-liquidity names this
  dominates the spread term, which is the correct behaviour but can
  surprise authors of strategies that "look profitable" in a
  backtest without realistic volume assumptions.

## 7. Single Postgres + TimescaleDB; no sharding, no MongoDB

**Context.** The engine models one operator's data. Sharding,
multi-tenant isolation, and event-sourced event stores were all
out of scope for v1. We needed transactional consistency between
`orders`, `tax_lot_records`, `legal_acceptances`, and `users`.

**Decision.** One Postgres database (TimescaleDB extension enabled).
No MongoDB, no Cassandra, no separate event store. Async access via
SQLAlchemy 2.0 + asyncpg. UUIDs for primary keys. `JSONB` for
semi-structured fields (`backtest_results.metrics`,
`webhook_configs.event_types`, etc.).

**Consequences.**
- Backups are one `pg_dump`; restores are one `pg_restore`. RPO/RTO
  live in [`operations/backup-and-recovery.md`](../operations/backup-and-recovery.md).
- We use Postgres-specific features (`JSONB`, `ON CONFLICT DO UPDATE`,
  deferred FK constraints). Switching databases would be a rewrite,
  not a port.
- TimescaleDB is optional but assumed — operators can run vanilla
  Postgres but give up compression and continuous aggregates on
  OHLCV. The engine doesn't crash without TimescaleDB; it just uses
  more disk.

## 8. Webhooks signed with HMAC-SHA256, retries with capped exponential backoff

**Context.** Outbound webhooks fail in two modes: transient (5xx,
timeouts, 429) and permanent (4xx, DNS, bad URL). Operators want
retries on the first class and a clear audit trail on both.

**Decision.** HMAC-SHA256 over the canonical payload, header
`X-Nexus-Signature: sha256=<hex>`. Retryable status set is
`{408, 425, 429, 500, 502, 503, 504}` plus `httpx.HTTPError`.
Backoff is `min(2^(attempt-1), 60)` seconds. Every attempt is
persisted to `webhook_deliveries` for audit. Four templates ship:
`generic` (raw JSON), `discord` (embed), `slack` (blocks),
`telegram` (markdown).

**Consequences.**
- Receivers must verify the signature; we document this in the
  webhooks route (`engine/api/routes/webhooks.py`).
- The dispatcher is single-process — it runs as part of the engine.
  Multi-replica deployments need an external coordinator (Redis-based
  work queue) to avoid duplicate deliveries. See known-issues.
- The `signing_secret` is returned exactly once on create. We do not
  log it; we do not store it in plaintext in `WebhookResponse` after
  the initial response. Operators who lose it must rotate.

## 9. Per-strategy event-sourced `Order`, separate from DB row

**Context.** An order has a lifecycle (`new → partially_filled →
filled | rejected | cancelled`) that is best modelled as a sequence
of events, not as a mutable row. But the DB still needs an
addressable row for queries, joins, and audit.

**Decision.** Two coexisting types:
- `engine.core.oms.order.Order` — frozen dataclass, event-sourced,
  transitions guarded by `can_transition`, returns a new immutable
  Order on `apply_event`.
- `engine.db.models.Order` — SQLAlchemy projection that the API
  reads. The intent is to project from the event-sourced order into
  the row periodically.

**Consequences.**
- The projection layer is not yet wired. Today the DB row is the
  source of truth; the event-sourced `Order` is used only inside
  the OMS package. Reconciling them is part of the live-trading
  milestone.
- Reviewers need to remember which `Order` a function takes — the
  static types help but the import paths are similar.
- Once live trading lands, the row will be append-only with a
  separate `OrderEvent` table. Migration path is sketched but not
  built.

## 10. JSONB for semi-structured fields; no `JSON` column type

**Context.** Postgres has two JSON types: `JSON` (text, reparsed every
query) and `JSONB` (binary, indexable). There is no reason to use
`JSON` in a new application.

**Decision.** All JSON-typed columns use `JSONB`. New tables follow
this convention. GIN-indexed when queried by key.

**Consequences.**
- `JSONB` is binary; it doesn't preserve key order. Tests that
  compare JSON outputs must compare semantically (e.g. Pydantic model
  equality), not by string match.
- Schemas inside `JSONB` columns are enforced by the application
  layer (Pydantic), not by the DB. Adding a migration that validates
  JSON structure is a manual step we don't take today — the format
  is documented in `architecture/data-model.md` and trusted from
  there.

## 11. Strategy scoring via z-score normalisation + weighted composite

**Context.** Strategies need to be ranked so operators can pick
between them. Different strategies expose different factors (Sharpe,
Sortino, max DD, win rate, custom factors) — comparing raw numbers
across strategies is meaningless.

**Decision.** `IScoringStrategy` declares `get_scoring_factors() ->
list[ScoringFactor]`. The engine runs the strategy across a universe,
collects raw values per factor, winsorises at the 1st/99th percentile,
z-score-normalises, then computes a weighted composite score in
`[0, 100]`. Per-symbol `SymbolScore`s are auto-ranked. Results are
persisted to `scoring_snapshots` with `excluded_factors` recorded for
audit.

**Consequences.**
- Factor weights must sum to 1.0 (`ScoringFactor` validates).
- Winsorisation protects against outliers but means the score is not
  a pure percentile rank — it's a clipped rank. Authors who care
  about tail behaviour can override `winsorize_pct` per factor.
- The composite is bounded; small differences in score (e.g. 78 vs
  80) are not significant. The `rank` field is the more meaningful
  comparison.

## 12. No implicit role escalation from upstream IdPs (SEV-741)

**Context.** Federated login providers (Google, GitHub, OIDC, LDAP)
assert role claims. A misconfigured or compromised IdP could grant
`admin` to anyone it likes. The naive "trust the IdP" behaviour is
a privilege-escalation vector.

**Decision.** Default behaviour (`auth_overwrite_role_on_login = False`,
`engine/config.py:69`): a federated login updates user identity
fields (email, display name, external_id) but **does not** change
the local `role` column. Operators who want the IdP to own roles
must explicitly opt in. `map_roles()` in
`engine/api/auth/base.py:71-113` drops unrecognized external roles
with a warning rather than mapping them to a default.

**Consequences.**
- Local role assignments are durable across federated logins. To
  promote a user, an admin must call the role-mutation endpoint,
  not reconfigure the IdP.
- A regression that briefly landed (silent role escalation in
  `map_roles`) was reverted in `a81578f` and centralised into a
  single policy in `5525d0f`. The test in
  `tests/test_auth_role_promotion_security_fix.py` is the canary.
- Operators who do enable `auth_overwrite_role_on_login` should
  also enable audit-log retention long enough to trace any
  unexpected role changes; the default is conservative.

---

## How to add a new entry

1. Copy [`adr/template.md`](../adr/template.md) to
   `adr/NNNN-short-slug.md` (next number, kebab-case).
2. Fill in **Context**, **Decision**, **Consequences**.
3. Add a row to the table at the top of this file.
4. If the decision supersedes an earlier one, mark the old ADR
   `Status: Superseded by NNNN` and update both rows.
