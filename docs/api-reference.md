# API Reference

The engine exposes one FastAPI app under `/api/v1/*` (plus `/health`,
`/ready`, `/metrics` outside the v1 prefix). OpenAPI is auto-generated
at `/docs` and `/redoc` when the engine is running; this document is
the curated, narrative version that explains *why* each endpoint
exists, what it requires, and how it behaves on error.

All routes are mounted by [`engine/api/router.py`](../engine/api/router.py).
Each table below maps to one router module so you can jump to source.

## Authentication

Every protected route accepts either:

- `Authorization: Bearer <jwt>` — short-lived JWT issued by
  `POST /api/v1/auth/login` or `/register`/`/refresh`/`/auth/{provider}/callback`.
- `X-API-Key: nxs_<prefix>_<secret>` — long-lived, bcrypt-hashed API
  key (gh#94). The plaintext secret is returned **exactly once** on
  creation. Issued by `POST /api/v1/auth/api-keys`.

JWT and API-key paths share [`get_current_user`](../engine/api/auth/dependency.py);
the dependency stashes the active `ApiKey` on `request.state` when
present, so scope checks downstream don't re-authenticate.

### Roles (RBAC hierarchy)

Enforced via `Depends(require_role("developer"))` etc. Numeric levels
in [`engine/api/auth/dependency.py`](../engine/api/auth/dependency.py#L27):

| Role | Level |
|---|---|
| `viewer` | 0 |
| `user` | 1 |
| `retail_trader` | 2 |
| `quant_dev` | 3 |
| `developer` | 4 |
| `portfolio_manager` | 5 |
| `admin` | 6 |

A request with role `R` satisfies `require_role(X)` iff
`level(R) >= level(X)`.

### API-key scopes

Hierarchy (gh#86): `admin > trade > read`. JWT-authenticated requests
bypass scope enforcement — JWTs are gated by role instead. API keys
that lack the required scope get `403`.

- `read` — GET / HEAD only.
- `trade` — POST / PUT / PATCH for backtest, portfolio, webhooks, etc.
- `admin` — equivalent to the `admin` role.

### Legal acceptance gate

`backtest`, `scoring`, `market-data`, `marketplace`, `portfolio`, and
`strategies` routers are mounted with
`Depends(require_legal_acceptance)`. Callers without an
`acceptances` row for the *current version* of every
`requires_acceptance` document get **`451 Unavailable For Legal
Reasons`** with body `{code:"legal_re_acceptance_required",
documents:[<slug>, …]}` (not `403` — the dedicated code lets clients
and the frontend distinguish a consent gate from an RBAC denial; see
[`engine/legal/dependencies.py`](../engine/legal/dependencies.py)).
An unauthenticated request hits `401` first: the dependency's
principal guard treats both an unresolved `Depends` marker and an
explicit `None` as "no user" so the gate can't be silently bypassed
when it is invoked outside FastAPI DI. Acceptance is recorded via
`POST /api/v1/legal/accept`. See [`data-model.md`](data-model.md) for
the immutable acceptance table.

Legal acceptance is wired in two places: most routers declare it at
the `APIRouter(dependencies=…)` level
([`portfolio.py`](../engine/api/routes/portfolio.py),
[`strategies.py`](../engine/api/routes/strategies.py),
[`marketplace.py`](../engine/api/routes/marketplace.py),
[`scoring.py`](../engine/api/routes/scoring.py)); `backtest` and
`market-data` get it from the top-level include in
[`router.py`](../engine/api/router.py). `reference`, `tax`, `webhooks`,
`privacy`, and `auth` are **not** gated — they need to be reachable
before the user has accepted anything (e.g. the legal docs UI itself
calls `/reference/suggest` to render attributions).

---

## Health & observability

Unauthenticated probes for load balancers and Prometheus.

| Method | Path | Source | Notes |
|---|---|---|---|
| GET | `/health` | [`routes/health.py`](../engine/api/routes/health.py#L19) | Liveness. Always returns `{"status": "ok"}`. |
| GET | `/health/providers` | same | Reports each registered data provider (up/degraded/down + latency). |
| GET | `/ready` | same | Readiness — pings DB (`SELECT 1`) and `valkey.ping()`. Returns `degraded` if either fails. |
| GET | `/metrics` | [`routes/metrics.py`](../engine/api/routes/metrics.py) | Prometheus exposition. The `RateLimitMiddleware` exempts `/metrics` from throttling. |

## System

`/api/v1/system/*` — operational metadata for CI probes.

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/v1/system/status` | any | Engine version, uptime, DB reachability, and counts (`users`, `portfolios`, `backtests`, `webhooks_active`, `api_keys_active`). |

Source: [`routes/system.py`](../engine/api/routes/system.py).

## Tasks

`/api/v1/tasks/*` — TaskIQ broker health. Source:
[`routes/tasks.py`](../engine/api/routes/tasks.py). The broker itself is
opened/closed in the app lifespan ([`engine.app`](../engine/app.py)) and
stashed on `app.state.taskiq_broker`.

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/v1/tasks/status` | none | Infrastructure liveness probe for the async pipeline (orchestrators / load balancers hit it during deploys, so it stays **unauthenticated**). **Always returns HTTP 200** — the API being up is independent of the broker, so a broker outage must never turn the probe into a restart loop; callers branch on the per-subsystem fields instead. Body `{status:"ok", broker:"running"\|"stopped", broker_online:bool}`. `broker` reflects the taskiq broker's *real* state (not a hardcoded constant): it prefers the broker's `is_started` flag on newer taskiq, and falls back to a 1 s-bounded `PING` against the broker's shared connection pool on older taskiq releases that lack the flag. The throwaway PING client is deliberately **never closed**, so the probe cannot `pool.disconnect()` — and thus cannot perturb — the pool the app's task dispatch depends on. A hung broker times out to `stopped` rather than 500. |

## Client errors

`/api/v1/client/*` — browser-side error ingest. Source:
[`routes/client_errors.py`](../engine/api/routes/client_errors.py).

The frontend's `ErrorBoundary` POSTs unhandled exceptions here so
browser-side failures correlate with the audit trail. The endpoint is
**not auth-gated** (an authenticated session is exactly when error
reporting is most likely to fail); abuse is bounded by a tight
per-route rate limit (`30 req/min/IP`, configured in
[`engine/app.py`](../engine/app.py)). There is no persistence slice —
it emits one structlog `client.error` event and returns a stable id.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/v1/client/errors` | `{message, stack?, component_stack?, url?, user_agent?, error_id?}` | `201 {error_id}`. All text fields are capped at 64 KiB; CRLF/ANSI escapes are stripped, `url` is reduced to scheme+host+path (query strings carry tokens), and a caller-supplied `error_id` must parse as a UUID. |

## Auth & MFA

Mounted at `/api/v1/auth/*` and `/api/v1/auth/mfa/*`. Source:
[`routes/auth.py`](../engine/api/routes/auth.py), [`routes/mfa.py`](../engine/api/routes/mfa.py).

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/v1/auth/register` | `{email, password, display_name?}` | `201` `{access_token, refresh_token, token_type:"bearer", expires_in}` |
| POST | `/api/v1/auth/login` | `{email, password}` | Either `200` token pair or `200` `{mfa_required:true, challenge_token}` |
| POST | `/api/v1/auth/refresh` | `{refresh_token}` | New token pair. **Detects reuse**: if a revoked token is presented, *all* the user's sessions are revoked (`401`). |
| GET  | `/api/v1/auth/me` | — | `UserProfileResponse` (`id, email, display_name, role, auth_provider, is_active`) |
| POST | `/api/v1/auth/logout` | `{refresh_token?}` | Revokes one token or every active session for the caller. |
| GET  | `/api/v1/auth/{provider}/authorize` | — | Returns `{authorize_url, state}`; sets an HttpOnly `oauth_state_<provider>` cookie for 10 minutes. |
| GET  | `/api/v1/auth/{provider}/callback` | `?code=&state=` | Validates state against cookie, mints tokens, returns token pair. |

Providers enabled by `NEXUS_AUTH_PROVIDERS` (csv): `local`, `google`,
`github`, `oidc`, `ldap`. Each provider registers in
[`engine/api/auth/`](../engine/api/auth/) at app startup via
[`_build_auth_registry`](../engine/app.py#L86).

### MFA (TOTP + backup codes)

TOTP secrets are Fernet-encrypted with `NEXUS_MFA_ENCRYPTION_KEY`.
Backup codes are bcrypt-hashed and stored in `users.mfa_backup_codes`
(JSONB). The plaintext is returned **once** on enrollment / regen.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/v1/auth/mfa/enroll` | — | Generates a TOTP secret + `otpauth://` URI. Doesn't enable yet. |
| POST | `/api/v1/auth/mfa/enroll/confirm` | `{secret, code}` | Verifies the code, persists encrypted secret, enables MFA, returns plaintext backup codes. |
| POST | `/api/v1/auth/mfa/verify` | `{challenge_token, code}` | Exchange a login challenge + TOTP/backup code for a token pair. |
| POST | `/api/v1/auth/mfa/disable` | `{password, code}` | Requires the user's password AND a valid TOTP code (defense in depth). |
| POST | `/api/v1/auth/mfa/backup-codes/regen` | `{code}` | Generates a fresh batch, returns plaintext. |

### API keys

Long-lived, scoped, bcrypt-hashed. Source: [`routes/api_keys.py`](../engine/api/routes/api_keys.py).
Mounted at `/api/v1/auth/api-keys`.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/v1/auth/api-keys` | `user` | `{name, scopes:["read"\|"trade"\|"admin"], expires_at?, env?}` → `201` `{id, name, prefix, scopes, ..., token}` — `token` is shown this one time only. |
| GET | `/api/v1/auth/api-keys` | `user` | Lists caller's keys (excluding hash). |
| DELETE | `/api/v1/auth/api-keys/{key_id}` | `user` | `204` — sets `revoked_at` (idempotent). |

## Legal

The legal router is mounted without a router-level `prefix=` in
[`router.py`](../engine/api/router.py) — every route declares its full
`/api/v1/legal/*` path in the decorator instead, so the effective URLs
are still under the v1 namespace. It is deliberately **not** gated by
`require_legal_acceptance`: it has to stay reachable so the user can
record the acceptance the gate checks for. Source:
[`routes/legal.py`](../engine/api/routes/legal.py).

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/v1/legal/documents?category=` | optional | Lists documents, with per-user acceptance status if a bearer token is sent. |
| GET | `/api/v1/legal/documents/{slug}?version=` | optional | Returns rendered markdown. Template vars (`{{OPERATOR_NAME}}`, `{{EFFECTIVE_DATE}}`, …) are substituted server-side. |
| POST | `/api/v1/legal/accept` | `user` | Records acceptance(s); `acceptances` are append-only (migration 006). |
| GET | `/api/v1/legal/acceptances/me?document_slug=` | `user` | Caller's acceptance history. |
| GET | `/api/v1/legal/attributions?context=` | — | `DataProviderAttribution` rows shown in the UI footer. |

## Portfolio

`/api/v1/portfolio/*`. Source: [`routes/portfolio.py`](../engine/api/routes/portfolio.py).
All routes require legal acceptance.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/v1/portfolio/` | `{name, description?, initial_capital?}` | `201` |
| GET | `/api/v1/portfolio/` | — | Lists caller's portfolios. |
| GET | `/api/v1/portfolio/{portfolio_id}` | — | `403` if not owner. |
| DELETE | `/api/v1/portfolio/{portfolio_id}` | — | Hard delete (cascades to positions, orders, tax lots, installed strategies). |

## Strategies

`/api/v1/strategies/*`. Source: [`routes/strategies.py`](../engine/api/routes/strategies.py).

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/strategies/` | Lists installed strategies via `app.state.plugin_registry.list_all()`. |
| GET | `/api/v1/strategies/{strategy_id}` | Manifest details: `config_schema`, `data_feeds`, `watchlist`, `requires_network`, `requires_gpu`, `is_loaded`. |
| POST | `/api/v1/strategies/{strategy_id}/activate` | `{params:dict}` — instantiates the strategy under its sandbox. |
| POST | `/api/v1/strategies/{strategy_id}/deactivate` | Unloads. |
| POST | `/api/v1/strategies/{strategy_id}/reload` | Hot-reload from disk. |
| GET | `/api/v1/strategies/{strategy_id}/health` | Runtime health (active flag only today). |

## Backtest

`/api/v1/backtest/*`. Source: [`routes/backtest.py`](../engine/api/routes/backtest.py).

Backtest results currently live in an **in-process dict** keyed by
`backtest_id` with a 1-hour TTL (see `_RESULTS_TTL_SECONDS`). A future
refactor persists them to the `backtest_results` table; today, results
are lost on process restart.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/v1/backtest/run` | `{strategy_name, symbol, start_date, end_date, initial_capital?, config?}` | Returns `202 {status:"accepted", backtest_id}`. Computation runs as a `BackgroundTasks` job (not TaskIQ — see [known-limitations.md](known-limitations.md)). |
| GET | `/api/v1/backtest/results/{backtest_id}` | — | `202 {status:"running"}` · `200 {status:"completed", metrics, equity_curve, drawdown_curve, evaluation?}` · `200 {status:"failed", error}` · `404` · `403`. |

The `metrics` object is `MetricsSummary` (24 fields including rolling
windows); see source for the canonical schema. The full list is also
served via OpenAPI.

## Scoring

`/api/v1/scoring/*`. Source: [`routes/scoring.py`](../engine/api/routes/scoring.py).
Requires legal acceptance.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/v1/scoring/{strategy_name}/run` | `{universe:string[], raw_data:{symbol:{factor:value}}}` | Computes scores via a scoring-strategy plugin, persists a `ScoringSnapshot` row. `400` if strategy isn't a scoring strategy. |
| GET | `/api/v1/scoring/{strategy_name}/results?limit=&offset=&sort_by=&sort_order=` | — | Paginated history. |

## Marketplace

`/api/v1/marketplace/*`. Source: [`routes/marketplace.py`](../engine/api/routes/marketplace.py).
**Most routes are stubs** (`{status:"not_implemented"}`) — see
[known-limitations.md](known-limitations.md).

| Method | Path | Auth | Status |
|---|---|---|---|
| GET | `/api/v1/marketplace/browse?category=&search=&sort_by=&page=&per_page=` | `user` | Returns empty list (stub). |
| GET | `/api/v1/marketplace/categories` | `user` | Static category list (algorithmic, ml, llm, hybrid, income, macro). |
| POST | `/api/v1/marketplace/install` | `developer` | Stub. |
| DELETE | `/api/v1/marketplace/uninstall/{strategy_id}` | `developer` | Stub. |
| POST | `/api/v1/marketplace/{strategy_id}/rate?rating=&review=` | `user` | Stub. `400` if `rating ∉ [1,5]`. |

## Reference

`/api/v1/reference/*`. Source: [`routes/reference.py`](../engine/api/routes/reference.py).

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/reference/suggest?q=&limit=&asset_class=` | Typeahead. Tries the local `SearchIndex` first (seeded at startup), then falls through to the Yahoo Finance search API. Caps `limit` at 50; rejects empty / oversize `q`. |

## Market data

`/api/v1/market-data/*`. Source: [`routes/market_data.py`](../engine/api/routes/market_data.py).
Requires legal acceptance.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/market-data/{symbol}/bars?interval=&period=&asset_class=&provider=` | OHLCV via the provider registry. Asset class is inferred from the symbol shape (`BTC/USD → CRYPTO`, `EURUSD=X → FOREX`, default `EQUITY`) and can be overridden. |
| GET | `/api/v1/market-data/{symbol}/quote?asset_class=&provider=` | Latest quote. |

Errors follow the provider hierarchy
([`market_data.py`](../engine/api/routes/market_data.py)):

| Exception | Status | When |
|---|---|---|
| `TransientProviderError`, `TimeoutError` | `503` | Upstream temporarily unavailable. |
| `NoProviderAvailableError` | `503` | Every candidate adapter failed / none registered. |
| `CapabilityNotSupportedError` | `501` | No adapter supports the requested operation for this asset class. |
| `FatalProviderError` | `400` | Caller-side problem (bad symbol, rate-limited by vendor, etc.). |
| `ProviderError` (quote only) | `502` | Generic upstream failure that isn't transient or fatal. |

## Tax

`/api/v1/tax/*`. Source: [`routes/tax.py`](../engine/api/routes/tax.py).

The dispatcher routes a two-letter jurisdiction `code` (`US`, `GB`,
`DE`, `FR`) to a per-jurisdiction summariser in
[`engine/core/tax/reports/`](../engine/core/tax/reports). The endpoint
is stateless — callers re-submit the disposals they care about.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/v1/tax/report/{code}` | `{disposals:[{description, acquired, disposed, proceeds:str, cost:str}]}` | `{jurisdiction, summary:{...}}` — shape depends on the jurisdiction. |
| POST | `/api/v1/tax/report/{code}/csv` | same | `text/csv` attachment (header + values). |

`proceeds`/`cost` are strings to preserve `Decimal` precision.

## Webhooks

`/api/v1/webhooks/*`. Source: [`routes/webhooks.py`](../engine/api/routes/webhooks.py).
The HMAC-signed `signing_secret` is returned **only on create**.

Templates: `generic`, `discord`, `slack`, `telegram`.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/webhooks` | `{url, event_types[], custom_headers?, template, max_retries?, portfolio_id?}` → `201`. **Enforces `trade` scope** for API keys (`require_api_scope("trade")`); JWTs bypass. |
| GET | `/api/v1/webhooks` | Lists caller's configs. `get_current_user` (any authed principal); ownership filtered in handler. |
| PUT | `/api/v1/webhooks/{webhook_id}` | Partial update. `get_current_user`; handler enforces ownership. |
| DELETE | `/api/v1/webhooks/{webhook_id}` | `204`. `get_current_user`; handler enforces ownership. |
| POST | `/api/v1/webhooks/{webhook_id}/test` | Synchronously fan out a test event; returns the resulting `WebhookDelivery`. |
| GET | `/api/v1/webhooks/{webhook_id}/deliveries` | Delivery history. |

## Privacy (GDPR / CCPA)

`/api/v1/privacy/*`. Source: [`routes/privacy.py`](../engine/api/routes/privacy.py).

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/privacy/export` | Synchronous export of the caller's data + audit row in `dsr_requests`. |
| POST | `/api/v1/privacy/delete` | `202`. Initiates a 30-day grace deletion. `409` if already pending. |
| POST | `/api/v1/privacy/delete/cancel` | Cancels during the grace window. `404` if not pending. |
| GET | `/api/v1/privacy/delete/status` | `{pending, sla_due_at?, request?}`. |
| GET | `/api/v1/privacy/requests` | DSR history for the caller. |
| GET | `/api/v1/privacy/kinds` | Allow-list of `kind` values for openapi clients. |

DSR rows are auditable under GDPR Art. 12 (one-month SLA tracked in
`sla_due_at`).

## WebSocket

`WS /api/v1/ws`. The active implementation is the `engine/api/ws/`
package:

| File | Role |
|---|---|
| [`ws/router.py`](../engine/api/ws/router.py) | Endpoint + message dispatch loop |
| [`ws/connection_manager.py`](../engine/api/ws/connection_manager.py) | Connection registry, room-based fan-out, heartbeat, backpressure |
| [`ws/channels.py`](../engine/api/ws/channels.py) | Resolves `subscribe` requests to rooms with permission checks |
| [`ws/permissions.py`](../engine/api/ws/permissions.py) | Channel access control + room-name resolution |
| [`ws/protocol.py`](../engine/api/ws/protocol.py) | Pydantic wire schemas + valid channel set |
| [`ws/event_bridge.py`](../engine/api/ws/event_bridge.py) | Subscribes to the `EventBus` and broadcasts events to rooms |
| [`ws/auth.py`](../engine/api/ws/auth.py) | In-band token validation + per-IP auth rate limiting |

> Note: `engine/api/routes/websocket.py` and
> `engine/api/websocket/manager.py` are a **legacy** implementation
> that is no longer mounted by [`router.py`](../engine/api/router.py).
> The active route comes from `ws/router.py`. Do not extend the legacy
> files.

Auth is JWT-only (the active `ws/auth.py` calls `decode_token`; unlike the
legacy endpoint it does **not** accept `nxs_*` API keys). The token is
delivered either as a `?token=` query param or as the first JSON message
within `NEXUS_WS_AUTH_TIMEOUT_SECONDS` (default 5 s). The handshake:

```
client                                   server
  │── accept ────────────────────────────────▶│
  │── {"type":"auth","token":"<jwt>"} ────────▶│   (5 s window)
  │                                          │── {"type":"ack","status":"ok","message":"connected"}
  │── {"type":"subscribe","channel":"portfolio","params":{...}}─▶│
  │◀──── {"type":"ack","status":"ok","room":"portfolio:..."} ──│
  │◀──── {"type":"event","channel":...,"room":...,"payload":{...},"seq":N} ──│  (broadcasts)
  │── {"type":"ping","ref":"1"} ─────────────▶│── {"type":"pong","ref":"1"}
```

Inbound message types (see `protocol.py`): `auth`, `subscribe`,
`unsubscribe`, `ping`. Every message accepts an optional `ref` that
the server echoes back in the matching `ack`/`pong`.

Outbound message types: `ack`, `error`, `event`, `pong`, `close`.

### Channels (valid subscriptions)

| Channel | Sub-keyed by | Room shape |
|---|---|---|
| `portfolio` | account / strategy id | `portfolio:account:<id>`, `portfolio:strategy:<id>` |
| `orders` | symbol / status | `orders:symbol:<sym>`, `orders:status:<status>` |
| `strategies` | strategy id | `strategies:strategy:<id>` |

Each connection is also auto-joined to a private `user:<user_id>` room
on registration, so user-scoped events can be targeted directly.

### Auth & scopes

`authenticate_websocket` (`ws/auth.py`) accepts the JWT from either a
`?token=` query parameter or the first `auth` message. Prefer the
first-message form — query strings are recorded by reverse proxies and
log aggregators. Auth attempts are rate-limited per IP
(`NEXUS_WS_AUTH_RATE_LIMIT_PER_MINUTE`, default 10) via a token bucket.

Connection scopes are derived from the JWT `role` claim (see
[`ws/auth.py:_extract_scopes`](../engine/api/ws/auth.py#L80)):

| Role | Scopes granted |
|---|---|
| `admin`, `portfolio_manager` | base + `:all` for every channel |
| all others (`viewer` … `quant_dev`) | base `read:<channel>` only |

Permission checks (`ws/permissions.py`) run on every `subscribe`:

- `:all` scope → unrestricted access to the channel.
- base scope only → **owner-based** access: the channel's owner param
  (`account_id` / `strategy_id`) in `params` must equal the caller's
  `user_id`, else `403`.
- neither → `403`. Unknown channel → `error_code:"404"`. Subscription
  cap exceeded (`NEXUS_WS_MAX_SUBSCRIPTIONS_PER_CONNECTION`) → `429`.

Mid-session, a client can send `{"type":"auth","token":"<new JWT>"}` to
refresh an expiring token; the server re-derives scopes on the live
connection.

### Event delivery

[`ws/event_bridge.py`](../engine/api/ws/event_bridge.py)
(`EventBusBridge`) subscribes to the [`EventBus`](../engine/events/bus.py)
for portfolio / order / strategy event types and broadcasts each to the
matching room(s) as an `event` message with a per-room `seq`. Because
the `EventBus` itself publishes over Redis/Valkey pub/sub, events
published on **any** replica reach local WebSocket connections on
**every** replica. The `ConnectionManager` (the live socket objects) is
still per-process, but event distribution is cross-replica.

### Second endpoint — `WS /api/v1/ws/events`

A second streaming route, `WS /api/v1/ws/events` (source:
[`ws/events.py`](../engine/api/ws/events.py)), shares the same
`ConnectionManager`, `ChannelResolver`, `EventBusBridge`, and wire
protocol as `/ws`, but authenticates **more strictly**: it validates
the session token from a query param **before** `ws.accept()`, so a
bad or missing token rejects the WebSocket handshake (close code
`4401`, reason `invalid session token`) and the server never upgrades
an unauthenticated socket. `/ws` deliberately relaxed this to permit
an in-band first-message `auth`; `/ws/events` trades that flexibility
for fail-closed auth at the handshake.

- **Token**: `?token=<jwt>` (alias `?session_token=`), validated with
  the same `decode_token` the REST dependency uses; scopes via the
  shared `extract_scopes`. **JWT-only** — no `nxs_*` API keys, same
  as `/ws`.
- **Server not ready**: if hit before `init_ws_events`, the socket is
  closed with code `1011` (`WS_CLOSE_SERVER_ERROR`, reason
  `server not ready`).
- **Actionable inbound messages**: `subscribe`, `unsubscribe`, `ping`
  (parsed by the shared `parse_inbound`). Mid-session token refresh is
  **not** supported here — the token is bound to the handshake, so a
  new token requires a new connection (re-connect rather than re-auth).
- **Outbound**: same `ack` / `error` / `event` / `pong` / `close` set
  as `/ws`; the channels, room shapes, and per-role scope rules in the
  tables above apply unchanged.
- **Wiring**: `init_ws_events(manager, resolver?, bridge?)` runs on
  startup and captures the running loop first; a re-init cleanly
  disconnects every existing client and stops the previous bridge
  before installing the new one, so a config reload leaks no
  connections or double event-bus subscriptions.

Prefer `/ws/events` when the client can put the token in the handshake
query (fewer moving parts, fail-closed auth). Prefer `/ws` when the
token can only be delivered after the socket opens (e.g. a browser
that refreshes the token in-band).

## Errors

- **Auth**: `401` for missing/invalid/expired credentials; `403` for
  insufficient role/scope; **`451`** for missing legal acceptance
  (body `{code:"legal_re_acceptance_required", documents:[…]}`).
- **Validation**: `422` from FastAPI; `400` for hand-rolled checks
  (e.g. invalid scope in API keys, unknown tax jurisdiction).
- **Rate limit**: `429` with `Retry-After` from
  [`RateLimitMiddleware`](../engine/api/rate_limit.py). Default 600
  req/min/IP, burst 60. `/health` and `/metrics` are exempt;
  `/api/v1/client/errors` is capped at 30/min to prevent log DoS.
- **Body size**: hard 1 MiB cap on every request
  ([`BodySizeLimitMiddleware`](../engine/api/body_size_limit.py)).
- **Provider errors**: see Market data section above.

## Cross-cutting middleware

Applied in reverse order in [`create_app`](../engine/app.py#L154) so the
last-added wraps everything:

1. `SecurityHeadersMiddleware` — CSP, HSTS, X-Content-Type-Options, …
2. `CORSMiddleware` — `NEXUS_CORS_ORIGINS` (defaults to `http://localhost:3000`).
3. `RateLimitMiddleware`
4. `BodySizeLimitMiddleware` (1 MiB)
5. `CorrelationIdMiddleware` — stamps `X-Request-ID`.
6. `HttpMetricsMiddleware` — Prometheus histogram + counter for every
   route (including `/metrics` itself, deliberately, so scrape latency
   is observable).
