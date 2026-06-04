# API reference

This page documents every HTTP and WebSocket endpoint the engine
exposes. The source of truth for routes is
[`engine/api/router.py`](../engine/api/router.py); the source of
truth for auth is
[`engine/api/auth/dependency.py`](../engine/api/auth/dependency.py).

For interactive exploration, the engine serves OpenAPI at
`/docs` (Swagger UI) and `/redoc` (ReDoc) when `NEXUS_APP_DEBUG=1`.

## Authentication

Every authenticated route accepts one of:

| Mechanism                | Header                                       | Verified by |
|--------------------------|----------------------------------------------|-------------|
| Bearer JWT               | `Authorization: Bearer <jwt>`                | `engine/api/auth/jwt.py:decode_token` |
| Bearer API key           | `Authorization: Bearer nxs_<prefix><secret>` | `engine/api/auth/api_keys.py:find_active_by_token` |
| API key header           | `X-API-Key: nxs_<prefix><secret>`            | Same as above |

JWT-authenticated requests are gated by **role checks**
(`require_role("developer")`). API-key requests are gated by
**scope checks** (`require_api_scope("trade")`). The two systems
are independent; see ADR-0007 for the rationale.

Roles (ascending): `viewer < user < retail_trader < quant_dev <
developer < portfolio_manager < admin`.

Scopes (ascending): `read < trade < admin`. `read` permits
GET/HEAD only; `trade` permits POST/PUT/PATCH for backtest,
portfolio, webhooks; `admin` is full access.

## Legal-acceptance gate

Routes marked **(legal-gated)** additionally require that the
caller has accepted every `requires_acceptance=true` legal
document at its `current_version`. Enforced by
[`engine/legal/dependencies.py`](../engine/legal/dependencies.py).

## Common response shapes

Every error response is a JSON object with a `detail` string:

```json
{ "detail": "Authentication required" }
```

Status codes are conventional:

| Code | Meaning                                                |
|------|--------------------------------------------------------|
| 400  | Validation failure (Pydantic, business invariant).     |
| 401  | Missing or invalid token.                              |
| 403  | Authenticated but insufficient role/scope/legal.       |
| 404  | Resource not found / not owned by caller.              |
| 409  | Conflict (duplicate email, pending DSR, MFA already on). |
| 429  | Rate-limited (per `NEXUS_RATE_LIMIT_*`).               |
| 500  | Unhandled server error. Sentry-captured.              |
| 503  | Upstream provider down or service degraded.            |

## Health & observability

Unauthenticated.

### `GET /health`
Returns `{"status": "ok"}`. Liveness probe.

### `GET /ready`
Readiness probe. Checks DB and Valkey reachability:

```json
{ "status": "ok", "db": "ok", "valkey": "ok" }
```
Status is `"degraded"` if any component fails.

### `GET /health/providers`
Health of every registered market-data provider. Returns per-
provider `status` (`up`/`down`), `latency_ms`, `detail`.

### `GET /metrics`
Prometheus exposition format. Unauthenticated by design — guard
at the network edge.

## Auth

### `POST /api/v1/auth/register`
Register a new local-account user. Enabled only when
`NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=true`.

Request:
```json
{ "email": "user@example.com", "password": "secret123",
  "display_name": "Alice" }
```
Response `201`:
```json
{ "access_token": "<jwt>", "refresh_token": "<raw>",
  "token_type": "bearer", "expires_in": 3600 }
```

### `POST /api/v1/auth/login`
Local login. If the user has MFA enabled, returns a challenge
instead of tokens:

Request: `{"email": "...", "password": "..."}`

Response when MFA disabled: `TokenResponse` (as above).
Response when MFA enabled: `{"mfa_required": true,
"challenge_token": "<short-lived>"}` — feed the challenge to
`POST /api/v1/auth/mfa/verify` with a TOTP code.

### `POST /api/v1/auth/refresh`
Rotate a refresh token. Implements replay detection: if the same
refresh token is presented twice, every live refresh token for
that user is revoked atomically.

Request: `{"refresh_token": "<raw>"}`.
Response: `TokenResponse`.

### `POST /api/v1/auth/logout`
Revokes the supplied refresh token (if any) or every live
refresh token for the user. Authenticated.

### `GET /api/v1/auth/me`
Returns the caller's profile. Authenticated.

Response:
```json
{ "id": "<uuid>", "email": "...", "display_name": "...",
  "role": "user", "auth_provider": "local", "is_active": true }
```

### `GET /api/v1/auth/{provider}/authorize`
Returns the OAuth2/OIDC authorization URL for the named provider
(`google`, `github`, `oidc`, `ldap`). Sets a state cookie.

### `GET /api/v1/auth/{provider}/callback`
OAuth2/OIDC callback. Validates the state cookie, exchanges the
code for user info, looks up or provisions the local user, and
returns `TokenResponse`.

## MFA

All routes are authenticated.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/auth/mfa/enroll` | Begin enrollment; returns TOTP secret + otpauth URI. |
| `POST` | `/api/v1/auth/mfa/enroll/confirm` | Verify first code, persist encrypted secret, return backup codes. |
| `POST` | `/api/v1/auth/mfa/verify` | Complete an MFA-gated login with a challenge token + TOTP code. Returns tokens. |
| `POST` | `/api/v1/auth/mfa/disable` | Disable MFA (requires current password + valid code). |
| `POST` | `/api/v1/auth/mfa/backup-codes/regen` | Generate a new backup-code batch (requires valid TOTP code). |

## API keys

Authenticated. Routes mounted at `/api/v1/auth/api-keys`.

### `POST /api/v1/auth/api-keys`
Issue a new API key. The full token is returned **only in this
response**; subsequent reads return only the prefix.

Request:
```json
{ "name": "CI", "scopes": ["read"], "expires_at": "2026-12-31T00:00:00Z",
  "env": "ci" }
```
Response `201` (extends `ApiKeySummary` with `token`):
```json
{ "id": "<uuid>", "name": "CI", "prefix": "nxs_abcd1234",
  "scopes": ["read"], "token": "nxs_abcd1234<was-shown-once>",
  "last_used_at": null, "expires_at": "...", "revoked_at": null,
  "created_at": "..." }
```

### `GET /api/v1/auth/api-keys`
List the caller's API keys (excludes the secret).

### `DELETE /api/v1/auth/api-keys/{key_id}`
Revoke an API key. Returns `204 No Content`.

## Portfolios (legal-gated)

### `POST /api/v1/portfolio/`
Create a portfolio. Request: `{"name": "...", "description":
"...", "initial_capital": 100000.0}`. Returns `PortfolioResponse`.

### `GET /api/v1/portfolio/`
List the caller's portfolios. Returns `PortfolioResponse[]`.

### `GET /api/v1/portfolio/{portfolio_id}`
Get one portfolio. Returns 403 if not owned by caller.

### `DELETE /api/v1/portfolio/{portfolio_id}`
Delete one portfolio. (Today this is a hard delete; soft-delete
is on the roadmap.)

## Backtest (legal-gated)

### `POST /api/v1/backtest/run`
Enqueue a backtest as a background task.

Request:
```json
{ "strategy_name": "mean_reversion_basic", "symbol": "AAPL",
  "start_date": "2022-01-01", "end_date": "2024-12-31",
  "initial_capital": 100000.0, "config": {} }
```
Response `200`:
```json
{ "status": "accepted", "backtest_id": "<uuid>" }
```

### `GET /api/v1/backtest/results/{backtest_id}`
Poll a backtest. Returns one of:

- `202` with `status="running"` while the task is in flight.
- `200` with full `BacktestResultResponse` on completion.
- `200` with `status="failed"` and an `error` field on failure.
- `404` if the id is unknown or has expired (results are kept
  in-memory for 1 hour; persistence to `backtest_results` is
  done by the strategy evaluator for scoring runs only).

`BacktestResultResponse` carries the full `MetricsSummary`
(Sharpe, Sortino, Calmar, max drawdown + duration + recovery,
volatility, win rate, profit factor, best/worst trade, cost
drag, turnover, exposure, rolling windows), the equity curve,
and the drawdown curve.

## Strategies (legal-gated)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/strategies/` | List all installed strategies and their status. |
| `GET` | `/api/v1/strategies/{strategy_id}` | Get manifest details (config schema, data feeds, watchlist, sandbox flags). |
| `POST` | `/api/v1/strategies/{strategy_id}/activate` | Instantiate + activate with config. |
| `POST` | `/api/v1/strategies/{strategy_id}/deactivate` | Unload. |
| `POST` | `/api/v1/strategies/{strategy_id}/reload` | Hot-reload strategy code from disk. |
| `GET` | `/api/v1/strategies/{strategy_id}/health` | Sandbox runtime metrics for an active strategy. |

## Scoring (legal-gated)

### `POST /api/v1/scoring/{strategy_name}/run`
Run a scoring strategy against a universe of symbols.

Request:
```json
{ "universe": ["AAPL", "MSFT", "NVDA"],
  "raw_data": { "AAPL": {"pe": 28.5, "roa": 0.22}, ... } }
```
Response:
```json
{ "strategy_id": "value_score", "scores": [{"symbol": "AAPL",
  "score": 0.81, ...}], "excluded_factors": ["pe"],
  "universe_size": 3 }
```

### `GET /api/v1/scoring/{strategy_name}/results`
Paginated history of scoring snapshots. Supports `limit`,
`offset`, `sort_by`, `sort_order` query params.

## Market data (legal-gated)

### `GET /api/v1/market-data/{symbol}/bars`
Historical OHLCV bars.

Query params:

| Param | Default | Notes |
|---|---|---|
| `interval` | `1d` | Bar interval (`1m`, `5m`, `1h`, `1d`, ...). |
| `period` | `1y` | History length (`1mo`, `3mo`, `1y`, `5y`, ...). |
| `provider` | (auto) | Pin a specific provider by name. |
| `asset_class` | (inferred) | Override auto-detection: `equity`, `etf`, `crypto`, `forex`, ... |

Response:
```json
{ "symbol": "AAPL", "interval": "1d", "period": "1y",
  "asset_class": "equity", "provider": "yahoo",
  "bars": [{"timestamp": "...", "open": ..., "high": ...,
  "low": ..., "close": ..., "volume": ...}, ...] }
```

Errors: `400` for invalid symbols; `503` when every candidate
provider fails; `501` if no provider supports the requested
capability.

### `GET /api/v1/market-data/{symbol}/quote`
Latest price for a symbol. Same query params and error semantics
as `/bars`.

## Tax

### `POST /api/v1/tax/report/{code}`
Generate a per-jurisdiction tax summary. `code` is one of `US`,
`GB`, `DE`, `FR` (case-insensitive). Authenticated.

Request:
```json
{ "disposals": [{ "description": "AAPL 100 sh",
  "acquired": "2023-01-15", "disposed": "2024-02-01",
  "proceeds": "19500.00", "cost": "15000.00" }, ...] }
```
Response: `{"jurisdiction": "US", "summary": { ... }}`. The
summary shape is jurisdiction-specific; see
[`engine/core/tax/reports/`](../engine/core/tax/reports/) for
the dataclass definitions.

### `POST /api/v1/tax/report/{code}/csv`
Same dispatch, response formatted as a 2-row CSV (header +
values). Suitable for spreadsheet round-trips.

## Webhooks

Authenticated. Routes mounted at `/api/v1/webhooks`. Write
operations require the `trade` scope when authenticated via API
key.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/webhooks` | Create a webhook config. Signing secret is returned only here. |
| `GET` | `/api/v1/webhooks` | List caller's webhooks. |
| `PATCH` | `/api/v1/webhooks/{id}` | Update (url, event_types, headers, template, max_retries, is_active). |
| `DELETE` | `/api/v1/webhooks/{id}` | Delete. |
| `POST` | `/api/v1/webhooks/{id}/test` | Send a test event. |
| `GET` | `/api/v1/webhooks/{id}/deliveries` | Delivery history (paginated). |

Templates: `generic`, `discord`, `slack`, `telegram`.

## Privacy / DSR

Authenticated. Routes mounted at `/api/v1/privacy`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/export` | Synchronous export of the caller's data (JSON). |
| `POST` | `/delete` | Initiate account deletion (30-day grace). Returns `202`. |
| `POST` | `/delete/cancel` | Cancel deletion during grace window. |
| `GET` | `/delete/status` | Pending? + remaining grace. |
| `GET` | `/requests` | Caller's DSR history. |
| `GET` | `/kinds` | Allow-list of DSR kinds (`export`, `delete`, `rectify`, `restrict`, `object`). |

## Marketplace (legal-gated)

Browse endpoints are live; install/uninstall/rate return
`{"status": "not_implemented"}` until the marketplace lands
fully.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/marketplace/browse` | Browse + search strategies (paginated). |
| `GET` | `/api/v1/marketplace/categories` | Static category list. |
| `POST` | `/api/v1/marketplace/install` | Install a strategy. *(not implemented)* |
| `DELETE` | `/api/v1/marketplace/uninstall/{strategy_id}` | Uninstall. *(not implemented)* |
| `POST` | `/api/v1/marketplace/{strategy_id}/rate` | Rate + review. *(not implemented)* |

## Reference data

### `GET /api/v1/reference/suggest`
Typeahead search for instruments. Hits the local search index
first, falls back to Yahoo Finance search.

Query params: `q=<query>` (required, max 64 chars), `limit=10`
(max 50), `asset_class=<optional>`.

Response:
```json
{ "suggestions": [{ "symbol": "AAPL", "name": "Apple Inc.",
  "display": "AAPL — Apple Inc.", "score": 100,
  "record": { "id": "...", "primary_ticker": "AAPL",
  "primary_venue": "NMS", "asset_class": "equity",
  "name": "Apple Inc.", "currency": "USD" } }, ...] }
```

## Legal

### `GET /api/v1/legal/documents`
List all current legal documents. Optional `?category=` filter.
Auth is optional — logged-in users see acceptance status
alongside each document.

### `GET /api/v1/legal/documents/{slug}`
Render a single document. Markdown body returned with
operator-substitution tokens (`{{OPERATOR_NAME}}`,
`{{EFFECTIVE_DATE}}`, etc.) replaced.

### `POST /api/v1/legal/accept`
Record user acceptances. Body: `{"acceptances": [{"slug": "terms",
"version": "1.0"}, ...]}`.

### `GET /api/v1/legal/acceptances/me`
List the caller's acceptance history. Optional
`?document_slug=` filter.

### `GET /api/v1/legal/attributions`
Attribution list for displayed market data. Optional
`?context=` filter.

## Client error reporting

### `POST /api/v1/client/errors`
Unauthenticated. The frontend's `ErrorBoundary` posts unhandled
exceptions here. CRLF and ANSI sequences are stripped; URLs are
reduced to scheme+host+path (auth tokens in query strings must
not flow into the audit trail). Rate-limited to 30 req/min/IP.

Request:
```json
{ "message": "...", "stack": "...", "component_stack": "...",
  "url": "...", "user_agent": "...", "error_id": "<uuid-or-null>" }
```
Response `201`: `{"error_id": "<uuid>"}`.

## System status

### `GET /api/v1/system/status`
Authenticated. Engine version, uptime, DB reachability, and
active counts (users, portfolios, backtests, webhooks, API keys).

Response:
```json
{ "engine_version": "0.1.0", "uptime_seconds": 1234.5,
  "server_time": "2026-06-04T12:34:56Z",
  "components": [{"name": "database", "healthy": true}],
  "counts": {"users": 42, "portfolios": 100, "backtests": 500,
  "webhooks_active": 12, "api_keys_active": 7} }
```

## WebSocket

Mounted at `WS /api/v1/ws`.

### Handshake

1. Client opens the connection.
2. Server accepts and waits up to 10 s for an auth message:
   ```json
   { "type": "auth", "token": "<jwt or nxs_*>" }
   ```
3. On success, server replies
   `{"type": "auth.ok", "user_id": "<uuid>"}`.
4. On failure, server closes with code `4400` (missing/invalid
   auth) or `4401` (auth timeout / invalid token).

### Messages (client → server)

| `type`        | Body | Server response |
|---------------|------|-----------------|
| `subscribe`   | `{"topics": ["portfolio", "backtest", ...]}` | `{"type": "subscribed", "topics": [...]}` |
| `unsubscribe` | `{"topics": [...]}` | `{"type": "unsubscribed", "topics": [...]}` |
| `ping`        | `{}` | `{"type": "pong"}` |

Valid topics: `portfolio`, `backtest`, `order`, `alert`.

JWT in the URL is **not** supported — query strings end up in
proxy logs.

### Messages (server → client)

The server pushes topic-routed events from the
[`EventBus`](../engine/events/bus.py) to every subscribed
connection. The shape is event-specific; see
[`engine/events/bus.py:EventType`](../engine/events/bus.py) for
the canonical list.

Multi-replica deployments need the Valkey pub/sub fan-out layer
that the WebSocket manager already exposes the shape for (see
[`engine/api/websocket/manager.py`](../engine/api/websocket/manager.py)).
