# Known limitations & technical debt

An honest list of the things we have not built yet, the things we
built and regret, and the things we built fast that need a
slow-down pass. Prioritised:

- **P0** — fix before the next non-patch release.
- **P1** — fix within one quarter.
- **P2** — track but no deadline.
- **P3** — accept or revisit when external pressure forces it.

Each item links to the issue / ADR / migration that owns it.

## Incomplete features

| Area                              | Status        | Notes                                                                                                          |
|-----------------------------------|---------------|----------------------------------------------------------------------------------------------------------------|
| **Live trade execution**          | Partial       | The `engine/core/execution/live.py` shape exists but the broker plumbing is not wired. Risk: high — production money path. |
| **Plugin sandbox**                | Partial       | Restricted-importer policy shipped (ADR-0005); resource limits are best-effort. Native code (ctypes) is blocked but not all escape paths are closed. |
| **Multi-provider auth**           | Partial       | Local + Google + GitHub + OIDC + LDAP all ship (ADR-0004). Account-linking between providers is missing — same-email users get two rows. |
| **WebSocket API**                 | Partial       | One endpoint (`/api/v1/ws`) with backtest + portfolio events. No market-data streaming yet. No pub/sub backplane — sticky sessions required for HA. |
| **MCP server**                    | Missing       | Not started. Planned to live alongside the API; will reuse the API-key scope model. |
| **Multi-asset instruments**       | Partial       | Crypto / forex / options providers are wired but the strategies + tax code assume equities. Section 1256 contracts are partially supported via Form 6781. |
| **Multi-strategy orchestration**  | Partial       | A portfolio can install multiple strategies but the evaluator scores them independently; no allocator combines them. |
| **Strategy marketplace**          | Partial       | Browse + install + rate work. No payment, revenue share, or signed-package verification yet. |
| **React dashboard / frontend**    | Partial       | Login, portfolio, backtest, webhooks all work. Strategy authoring UI, mobile responsiveness, and the observability view are not finished. |
| **Observability (OTel / Prometheus / Sentry / Structlog)** | Partial | All four SDKs are wired. The metrics backend currently emits to a no-op unless an exporter is plugged in. Grafana dashboards are skeletal. |

## P0 — fix before next release

### Backtest results live in process memory

`POST /api/v1/backtest/run` stores results in an in-process dict
in
[`engine/api/routes/backtest.py:22`](../../engine/api/routes/backtest.py:22).
A uvicorn restart or a worker crash loses both running and
completed runs that have not been re-persisted. The `BacktestResult`
table exists for the evaluator's output but is not written by
`/run`.

**Fix:** move storage to Postgres as soon as the run is created,
update the row in place as the worker progresses. This is a
one-day change blocked only on prioritisation.

### Pagination is `limit`-only

Every list endpoint takes `limit` and returns newest-first. There
is no cursor pagination, so a client cannot reliably walk full
history. With current row counts (webhook deliveries, dsr
requests) this is fine. As data grows it will become a hot path.

**Fix:** introduce `?cursor=...` on the list endpoints that
matter most (webhook deliveries, dsr requests, backtest results).

### No idempotency on writes

None of the mutating endpoints accept an idempotency key. A
network retry on `POST /portfolio/` creates a duplicate row;
same for `POST /webhook`, `POST /backtest/run`, `POST
/marketplace/install`.

**Fix:** standardise on `Idempotency-Key` header with a short
Valkey-backed cache.

## P1 — fix within one quarter

### Account-linking across auth providers

Two rows for the same human is the most common support ticket
once SSO lands. A `/auth/link` endpoint + a merge procedure that
respects `auth_overwrite_role_on_login` is the fix. See
[ADR-0004 → open questions](../adr/0004-multi-provider-auth.md#open-questions).

### MFA not enforced per provider

Today MFA is a user-level boolean. An enterprise IdP that
asserts MFA via its own claims has no way to tell the engine
"this user must always come through me." The fix is
`requires_external_mfa: bool` on `users`, checked at login.

### Live trading risk surface

`engine/core/live/kill_switch.py` exists. The risk engine in
`engine/core/risk_engine.py` does not yet consult it on every
fill. Until it does, live trading must be treated as "broker =
paper only" by operators.

### Strategy sandbox native-code escape paths

`ctypes` is blocked but a strategy that ships a wheel with its
own `.so` can still call `libc.system` indirectly. The defence is
manifest-level review in the marketplace; we should add a CI job
that strips native code from installed wheels.

### Reference data hot-reload

`engine/reference/seed.py` is loaded once at app start. Adding a
new instrument requires a restart. Add an admin-only
`POST /api/v1/reference/reload`.

## P2 — track but no deadline

### Refresh-token rotation is racy under load

The atomic UPDATE in
[`engine/api/routes/auth.py:192`](../../engine/api/routes/auth.py:192)
is correct but is one round-trip per refresh. At > 100 req/s the
refresh path becomes the bottleneck. Move to Valkey-cached
rotation tokens with Postgres as the durable ledger.

### Body size limit is global

`BodySizeLimitMiddleware` applies a uniform 1 MiB. The tax CSV
export and the privacy export endpoints are fine today, but the
planned backtest-import endpoint will need a per-route override.

### No structured audit log of admin actions

The `audit_log` module exists but writes are scattered and
unstructured. Standardise on a single `admin_actions` table with
JSON details.

### The `nexus_*` env-var namespace leaks

Some env vars (`POSTGRES_*`) live outside the `NEXUS_` namespace
because they are shared with the docker-compose entrypoint. The
asymmetry is documented but irksome.

### OAuth state cookie is provider-prefixed

`oauth_state_<provider>` cookies do not include the provider's
own redirect URL; an attacker who controls one provider can
potentially reuse the state for another. Mitigated by the
constant-time compare and the cookie path scoping, but a single
verifier per provider would be cleaner.

### Linting has per-file exemptions we'd rather not have

`pyproject.toml` has ~50 per-file ignores. Many are intentional
(B008 for FastAPI `Depends(...)` at module scope) but a few are
there because the code predated the rule. Sweep quarterly.

## P3 — accept or revisit under pressure

### No multi-tenant story

By design (see [architecture/overview.md → non-goals](../architecture/overview.md#non-goals)).
Operators with multi-tenant needs run multiple deployments.

### The SDK is sync-only

`nexus_sdk.testing.StrategyTestHarness` is async but the
`IStrategy.evaluate()` signature forces `async def`. That's the
right call for engine interop; sync-only callers have to wrap in
`asyncio.run()`. Acceptable for now.

### No native mobile app

ADR-0003 is "PWA on top of React". That's still the plan; revisit
if App Store distribution becomes a real requirement.

### Alembic chain is sequential, not date-stamped

The chain is `001_`, `002_`, …, `012_`. Date-stamped revisions
would survive rebase better when two PRs add migrations
concurrently. The current scheme is fine at our PR cadence.

## Tech-debt we have already paid down

For posterity — these were P0 once and got fixed:

- **SEV-741** — silent role escalation via federated login
  ([commit](https://github.com/your-org/nexus-trade-engine/commit/a81578f)).
- **gh#157** — GDPR export dropped orphaned BacktestResults
  (outerjoin fix).
- **SEV-264** — coverage gaps in low-coverage modules
  (targeted tests landed).
- **SEV-507** — security-header gaps (full set now applied).
- **compose ports bound to 127.0.0.1** — accidental public
  exposure fixed.

If you're adding to this list, also add the SEV / issue number
so a future reader can grep for the original incident.
