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

**Background**: an *uncommitted* merge conflict in
[`engine/portfolio/rebalancer.py`](../engine/portfolio/rebalancer.py)
once left an unterminated string literal that made `engine.portfolio`
`SyntaxError` on import. It was never committed (HEAD clean, CI green)
and is since resolved — but the failure mode is severe:
`engine.api.router` imports `engine.portfolio` at app build time, so a
single committed `<<<<<<<`/`=======`/`>>>>>>>` block would `SyntaxError`
every web **and** worker process at startup before CI could catch it.

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

## P1 — Strategy Marketplace is mostly a stub (search + ratings real, in-memory)

**Where**: [`engine/api/routes/marketplace.py`](../engine/api/routes/marketplace.py)

`browse`, `install`, `uninstall`, and the legacy
`{strategy_id}/rate` route still return `{"status":"not_implemented"}`;
`categories` returns a hardcoded list. There is **no installable
marketplace registry** (local or remote) behind those routes.

The **search** surface landed (gh#1476):
`GET /api/v1/marketplace/search?q=&category=&tag=&sort=&page=&limit=`
is real (keyword + filter, weighted relevance ranking, paginated) but
resolves against the same kind of process-local store — an
`InMemoryStrategyCatalog` ([`engine/marketplace/search.py`](../engine/marketplace/search.py))
seeded with demo strategies. No DB model, no persistence, no
cross-replica visibility — a browse/UX preview, not a source of truth.

The **ratings** surface landed (gh#1430): `POST` / `GET`
`/api/v1/marketplace/strategies/{strategy_id}/ratings` are real (one
upsert per `(strategy_id, user_id)`, aggregate + reviews). But they are
backed by a process-local `InMemoryRatingsStore`
([`engine/marketplace/ratings.py`](../engine/marketplace/ratings.py)) —
**no DB model, no migration, no persistence**: ratings vanish on every
restart and are invisible to other replicas. The store itself warns
when instantiated outside pytest. That is a P0-grade data-loss risk
hiding behind a "stub" router — treat ratings as non-production until
a Postgres-backed `RatingsStore` replaces the in-memory default.

**Workaround today**: install strategies under
`engine/plugins/<kind>/<name>/` and reload the plugin registry. Treat
marketplace `/search` as a UX/browse preview and do not rely on
ratings or catalog contents surviving a restart or being visible
across replicas.

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
- **`MarketWatch`** is backed by live market data
  ([`frontend/src/api/marketData.js`](../frontend/src/api/marketData.js)).
- **`PortfolioOverview`** (`/portfolio`) and **`StrategiesPage`**
  (`/strategies`, the primary destination of the STRATEGIES nav link)
  are wired to the engine through a typed `apiClient` + TanStack Query
  — `GET /api/v1/portfolio/summary` and `GET /api/v1/strategies/`
  respectively (gh#1671, gh#1669). Both render loading-skeleton / error
  / empty states so a slow or absent backend degrades to an inline
  notice. This is the leading edge of a `screens/*.jsx` → `pages/*.tsx`
  migration that is still in progress (see below).

What is **not** wired is the rest of the core trading surface. These
routes still render hardcoded `MOCK_*` fixtures instead of engine
responses:

- [`Dashboard.jsx`](../frontend/src/screens/Dashboard.jsx) (`/`) — `MOCK_PORTFOLIO`.
- [`Backtest.jsx`](../frontend/src/screens/Backtest.jsx) (`/backtest`) —
  `MOCK_RESULTS`, `MOCK_CONFIG`, a synthetic `EQUITY_CURVE` (it never
  calls `POST /api/v1/backtest/run`). A migrated
  [`BacktestPage.tsx`](../frontend/src/pages/BacktestPage.tsx) that *does*
  post to `/api/v1/backtest/run` and poll `/api/v1/backtest/results/{id}`
  exists on disk but is **not yet imported or routed** in `App.tsx`.
- [`Strategies.jsx`](../frontend/src/screens/Strategies.jsx)
  (`/strategies/runner`) — the legacy interactive runner, kept as mock;
  the primary `/strategies` listing is the real `StrategiesPage` above.
- [`Positions.jsx`](../frontend/src/screens/Positions.jsx) (`/positions`),
  [`Marketplace.jsx`](../frontend/src/screens/Marketplace.jsx) (`/marketplace`) — mock lists.

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

**Fix path**: (1) finish the `screens/*.jsx` → `pages/*.tsx` migration
(the pattern is already set by `PortfolioOverview`/`StrategiesPage`:
replace the remaining `MOCK_*` fixtures with `useQuery` calls and flip
the `App.tsx` route imports — `BacktestPage.tsx` is already migrated on
disk and only needs to be wired to `/backtest`);
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

## P1 — TaskIQ plumbing is wired but backtests still run in-process

The TaskIQ broker lifecycle is now correctly owned by the FastAPI app
([`engine/tasks/broker.py`](../engine/tasks/broker.py) is the canonical
broker; [`engine/app.py`](../engine/app.py) calls `await
broker.startup()` / `await broker.shutdown()` in the lifespan), and
`GET /api/v1/tasks/status` is a real, **unauthenticated** liveness
probe that reflects the broker's *actual* state (`running` /
`stopped`) rather than a hardcoded constant. So the *broker* side of
the plumbing is complete.

What is **still** incomplete:

- `POST /api/v1/backtest/run` and `POST /api/v1/backtest` use FastAPI
  `BackgroundTasks`, not TaskIQ — so backtests still run in the *web*
  process, not the worker. A long backtest stalls the uvicorn worker
  pool that served the request. The TaskIQ task `run_backtest_task`
  ([`engine/tasks/worker.py`](../engine/tasks/worker.py)) exists and
  works, but no REST route enqueues onto it.
- The task-pipeline SLO in [`operations/slos.md`](operations/slos.md)
  is defined but has no emitter yet — `nexus.task.runs_total` is the
  intended metric name. `/tasks/status` itself emits `tasks.status.*`
  logs but not a Prometheus counter.

**Workaround today**: run uvicorn with `--workers N` and tune
`NEXUS_WORKER_CONCURRENCY` high enough to absorb long backtests. Do
not depend on the worker process for backtest isolation today. The
`/tasks/status` endpoint can at least tell you whether the broker is
reachable from the web process.

---

<a id="ldap-has-no-route"></a>
## P1 — LDAP is registered but has no route

**Where**:
[`engine/app.py`](../engine/app.py) (`_build_auth_registry`, the
`case "ldap"` branch),
[`engine/api/auth/ldap.py`](../engine/api/auth/ldap.py) (the
`python-ldap`-backed provider wired into the registry), and
[`engine/auth/providers/ldap.py`](../engine/auth/providers/ldap.py)
(a newer, more robust `ldap3`-backed provider landed in PR #1368 that
is *not* wired).

`NEXUS_AUTH_PROVIDERS=…,ldap` makes `create_app()` register an
`LDAPAuthProvider` in the `AuthProviderRegistry`, so a caller that
imports the registry can drive it via
`registry.authenticate("ldap", username=…, password=…, db=…)`. But
**no HTTP route does so**:

- `POST /api/v1/auth/login` hard-codes `"local"`
  ([`engine/api/routes/auth.py`](../engine/api/routes/auth.py)).
- `GET /api/v1/auth/{provider}/callback` is OAuth-shaped — it expects a
  `code` and validates an OAuth state cookie, neither of which fits
  LDAP's username/password flow.

Consequence: an operator who enables LDAP can prove the bind works
from a Python shell, but no end user can log in through the engine.

There is also a second, more sophisticated LDAP implementation now:
`engine/auth/providers/ldap.py` (PR #1368, 662 lines — search-then-bind,
an `LDAPConnectionPool` with single-flight safety, typed exceptions
inheriting from the shared `OAuthError`, lazy `ldap3` import). It is
exported from `engine.auth.providers` but **not** registered in the
app — it is library-only, paralleling the wired `engine/api/auth/ldap.py`.

**Workaround today**: none at runtime over HTTP. Treat LDAP as a
library-callable provider; do not list it in `NEXUS_AUTH_PROVIDERS`
expecting end users to authenticate against it. Operators who need
LDAP today must add a route that calls
`registry.authenticate("ldap", …)` and mints tokens via the existing
`_mint_tokens` / `_store_refresh_token` helpers.

**Fix path**: (1) add `POST /api/v1/auth/ldap/login` (body
`{username, password}`) that drives the registry's `ldap` provider;
(2) decide whether to keep the simpler `engine/api/auth/ldap.py`
(`python-ldap`) or replace it with the newer
`engine/auth/providers/ldap.py` (`ldap3`, pooled, search-then-bind)
and rewire `_build_auth_registry`'s `case "ldap"` branch to the new
one; (3) extend the role-mapping documentation in
[`adr/0002-auth-rbac.md`](adr/0002-auth-rbac.md) once the wiring
choice is made.

---

<a id="oidc-two-implementations"></a>
## P1 — OIDC has two implementations; only the discovery-based one is wired

**Where**: [`engine/api/auth/oidc.py`](../engine/api/auth/oidc.py)
(wired) vs [`engine/auth/oidc.py`](../engine/auth/oidc.py) (PR #1633,
library-only)

There are **two OIDC providers** on disk, and they are not the same
code path:

- **`OIDCAuthProvider`** in `engine/api/auth/oidc.py` is the one
  `create_app()._build_auth_registry()` actually registers
  (`case "oidc"` in [`engine/app.py`](../engine/app.py)). It is
  discovery-document driven: it fetches `oidc_discovery_url`, reads
  `token_endpoint` / `jwks_uri` / `authorization_endpoint` from it, and
  creates/upserts the `User` row (`auth_provider="oidc"`). This is the
  provider an operator reaches by setting
  `NEXUS_AUTH_PROVIDERS=…,oidc` and hitting
  `GET /api/v1/auth/oidc/callback`.
- **`OIDCProvider`** in `engine/auth/oidc.py` (PR #1633) is a generic,
  issuer-configurable OIDC client implementing the
  `IOAuthProvider` contract (`engine/auth/base.py`). It is more
  rigorous than the wired adapter — JWKS caching with `force=` refresh,
  an injectable `_JWKSClient`/`httpx` transport for tests, an explicit
  signing-algorithm allowlist that makes `alg=none` impossible, HTTPS
  enforcement on JWKS/token endpoints (localhost exempt), PKCE
  `code_verifier` forwarding, and a typed exception hierarchy
  (`OIDCError` / `InvalidTokenError` / `TokenExchangeError` /
  `DiscoveryError`). It is configurable via the **separate**
  `oidc_issuer` / `oidc_jwks_uri` settings (not `oidc_discovery_url`)
  and can be built by `engine.auth.get_oauth_provider("oidc")` — but
  **that factory is not on the request path**, so this provider is
  library-only today.

This is the same shape as the [LDAP split](#ldap-has-no-route) and the
Google/GitHub split recorded in
[ADR-0002](adr/0002-auth-rbac.md#evolution--how-this-actually-landed):
the `engine/auth/` tree is a standalone protocol library whose providers
are not wired into the runtime registry.

**Workaround today**: the wired `OIDCAuthProvider` works end-to-end for
real users — use it. Treat `engine/auth/oidc.OIDCProvider` as a
library/reusable component (or as the stricter reference implementation)
until the two are reconciled. Do not assume configuring `oidc_issuer`
will change login behaviour; login is driven by `oidc_discovery_url`.

**Fix path**: (1) decide whether the JWKS-verify strictness of
`engine/auth/oidc.py` (alg allowlist, HTTPS enforcement, typed
errors) should replace the inline verification in
`engine/api/auth/oidc.py`; (2) if so, have the wired adapter delegate
verification to `OIDCProvider.verify_id_token` and collapse the two
config surfaces (`oidc_discovery_url` vs `oidc_issuer`/`oidc_jwks_uri`);
(3) record the choice in
[ADR-0002](adr/0002-auth-rbac.md#evolution--how-this-actually-landed).

---

<a id="ast-validator-two-implementations"></a>
## P1 — Static AST validator has two implementations; only the simpler one is wired

**Where**: [`engine/plugins/restricted_importer.py`](../engine/plugins/restricted_importer.py)
(wired) vs [`engine/plugins/sandbox/ast_validator.py`](../engine/plugins/sandbox/ast_validator.py)
(PR #1647, patched #1653 — library-only)

This is the import-checking analogue of the
[OIDC split](#oidc-two-implementations) and
[LDAP split](#ldap-has-no-route): there are **two** static, parse-time
AST validators on disk, and the one actually on the plugin-load path is
the simpler of the two.

- **`ImportValidator`** in `engine/plugins/restricted_importer.py` is
  the **wired** one. It is invoked at plugin load by
  [`engine/plugins/registry.py`](../engine/plugins/registry.py)
  (`ImportValidator(DENYLIST_MODULES).validate(source_bytes)`, run
  **before** `compile`/`exec`) — this is the Layer 0 check recorded in
  [ADR-0010](adr/0010-static-ast-validation-toctou-loading.md) and
  [`architecture/plugins.md`](architecture/plugins.md). Its
  `visit_ImportFrom` flags only `from <blocked> import …` where the
  **module root** is on the denylist; it **skips relative imports**
  (`level > 0` with no absolute module) and does **not** reject wildcard
  `from … import *`. It returns a flat `list[str]` and applies the
  denylist only — no parse-time allowlist enforcement (that lives in the
  runtime Layer 1 hook).
- **`ASTValidator`** in `engine/plugins/sandbox/ast_validator.py`
  (PR #1647, patched #1653) is the stricter successor. It additionally:
  - rejects **wildcard** `from … import *` outright — relative *and*
    absolute — as `CODE_FORBIDDEN_FROM_IMPORT`, because the bound names
    cannot be enumerated statically (the #1653 *"critical security
    bypass for `from . import *`"* lived here);
  - flags **relative imports that escape the strategy package**
    (`level > 1`) as `CODE_RELATIVE_IMPORT`, while checking the module
    root consistently for within-package (`level == 1`) relative
    imports;
  - enforces an **allowlist with denylist precedence** at parse time
    (defence-in-depth that catches unlisted modules before `exec`, not
    only at import time);
  - returns a **structured, total** `ValidationResult` of
    `Violation(line, col, code, severity, …)` records instead of a flat
    `list[str]`, capturing `SyntaxError` as a violation rather than
    raising.

  It is **not imported anywhere in the engine** (non-test) code today —
  confirmed by grep across `engine/` — so none of these stricter checks
  are live. It is a reusable component / reference implementation
  awaiting wiring.

**Framing on severity (read this before assuming an exploit):** the
wired `ImportValidator` not blocking `from … import *` is a
**defence-in-depth gap at Layer 0**, *not* a full sandbox escape. Every
`from … import` still passes through the **runtime** allowlist
([`RestrictedImporter`](../engine/plugins/restricted_importer.py),
Layer 1) when the names are actually bound, so a forbidden module is
still refused at exec time. The gap is that the *static* pre-`exec`
trip-wire is weaker than the stricter `ASTValidator` was built to be, so
an attempt that should have died at parse time instead survives to the
runtime hook.

**Workaround today**: assume Layer 0 statically rejects only
absolute/denylisted imports and forbidden *calls*
(`exec`/`eval`/`compile`/`__import__`/`importlib.import_module`); rely
on the runtime Layer 1 allowlist as the real import gate. Treat
`ASTValidator` as the stricter reference and do **not** assume wildcard
or escaping-relative rejection is enforced at parse time.

**Fix path**: (1) switch [`registry.py`](../engine/plugins/registry.py)
from `ImportValidator(...).validate(...)` to
`validate_strategy_source(...)` (or `ASTValidator(...).validate(...)`),
adapting the `list[str]`→`ValidationResult` boundary so existing
violation handling keeps working; (2) confirm the new wildcard /
relative-import / allowlist checks pass the `tests/test_ast_validator.py`
corpus (allowlist/denylist/forbidden-call/relative-import cases added in
#1647) before flipping it on; (3) record the wiring decision in
[ADR-0010](adr/0010-static-ast-validation-toctou-loading.md) and retire
`ImportValidator` (or fold it in) once nothing else references it.

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

[`docs/adr/`](adr/README.md) now captures twelve decisions: scaffold
(0001), auth/RBAC (0002), mobile/PWA (0003), TaskIQ (0004), Valkey
(0005), bcrypt+Fernet (0006), the strategy sandbox allowlist import
model (0007), the pluggable `MetricsBackend` Protocol (0008), the
cross-replica `EventBus` WebSocket bridge (0009), the static AST
validation + TOCTOU-safe strategy loader (0010), the runtime
introspection / dunder-attribute guard (0011), and the Layer-3 sandbox
resource limits — SIGALRM + tracemalloc + single-flight lock (0012).
A handful of smaller decisions are still recorded only as PR
descriptions or commit messages — e.g. the in-process backtest result
store (this is tech debt to be fixed, P0 above, rather than an
accepted architecture decision), and the dual LDAP-provider situation
(see [LDAP has no route](#ldap-has-no-route) above). Use
[`adr/template.md`](adr/template.md) to capture remaining decisions as
they come up in code review; don't batch them into one mega-ADR.

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
