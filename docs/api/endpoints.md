# API endpoint reference

Per-endpoint detail. See [README.md](README.md) for the auth model,
conventions, and group index.

## Health & observability (no auth)

### `GET /health`
Liveness probe. Always returns 200 if the process is up.
**Response:** `{"status": "ok"}`

### `GET /health/providers`
Health check for every registered market-data provider.
**Response:**
```json
{
  "status": "ok | degraded | down",
  "providers": {
    "yahoo": {"status": "up", "latency_ms": 142, "detail": null}
  }
}
```

### `GET /ready`
Readiness probe. Runs `SELECT 1` against Postgres and `PING`
against Valkey. Returns 200 even when degraded — operators decide
based on the component fields.
**Response:** `{"status": "ok", "db": "ok", "valkey": "ok"}`.

### `GET /metrics`
Prometheus exposition. Exempt from the rate limiter.

## Auth (`/api/v1/auth`)

Source: [`engine/api/routes/auth.py`](../../engine/api/routes/auth.py).

### `POST /api/v1/auth/register`
Create a new local account. Disabled when
`NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=false`. Password minimum
length: 8.
**Request:** `RegisterRequest`
```json
{ "email": "user@example.com", "password": "...", "display_name": "User" }
```
**Response (201):** `TokenResponse` (see `/login`).
**Errors:** `409 Conflict` if email is registered.

### `POST /api/v1/auth/login`
**Request:** `LoginRequest`
```json
{ "email": "user@example.com", "password": "..." }
```
**Response (200):** either `TokenResponse` or, for MFA-enabled
users, `MFARequiredResponse`:
```json
{ "mfa_required": true, "challenge_token": "<short-lived jwt>" }
```
The challenge token is then used with `POST /api/v1/auth/mfa/verify`.
`TokenResponse`:
```json
{ "access_token": "<jwt>", "refresh_token": "<opaque>",
  "token_type": "bearer", "expires_in": 3600 }
```
**Errors:** `401 Unauthorized` on bad credentials.

### `POST /api/v1/auth/refresh`
Rotate a refresh token. Single-use — the presented token is
revoked atomically; if the same token is presented twice, **all**
the user's sessions are revoked (replay detection).
**Request:** `{"refresh_token": "..."}` **Response:** `TokenResponse`.

### `GET /api/v1/auth/me` 🔒
Current caller's profile.
**Response:** `UserProfileResponse`
```json
{ "id": "<uuid>", "email": "...", "display_name": "...",
  "role": "developer", "auth_provider": "local", "is_active": true }
```

### `POST /api/v1/auth/logout` 🔒
Revokes either the presented refresh token (if included in the
body) or every active session for the caller.
**Response:** `{"status": "logged_out"}`.

### `GET /api/v1/auth/{provider}/authorize`
Begin an OAuth/OIDC/LDAP flow. `provider` ∈ {`google`, `github`,
`oidc`, `ldap`}. Only providers listed in `NEXUS_AUTH_PROVIDERS`
are wired.
**Response:** `{"authorize_url": "..."}` for OAuth; LDAP returns
405 with a pointer to the SPNEGO flow.

### `GET /api/v1/auth/{provider}/callback`
OAuth callback target. Validates `code`, exchanges for provider
tokens, looks up / creates the local `User`, and redirects to the
frontend with `access_token` and `refresh_token` in the fragment.

## MFA (`/api/v1/auth/mfa`)

Source: [`engine/api/routes/mfa.py`](../../engine/api/routes/mfa.py).
TOTP (RFC 6238). Secrets encrypted at rest with Fernet
(`NEXUS_MFA_ENCRYPTION_KEY`).

| Method                                | Auth          | Purpose |
|---------------------------------------|---------------|---------|
| `POST /api/v1/auth/mfa/enroll`        | bearer JWT    | Begin enrollment. Returns shared secret + `otpauth://` URI. |
| `POST /api/v1/auth/mfa/enroll/confirm`| bearer JWT    | Confirm enrollment with a valid 6-digit code. Returns one-time backup codes. |
| `POST /api/v1/auth/mfa/verify`        | challenge token | Complete an MFA-challenged login. |
| `POST /api/v1/auth/mfa/disable`       | bearer JWT    | Disable MFA. Requires current password + valid TOTP code. |
| `POST /api/v1/auth/mfa/backup-codes/regen` | bearer JWT | Regenerate backup codes. Requires valid TOTP. |

## API keys (`/api/v1/auth/api-keys`)

Source: [`engine/api/routes/api_keys.py`](../../engine/api/routes/api_keys.py).
Plaintext token returned only on `POST`.

| Method                                   | Auth | Scope |
|------------------------------------------|------|-------|
| `POST /api/v1/auth/api-keys`             | JWT  | —     |
| `GET  /api/v1/auth/api-keys`             | JWT  | —     |
| `DELETE /api/v1/auth/api-keys/{key_id}`  | JWT  | —     |

**Create request:**
```json
{ "name": "ci-runner", "scopes": ["read", "trade"],
  "expires_at": "2026-12-31T00:00:00Z", "env": "ci" }
```
**Create response (201):** `ApiKeyCreatedResponse`
```json
{ "id": "<uuid>", "name": "ci-runner", "prefix": "nxs_abcd",
  "scopes": ["read", "trade"], "token": "nxs_abcd_<opaque>",
  "last_used_at": null, "expires_at": "...", "revoked_at": null,
  "created_at": "..." }
```

## Legal (`/api/v1/legal`)

Source: [`engine/api/routes/legal.py`](../../engine/api/routes/legal.py).

| Method                                                | Auth          | Purpose |
|-------------------------------------------------------|---------------|---------|
| `GET  /api/v1/legal/documents`                        | optional      | List; `?category=` filter. |
| `GET  /api/v1/legal/documents/{slug}`                 | optional      | Markdown body (front-matter stripped, operator substitutions applied). |
| `POST /api/v1/legal/accept`                           | bearer JWT    | Record acceptance of `[{slug, version}, …]`. |
| `GET  /api/v1/legal/acceptances/me`                   | bearer JWT    | Caller's acceptance history. |
| `GET  /api/v1/legal/attributions`                     | none          | Data-provider attributions (`?context=`). |

## Portfolio (`/api/v1/portfolio`)

Source: [`engine/api/routes/portfolio.py`](../../engine/api/routes/portfolio.py).
JWT + legal acceptance.

| Method                                | Purpose |
|---------------------------------------|---------|
| `POST   /api/v1/portfolio/`           | Create. |
| `GET    /api/v1/portfolio/`           | List caller's. |
| `GET    /api/v1/portfolio/{id}`       | Fetch one (403 if not owned). |
| `DELETE /api/v1/portfolio/{id}`       | Archive (soft-delete in current impl). |

**Create request:**
```json
{ "name": "Long-term", "description": "Buy & hold",
  "initial_capital": 100000.0 }
```

## Backtest (`/api/v1/backtest`)

Source: [`engine/api/routes/backtest.py`](../../engine/api/routes/backtest.py).
JWT + legal acceptance.

### `POST /api/v1/backtest/run`
Enqueue a backtest. Runs in a `BackgroundTasks` coroutine on the
API process — see [limitations](../limitations.md) for the
follow-up plan.

**Request:** `BacktestRequest`
```json
{
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "start_date": "2022-01-01",
  "end_date": "2024-12-31",
  "initial_capital": 100000.0,
  "config": {}
}
```
**Response (202):** `{"status": "accepted", "backtest_id": "<uuid>"}`

### `GET /api/v1/backtest/results/{backtest_id}`
Fetch the result. Results are kept in an in-process dict keyed by
`backtest_id` with a 1-hour TTL. **Lost on process restart.**

**Response (200):** `BacktestResultResponse` — full metrics summary
(Sharpe, Sortino, max drawdown, win rate, distribution metrics,
equity / drawdown curves, rolling metrics, benchmark comparison).
**Response (404):** result missing or expired.

## Strategies (`/api/v1/strategies`)

Source: [`engine/api/routes/strategies.py`](../../engine/api/routes/strategies.py).
Operates on the in-process `PluginRegistry`. JWT + legal acceptance.

| Method                                         | Purpose |
|------------------------------------------------|---------|
| `GET   /api/v1/strategies/`                    | List installed + status. |
| `GET   /api/v1/strategies/{id}`                | Strategy detail (manifest, config schema, watchlist). |
| `POST  /api/v1/strategies/{id}/activate`       | Instantiate with `StrategyConfigRequest`. |
| `POST  /api/v1/strategies/{id}/deactivate`     | Unload. |
| `POST  /api/v1/strategies/{id}/reload`         | Hot-reload from disk. |
| `GET   /api/v1/strategies/{id}/health`         | Liveness of an active instance. |

**Activate request:** `{"params": {"lookback": 20}}`

## Marketplace (`/api/v1/marketplace`)

Source: [`engine/api/routes/marketplace.py`](../../engine/api/routes/marketplace.py).
**Currently a stub** — browse/categories return hard-coded shapes;
install/uninstall/rate return `{"status": "not_implemented"}`.
JWT + legal acceptance.

| Method                                              | Notes |
|-----------------------------------------------------|-------|
| `GET    /api/v1/marketplace/browse`                 | `?page=&per_page=&category=&search=&sort_by=`. |
| `GET    /api/v1/marketplace/categories`             | Hard-coded: algorithmic / ml / llm / hybrid / income / macro. |
| `POST   /api/v1/marketplace/install`                | `developer` role. **Not implemented.** |
| `DELETE /api/v1/marketplace/uninstall/{id}`         | `developer` role. **Not implemented.** |
| `POST   /api/v1/marketplace/{id}/rate?rating=&review=` | 1–5 scale. **Not implemented.** |

## Market data (`/api/v1/market-data`)

Source: [`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py).
JWT + legal acceptance. Dispatches to the provider registry; see
[`engine/data/providers/registry.py`](../../engine/data/providers/registry.py).

### `GET /api/v1/market-data/{symbol}/bars`
Query params: `interval` (default `1d`), `period` (default `1y`),
`provider` (optional), `asset_class` (optional).

**Response:** `BarsResponse`
```json
{ "symbol": "AAPL", "interval": "1d", "period": "1y",
  "asset_class": "equity", "provider": "yahoo",
  "bars": [{"ts": "...", "open": ..., "high": ..., "low": ...,
            "close": ..., "volume": ...}] }
```
**Errors:** `503 Upstream provider unavailable` (transient),
`400 Bad Request` (fatal), `501 Not Implemented` (no provider for
this asset class).

### `GET /api/v1/market-data/{symbol}/quote`
Real-time quote. Same params as `/bars` minus `interval` / `period`.

## Reference / instrument search (`/api/v1/reference`)

Source: [`engine/api/routes/reference.py`](../../engine/api/routes/reference.py).

### `GET /api/v1/reference/suggest`
Symbol / name autocomplete. Hits the in-memory `SearchIndex`
first, falls back to Yahoo Finance search if local returns
nothing.
**Query:** `?q=apple&limit=10&asset_class=equity`
**Response:** `{"suggestions": [{"symbol": "AAPL",
"name": "Apple Inc.", "exchange": "NAS", "asset_class": "equity",
"type": "EQ"}, …]}`

## Scoring (`/api/v1/scoring`)

Source: [`engine/api/routes/scoring.py`](../../engine/api/routes/scoring.py).
JWT + legal acceptance.

### `POST /api/v1/scoring/{strategy_name}/run`
**Request:** `ScoringRunRequest`
```json
{ "universe": ["AAPL", "MSFT", "GOOG"],
  "raw_data": { "AAPL": {"momentum": 0.12, "value": null}, … } }
```
**Response:** `ScoringRunResponse`
```json
{ "strategy_id": "value_momentum_composite",
  "scores": [{"symbol": "AAPL", "score": 0.84, "rank": 1}, …],
  "excluded_factors": ["value"],
  "universe_size": 3 }
```

### `GET /api/v1/scoring/{strategy_name}/results`
Paginated history. `?limit=20&offset=0&sort_by=created_at&sort_order=desc`.

## Tax (`/api/v1/tax`)

Source: [`engine/api/routes/tax.py`](../../engine/api/routes/tax.py).
JWT required. No persistence — caller re-submits disposals per call.

### `POST /api/v1/tax/report/{code}`
`code` is a jurisdiction slug, case-insensitive: **US, GB, DE, FR**.

**Request:** `TaxReportRequest`
```json
{ "disposals": [{
    "description": "AAPL 100 sh",
    "acquired": "2022-01-15",
    "disposed": "2024-06-30",
    "proceeds": "19500.00",
    "cost": "12000.00"
}] }
```
**Response:** `{"jurisdiction": "US", "summary": {…}}` — shape
depends on the summariser in
[`engine/core/tax/reports/`](../../engine/core/tax/reports/).

### `POST /api/v1/tax/report/{code}/csv`
Same payload, response is `text/csv` (2 rows: header + values).

## Webhooks (`/api/v1/webhooks`)

Source: [`engine/api/routes/webhooks.py`](../../engine/api/routes/webhooks.py).
JWT + `trade` scope. Templates: `generic | discord | slack | telegram`.

| Method                                              | Purpose |
|-----------------------------------------------------|---------|
| `POST   /api/v1/webhooks`                           | Register. `signing_secret` returned only on create. |
| `GET    /api/v1/webhooks`                           | List caller's. |
| `PUT    /api/v1/webhooks/{id}`                      | Update URL / events / template. |
| `DELETE /api/v1/webhooks/{id}`                      | Delete. |
| `POST   /api/v1/webhooks/{id}/test`                 | Fire a synthetic `webhook.test` event. |
| `GET    /api/v1/webhooks/{id}/deliveries`           | Delivery history (last 100). |

Deliveries are HMAC-SHA256 signed with the `signing_secret`. Valid
event types are the `EventType` enum values from
[`engine/events/bus.py`](../../engine/events/bus.py): market,
signal, order, portfolio, strategy, risk, system events.

## Privacy / DSR (`/api/v1/privacy`)

Source: [`engine/api/routes/privacy.py`](../../engine/api/routes/privacy.py).
JWT required. Implements GDPR / CCPA data-subject requests.

| Method                                | Purpose |
|---------------------------------------|---------|
| `POST /api/v1/privacy/export`         | Synchronous export of caller's data. Records DSR row with `kind=export`. |
| `POST /api/v1/privacy/delete`         | Initiate deletion. 30-day grace window before the deletion job runs. |
| `POST /api/v1/privacy/delete/cancel`  | Cancel during grace window. |
| `GET  /api/v1/privacy/delete/status`  | `pending`, remaining grace, request summary. |
| `GET  /api/v1/privacy/requests`       | DSR history for the caller. |
| `GET  /api/v1/privacy/kinds`          | Allow-list of DSR kinds (for UIs). |

The GDPR Art. 12 SLA (1 month) is tracked in `DSRequest.sla_due_at`.

## System (`/api/v1/system`)

Source: [`engine/api/routes/system.py`](../../engine/api/routes/system.py).
JWT required.

### `GET /api/v1/system/status`
```json
{
  "engine_version": "0.1.0",
  "uptime_seconds": 1832.4,
  "server_time": "2026-06-04T12:00:00Z",
  "components": [
    {"name": "database", "healthy": true, "detail": null}
  ],
  "counts": { "users": 3, "portfolios": 7, "backtest_results": 42,
              "webhook_configs": 2, "api_keys": 1 }
}
```

## Client errors (`/api/v1/client`)

Source: [`engine/api/routes/client_errors.py`](../../engine/api/routes/client_errors.py).
Rate-limited to 30 req/min/IP (caps render-loop ErrorBoundary floods).

### `POST /api/v1/client/errors`
Frontend `ErrorBoundary` sink. Body is the serialized error.
Returns `204 No Content`.

## WebSocket (`/api/v1/ws`)

Source: [`engine/api/routes/websocket.py`](../../engine/api/routes/websocket.py).
JWT or `nxs_*` API key. The token **must** be sent in the first
frame, not in the URL — query strings leak into proxy logs.

Protocol:

1. Client opens `WS /api/v1/ws`. Server accepts.
2. Client sends `{"type": "auth", "token": "<jwt or nxs_…>"}` within
   10 seconds (`AUTH_TIMEOUT_SECONDS`).
3. Server replies `{"type": "auth.ok", "user_id": "<uuid>"}` or
   closes with `{"type": "auth.failed", "detail": "..."}`.
4. Subscribe / unsubscribe:
   ```json
   {"type": "subscribe",   "topics": ["portfolio", "order"]}
   {"type": "unsubscribe", "topics": ["portfolio"]}
   ```
   Valid topics: `market`, `signal`, `portfolio`, `backtest`,
   `order`, `alert` (from `Topic` enum in
   [`engine/api/websocket/manager.py`](../../engine/api/websocket/manager.py)).
5. Either side may send `{"type": "ping"}`; peer replies
   `{"type": "pong"}`.

Event payloads are wrapped: `{"type": "event", "topic": "...",
"event_type": "...", "data": {...}}`.
