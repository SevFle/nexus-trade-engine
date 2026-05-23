# Known Limitations & Technical Debt

This is an honest inventory of what's incomplete, fragile, or just plain
missing. It is ordered by severity: items at the top are production blockers;
items at the bottom are DX polish.

---

## P0 — Production Blockers

### Live Trading Is Partial

The `LiveBackend` (`engine/core/execution/live.py`) exists but has no
broker adapter implementation. The execution backend ABC is complete; the
missing piece is a concrete adapter (Alpaca, IBKR) that:
- Manages the broker WebSocket connection
- Translates between the engine's `Order` model and the broker's order API
- Handles partial fills and rejections

**Impact:** Live mode cannot be used. Paper trading works end-to-end.

### Plugin Sandbox Is Not Fully Hardened

The 5-layer sandbox (`engine/plugins/sandbox/`) blocks the obvious escape
vectors (import restrictions, network whitelist, resource limits, filesystem
isolation, introspection blocking) but:
- Resource limits use Python's `resource` module, which is Linux-only — no
  enforcement on macOS/Windows.
- Memory limits are not enforced on individual `asyncio` coroutines — a
  strategy that builds a huge list in memory will consume process memory.
- The restricted importer does not cover all stdlib escape vectors (e.g.,
  `ctypes`, `subprocess` are blocked but edge cases may exist).

**Impact:** Do not run untrusted third-party strategies without an OS-level
container boundary. The sandbox raises the bar but is not a security boundary.

### No Persistence for Kill Switch State

The live trading kill switch (`engine/core/live/kill_switch.py`) is a
process-singleton — state is lost on restart. If the engine crashes after
the switch is engaged, the replacement process will start with trading
enabled.

**Impact:** Operators must verify kill switch state after any restart.
The runbook documents this as a mandatory check.

---

## P1 — Functional Gaps

### Marketplace Is Stubbed

The marketplace routes (`/api/v1/marketplace/*`) return placeholder responses.
The browse, install, and uninstall endpoints are defined but not implemented:

```
POST /api/v1/marketplace/install → {"status": "not_implemented", ...}
```

**Impact:** Strategies can only be installed by dropping files into the
`./strategies/` directory. No remote install workflow exists.

### No Strategy Versioning in the Registry

The `PluginRegistry` loads strategies from the filesystem by name but has
no version tracking. Running two versions of the same strategy requires
different directory names (e.g., `mean_reversion_v1/`, `mean_reversion_v2/`).

**Impact:** No blue/green strategy deployments. Updating a strategy requires
deactivating the old one and activating the new one in sequence.

### OAuth Providers Require Manual User Provisioning

The Google, GitHub, and OIDC auth providers create a `UserInfo` on successful
authentication but the callback flow expects the user to already exist in the
database. There's no auto-provisioning (upsert) for OAuth-first users.

**Impact:** OAuth providers must be combined with a local registration step
or manual DB seeding. The `local` provider works correctly end-to-end.

### Tax Reports Are Stateless

The tax report endpoint computes tax summaries on-the-fly from the
submitted disposals. There is no persistence of annual tax inputs. Operators
who want to build a tax dashboard must store the disposals themselves.

**Impact:** Re-running a tax report requires re-submitting all disposals
for the year.

### No Multi-Currency Support

The cost model and portfolio manager assume USD throughout. The `Money`
dataclass has a `currency` field but it's always `"USD"` in practice.

**Impact:** Trading non-USD instruments will produce incorrect cost and
tax calculations.

---

## P2 — Operational Debt

### README.md Tech Stack Table Is Wrong

The root `README.md` lists "Celery" as the task queue and "Redis 7" as the
cache. The actual stack uses TaskIQ and Valkey 8. The table at line 134-140
needs updating.

### Two Entry Points

Both `engine/app.py` (production, full middleware) and `engine/main.py`
(minimal, fewer routes) exist. The README and Dockerfile correctly reference
`engine.app:create_app`, but having `main.py` is confusing for new
contributors. It should be removed or clearly documented as a lightweight
dev-only entry point.

### Coverage Gate Inconsistency

`pyproject.toml` sets `fail_under = 80` but the Makefile uses `--cov-fail-under=70`.
The CI workflow should be the source of truth (80%), but the Makefile target
will pass at 70%.

### Event Bus Redis Fallback Is Silent

When Valkey is unavailable, the event bus falls back to in-process delivery
with a warning log. But the `/ready` endpoint checks Valkey independently
and will report `degraded`. This means events (order fills, backtest
completions) may not reach the worker if the app and worker are in separate
processes and Valkey is down.

### WebSocket Auth Is Not Fully Implemented

The WebSocket route (`engine/api/routes/websocket.py`) exists but the
auth flow (query param token vs. first-message token) is not documented
and may not be enforced consistently.

---

## P3 — Code Quality

### Strategy SDK Has Two Interfaces

There are two strategy base classes:
- `nexus_sdk.strategy.IStrategy` (public SDK) — uses `evaluate(portfolio, market, costs)`
- `engine.plugins.sdk.BaseStrategy` (internal) — uses `on_bar(state, portfolio)`

The plugin registry loads strategies using the `BaseStrategy` interface,
but the SDK's `IStrategy` is the documented public API. This mismatch
means SDK-written strategies won't work with the current registry without
an adapter layer.

### Test Factory Coverage

`tests/factories.py` provides `make_user` and `make_portfolio` helpers but
many test files manually construct objects instead of using the factories.
This leads to duplication and makes schema changes harder.

### No Alembic Offline Mode Support

The Alembic configuration uses an online connection for `--autogenerate`.
Teams that want to review generated migrations without a running database
need to switch to `--sql` mode or templated migrations.

---

## P4 — Nice-to-Haves

### No API Pagination Convention

List endpoints (portfolios, webhooks, scoring results) each implement their
own pagination slightly differently (some use `page/per_page`, others use
`limit/offset`). A shared pagination dependency would reduce boilerplate.

### Frontend Tests Are Minimal

The frontend has test infrastructure (Vitest + Testing Library) but most
screens lack test coverage. The onboarding flow has tests, but core screens
(Dashboard, Strategies, Backtest) do not.

### No OpenAPI Schema Customization

The auto-generated OpenAPI schema at `/docs` includes all routes but lacks
example values, response examples, and operation descriptions. Adding
`response_model_example` to routes would improve the Swagger UI experience.
