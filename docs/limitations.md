# Known limitations and technical debt

A prioritised, honest list of what does not work yet, what is
deliberately simplified, and what we have already decided to fix but
have not shipped. If you are about to use the engine in production,
read this *before* reading the feature list.

Conventions:

- **Severity** reflects user-visible blast radius (P0 = data loss /
  security; P1 = common feature gap; P2 = polish).
- **Status** is one of `Open`, `In progress`, `Accepted trade-off`,
  `Obsolete-on-roadmap`. We do not delete entries when fixed — the
  entry flips to `Fixed in <release>` and stays for context.

## P0 — Production-blockers

### 1. Backtest results are not durable

The result of `POST /api/v1/backtest/run` is cached **in-process**
for 1 hour (`_RESULTS_TTL_SECONDS` in
[`engine/api/routes/backtest.py`](../engine/api/routes/backtest.py)).
The `backtest_results` table exists but the route does not write to
it.

Consequences:

- Restarting the engine loses every in-flight and completed result.
- A multi-instance deploy (load balancer in front of N engines)
  cannot reliably route `GET /results/{id}` to the instance that ran
  the backtest.
- There is no historical record once the TTL expires.

**Fix**: write a `BacktestResult` row on completion; route
`GET /results/{id}` to the database; treat the in-process cache as
an optional read-through layer. The model already supports this.

### 2. `X-Forwarded-For` is not honoured

`RateLimitMiddleware` (`engine/api/rate_limit.py`) keys on
`request.client.host`, which is the immediate peer — i.e. the
reverse proxy. Behind a proxy, every request appears to come from
the same IP and the rate limiter effectively caps the *deployment*,
not the client.

`trusted_proxy_depth` exists in the config but defaults to `0`
(defensive). Operators running behind a known proxy must opt in.

**Fix**: document the opt-in knob; add a deployment-test that
exercises the `X-Forwarded-For` chain end-to-end.

### 3. No idempotency for write paths

No endpoint honours `Idempotency-Key`. Retrying a `POST` (e.g. after
a network blip) creates a second row. For backtests, this is a
nuisance; for portfolio creates, it can confuse the UI; for webhook
creates, it can produce a duplicate `signing_secret`.

**Fix**: add a Valkey-backed `Idempotency-Key` cache keyed by `(user,
key, route)`. Return the cached response on retry.

## P1 — Notable feature gaps

### 4. Live trading is not wired

`engine/core/execution/live.py` exists, plus the OMS (`engine/core/oms/`)
and risk engine (`engine/core/risk_engine.py`), but no live broker
integration has shipped. The marketplace, the React frontend, and
the dashboard's "live mode" toggle all assume live trading; today
they are UI-only.

Roadmap order: paper-trading parity → first broker (Alpaca or
IBKR) → OMS hardening → live SLO.

### 5. Marketplace is a stub

`/api/v1/marketplace/install` returns `{status: "not_implemented"}`
today. The shape is finalised; the missing pieces are a package
registry, signature verification, and the per-portfolio permissions
model.

### 6. Privacy deletion is not automatic

`POST /api/v1/privacy/delete` sets a 30-day grace window. After the
window, the deletion does not happen automatically — the engine does
not run a sweeper. Operators must either run the deletion manually
or wire up the planned `engine/tasks/worker.py` job.

### 7. WebSocket manager is single-instance

`engine/api/websocket/manager.py` is in-process. A multi-instance
deploy needs a Valkey pub/sub backplane; without it, events only
reach clients connected to the same instance that produced them.
Sticky sessions at the LB hide this for single-tenant deploys but
will break under any horizontal scale-out.

### 8. No admin-side user / key management

There is no `admin` route to list all users, list another user's
API keys, or revoke another user's refresh tokens. Help-desk
workflows today require direct database access.

### 9. CSV export is only for tax summaries

`POST /api/v1/tax/report/{code}/csv` works; nothing else produces
CSV. Backtest results, privacy exports, and webhook deliveries all
need an "as CSV" surface for spreadsheet round-trips.

### 10. No fine-grained API-key scopes

API keys are `read` / `trade` / `admin`. Operators that need to
grant a key just `portfolio:write` (without `webhook:write`) have no
recourse today.

## P2 — Polish and tech debt

### 11. README mentions Celery; the engine uses TaskIQ

The top-level `README.md`'s "Tech Stack" table still lists Celery as
the task queue. The actual implementation is TaskIQ; the ADR (`0001`)
is correct, the README is not.

### 12. `portfolio.py` route accepts trailing slash inconsistently

`POST /api/v1/portfolio/` and `GET /api/v1/portfolio/` are mounted
with a trailing slash; clients that omit it may get a 307 redirect.
Either standardise on no-trailing-slash (preferred — FastAPI
convention) or document the requirement.

### 13. `_STRATEGIES_DIR = None` global in scoring

`engine/api/routes/scoring.py` reads a module-level `_STRATEGIES_DIR =
None`. The intended override pattern is via FastAPI dependency
injection; the global is a leftover.

### 14. Two sources of truth for the strategy manifest

`engine/plugins/manifest.py:StrategyManifest` (Pydantic model) and
the YAML `manifest.yaml` format are not formally linked. The
`strategies/mean_reversion_basic/manifest.yaml` example uses a
slightly different shape (no `id` field, `parameters` instead of
`config_schema`). Pick one and make the loader enforce it.

### 15. No per-route rate-limit response headers

The middleware enforces the limit but does not emit
`X-RateLimit-Limit`, `-Remaining`, `-Reset`. Clients cannot pre-flight
their retry behaviour.

### 16. Coverage gate is 70% in Makefile, 80% in pyproject.toml

`make test` enforces `--cov-fail-under=70`; `pyproject.toml`'s
`[tool.coverage.report]` says `fail_under = 80`. The Makefile wins
(it is the last flag), but the two should agree.

### 17. Many `# noqa` and per-file lint ignores

`pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` has ~50
entries. Several are workarounds that could be removed with a small
refactor (e.g. moving inline imports to the top of the file once
their target module is fast enough to import eagerly).

### 18. `engine/marketplace/` is an empty package

`engine/marketplace/__init__.py` exists and is empty. Either delete
it (the routes live in `engine/api/routes/marketplace.py`) or fill it
in.

### 19. `engine/data/` directory mentioned in overview but not in repo layout

The architecture overview references `engine/data/`. The directory
exists but the top-level `README.md` project-structure block does
not list it. Drift between the two is a contributor onboarding
hazard.

### 20. Reference seed has no swap-out point

`engine/reference/seed.py` ships a fixed instrument list. Operators
that want a different seed (e.g. an EU-only universe) have to fork
the file. A config-driven seed path would unblock this.

## Accepted trade-offs

These are *not* bugs; they are deliberate choices the team has
signed off on. Listed here so future contributors do not re-debate
them.

- **No multi-tenancy.** Operators run their own deployment; the
  codebase models one tenant per database. See
  [`adr/0002-auth-rbac.md`](adr/0002-auth-rbac.md) and
  [`architecture/overview.md`](architecture/overview.md#non-goals).
- **TaskIQ over Celery.** Async-native, smaller surface, no
  separate broker protocol. See [`adr/0001-scaffold-tech-choices.md`](adr/0001-scaffold-tech-choices.md).
- **In-process event bus.** `EventBus` is synchronous, in-process,
  single-subscriber (`WebhookDispatcher`) today. Cross-process
  events belong to the WebSocket backplane (item 7 above) when it
  lands.
- **bcrypt for API keys.** ~50 ms of CPU per API-key request is
  acceptable at our scale; if it bites, a short-TTL Valkey cache on
  `(prefix, verified)` is the escape hatch.
- **No `Idempotency-Key` middleware today.** Single-tenant deploys
  with well-behaved UIs do not produce enough duplicate writes to
  justify the complexity. Add it before scaling out the client
  surface.

## How to add to this list

Open a PR titled `docs(limitations): add <thing>`. In the PR:

- Severity (P0/P1/P2) or "Accepted trade-off".
- One-sentence description that a *new* contributor can understand.
- The fix sketch (a sentence, not a design doc — designs go in an
  ADR).

Mark the entry `Fixed in <release>` rather than deleting it when it
ships; the audit trail is useful when planning the next hardening
pass.
