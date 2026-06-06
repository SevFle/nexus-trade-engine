# API reference

Every HTTP and WebSocket endpoint exposed by the engine, the auth
required to call it, and the request/response shape. The source of
truth is the FastAPI OpenAPI schema at `/openapi.json` (or `/docs` for
the Swagger UI) — this document is the human-readable companion, not
a replacement.

Routes are mounted by [`engine/api/router.py`](../../engine/api/router.py).
Each section below maps 1:1 to a file in
[`engine/api/routes/`](../../engine/api/routes/).

## Authentication

Every protected endpoint accepts **either**:

- A Bearer JWT in the `Authorization` header:
  ```
  Authorization: Bearer <jwt>
  ```
  Issued by `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, or
  the OAuth callback. HS256. Access tokens expire in
  `NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default 60).

- An API key in the `X-API-Key` header:
  ```
  X-API-Key: nxs_<env>_<32hex>
  ```
  Issued by `POST /api/v1/auth/api-keys`. Format and scope validation
  in [`engine/api/auth/api_keys.py`](../../engine/api/auth/api_keys.py).
  Stored as bcrypt hash.

The dependency `get_current_user`
([`engine/api/auth/dependency.py:102-118`](../../engine/api/auth/dependency.py))
inspects both, in that order. If neither is present the request gets
`401 Unauthorized`.

### Roles and scopes

RBAC roles, low → high privilege
([`engine/api/auth/dependency.py:27-35`](../../engine/api/auth/dependency.py)):

| Role | Level | Notes |
|---|---|---|
| `viewer` | 0 | Read-only |
| `user` | 1 | Default local-account role |
| `retail_trader` | 2 | Trading actions on own portfolios |
| `quant_dev` | 3 | Strategy authoring, backtest |
| `developer` | 4 | Marketplace install, key management |
| `portfolio_manager` | 5 | Multi-portfolio management |
| `admin` | 6 | All operations, role management |

`require_role("developer")` means "this role or higher".

API-key scopes, low → high
([`engine/api/auth/dependency.py:160`](../../engine/api/auth/dependency.py)):

| Scope | Level | Allows |
|---|---|---|
| `read` | 0 | All GET endpoints that require auth |
| `trade` | 1 | Webhook management, strategy activation |
| `admin` | 2 | Key revocation, anything trade + read |

JWT-authenticated requests **bypass** scope checks — the role hierarchy
is the gate. This is intentional so the dashboard UI works without
requiring users to manage scopes.

### MFA

If a user has MFA enabled, `POST /auth/login` returns
`{"mfa_required": true, "challenge_token": "..."}` instead of a JWT.
The client must call `POST /auth/mfa/verify` with the TOTP code to
exchange the challenge token for a real JWT. See MFA section below.

### Legal acceptance

Most trading routes (`/backtest`, `/portfolio`, `/strategies`,
`/scoring`, `/market-data`, `/marketplace`) are wired with
`Depends(require_legal_acceptance)`. **Today this dependency is a
no-op stub** ([`engine/legal/dependencies.py:18-25`](../../engine/legal/dependencies.py));
it will return `451 Unavailable For Legal Reasons` with the pending
document slugs once wired. Privacy and auth routes bypass it
intentionally — a user must be able to read terms and request deletion
without first accepting the terms.

---

## Health and observability

Public, no auth. Mounted without prefix.

### `GET /health` — liveness
[`engine/api/routes/health.py:19-21`](../../engine/api/routes/health.py)

```json
{ "status": "ok" }
```

### `GET /health/providers` — data-provider health
[`engine/api/routes/health.py:24-39`](../../engine/api/routes/health.py)

Per-provider health summary plus overall `ok | degraded | down`.
Use for dashboard status badges.

### `GET /ready` — readiness
[`engine/api/routes/health.py:42-62`](../../engine/api/routes/health.py)

Checks DB connectivity and Valkey ping. Returns `503` if any
dependency is down. Use as the load-balancer target.

### `GET /metrics` — Prometheus exposition
[`engine/api/routes/metrics.py:30-42`](../../engine/api/routes/metrics.py)

`text/plain; version=0.0.4; charset=utf-8`. Renders counters, gauges,
and histograms (as Prometheus `summary` with `_count` + `_sum` only).
Public by design — gate at the proxy.

---

## Auth

Prefix `/api/v1/auth`. Source: [`engine/api/routes/auth.py`](../../engine/api/routes/auth.py).

### `POST /register` — register local user
**Public.** Body:
```json
{ "email": "user@example.com", "password": "secret123",
  "display_name": "Alice" }
```
Password minimum length 8. Disabled when
`NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=false`. Returns `TokenResponse`
(`access_token`, `refresh_token`, `token_type=Bearer`, `expires_in`).

### `POST /login` — local login
**Public.** Body: `{ "email", "password" }`. Returns `TokenResponse`,
or `MFARequiredResponse` (`{ "mfa_required": true, "challenge_token": "..." }`)
if the user has MFA enabled.

### `POST /refresh` — rotate refresh token
**Public.** Body: `{ "refresh_token" }`. Performs atomic rotation:
the old refresh token is revoked, a new pair is issued. Replay
detection (using a revoked token) revokes **all** of the user's
sessions.

### `GET /me` — current user
**Auth required.** Returns `UserProfileResponse` (`id, email,
display_name, role, auth_provider, mfa_enabled, created_at`).

### `POST /logout` — revoke session
**Auth required.** Body: `{ "refresh_token"? }`. Revokes the supplied
refresh token (if any). Always returns `{"status": "logged_out"}`.

### `GET /{provider}/authorize` — start OAuth flow
**Public.** `provider` ∈ `{google, github, oidc}`. Sets an
`oauth_state_<provider>` cookie and redirects to the IdP.

### `GET /{provider}/callback` — finish OAuth flow
**Public.** Validates the state cookie, exchanges the code for an
IdP token, fetches user info, mints local tokens. Returns
`TokenResponse` or redirects to the frontend with tokens in the URL
fragment (depending on `redirect_uri` config).

---

## MFA

Prefix `/api/v1/auth/mfa`. Source: [`engine/api/routes/mfa.py`](../../engine/api/routes/mfa.py).

### `POST /enroll` — begin TOTP enrollment
**Auth required.** Returns `EnrollResponse(secret, otpauth_uri)`.
Conflict (409) if MFA already enabled. The `secret` is unencrypted
in this response only — the database stores it Fernet-encrypted
with `NEXUS_MFA_ENCRYPTION_KEY`.

### `POST /enroll/confirm` — confirm enrollment
**Auth required.** Body: `{ "secret", "code" }`. Returns
`ConfirmResponse(backup_codes: list[str])`. The backup codes are
returned once; we do not log them.

### `POST /verify` — challenge response at login
**Public.** Body: `{ "challenge_token", "code" }`. Exchanges a
challenge token (issued by `/login`) for a JWT pair when the TOTP
code is valid.

### `POST /disable` — disable MFA
**Auth required.** Body: `{ "password", "code" }`. Requires
re-authentication with the user's password plus a current TOTP code.

### `POST /backup-codes/regen` — regenerate backup codes
**Auth required.** Body: `{ "code" }`. Invalidates the previous set
and returns a new one.

---

## API keys

Prefix `/api/v1/auth/api-keys`. Source:
[`engine/api/routes/api_keys.py`](../../engine/api/routes/api_keys.py).

### `POST /` — issue a new key
**Auth required.** Body: `ApiKeyCreateRequest`:
```json
{ "name": "Trading bot",
  "scopes": ["read", "trade"],          // subset of {read,trade,admin}
  "expires_at": "2026-12-31T00:00:00Z", // optional
  "env": "prod" }                       // free-form label
```
Returns `ApiKeyCreatedResponse` (`id, name, prefix, scopes, env,
expires_at, last_used_at, created_at, token`). The plaintext `token`
starts with `nxs_` and is shown **only in this response**.

### `GET /` — list caller's keys
**Auth required.** Returns `list[ApiKeySummary]` (same as above
without `token`).

### `DELETE /{key_id}` — revoke
**Auth required.** Returns `204 No Content`. The hashed row stays in
the table for audit; queries treat `revoked_at IS NOT NULL` as
inactive.

---

## Portfolios

Prefix `/api/v1/portfolio`. Source:
[`engine/api/routes/portfolio.py`](../../engine/api/routes/portfolio.py).
Wired with `require_legal_acceptance` (currently no-op).

### `POST /` — create portfolio
**Auth required.** Body:
```json
{ "name": "Long-term", "description": "...",
  "initial_capital": 100000 }
```
Returns `PortfolioResponse`.

### `GET /` — list user's portfolios
**Auth required.** Returns `list[PortfolioResponse]`.

### `GET /{portfolio_id}` — fetch one
**Auth required.** Ownership check: returns `403` if the portfolio
belongs to a different user.

### `DELETE /{portfolio_id}` — delete
**Auth required.** Ownership check. Returns `{"status": "deleted"}`.

---

## Backtest

Prefix `/api/v1/backtest`. Source:
[`engine/api/routes/backtest.py`](../../engine/api/routes/backtest.py).
Wired with `require_legal_acceptance`.

### `POST /run` — submit a backtest
**Auth required.** Body `BacktestRequest`:
```json
{ "strategy_name": "mean_reversion",
  "symbol": "AAPL",
  "start_date": "2023-01-01",
  "end_date": "2024-01-01",
  "initial_capital": 100000,
  "config": {} }
```
Returns `BacktestResponse(status="submitted", backtest_id="...")`.
The actual computation runs in the TaskIQ worker. **Results are stored
in-process only** (`_backtest_results` dict, 3600 s TTL) — restarts
lose them. See [`operations/known-issues.md`](../operations/known-issues.md).

### `GET /results/{backtest_id}` — fetch result
**Auth required.** Returns:
- `200 BacktestResultResponse` when ready
- `202 {"status": "running", "backtest_id": "..."}` if still queued
- `404` if unknown or expired from the in-memory cache

---

## Strategies

Prefix `/api/v1/strategies`. Source:
[`engine/api/routes/strategies.py`](../../engine/api/routes/strategies.py).
Wired with `require_legal_acceptance`.

### `GET /` — list installed strategies
**Auth required.** Reads from `request.app.state.plugin_registry`.
Returns `list[StrategySummary]`.

### `GET /{strategy_id}` — strategy detail
**Auth required.** Includes manifest, params, last-evaluated health.

### `POST /{strategy_id}/activate` — activate
**Auth required.** Body: `{ "params": { ... } }`.

### `POST /{strategy_id}/deactivate` — deactivate
**Auth required.**

### `POST /{strategy_id}/reload` — reload from disk
**Auth required.** Re-reads the manifest and strategy module.

### `GET /{strategy_id}/health` — last evaluation health
**Auth required.** Errors-per-bar, timeouts, exceptions.

---

## Scoring

Prefix `/api/v1/scoring`. Source:
[`engine/api/routes/scoring.py`](../../engine/api/routes/scoring.py).
Wired with `require_legal_acceptance`.

### `POST /{strategy_name}/run` — score a universe
**Auth required.** Body `ScoringRunRequest`:
```json
{ "universe": ["AAPL", "MSFT", "GOOGL"],
  "raw_data": { /* optional strategy-specific context */ } }
```
Strategy must implement `IScoringStrategy`. Persists a
`ScoringSnapshot` row. Returns `ScoringRunResponse(strategy_id,
scores, excluded_factors, universe_size)`.

### `GET /{strategy_name}/results` — paginated history
**Auth required.** Query params: `limit=20, offset=0,
sort_by=created_at, sort_order=desc`.

---

## Market data

Prefix `/api/v1/market-data`. Source:
[`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py).
Wired with `require_legal_acceptance`.

### `GET /{symbol}/bars` — OHLCV bars
**Auth required.** Query params: `interval=1d, period=1y, provider?,
asset_class?`. Provider selection falls through the
`DataProviderRegistry` priority list. Returns `BarsResponse`.

### `GET /{symbol}/quote` — latest price
**Auth required.** Query params: `provider?, asset_class?`. Returns
`QuoteResponse`.

---

## Webhooks

Prefix `/api/v1/webhooks`. Source:
[`engine/api/routes/webhooks.py`](../../engine/api/routes/webhooks.py).
**Note:** `POST /` requires `trade` scope (or higher role via JWT);
other endpoints are `get_current_user`.

### `POST /` — create webhook
**Auth required (scope `trade`).** Body `WebhookCreateRequest`:
```json
{ "url": "https://example.com/hook",
  "event_types": ["order.filled", "backtest.completed"],
  "custom_headers": { "X-Tenant": "acme" },
  "template": "generic",            // generic|discord|slack|telegram
  "max_retries": 3,
  "portfolio_id": null }
```
Returns `WebhookResponse` with the `signing_secret` echoed **once**.

### `GET /` — list caller's webhooks
**Auth required.** Returns `list[WebhookResponse]` (without
`signing_secret`).

### `PUT /{webhook_id}` — update
**Auth required.** Body `WebhookUpdateRequest` (any subset of the
create fields). Returns `WebhookResponse`.

### `DELETE /{webhook_id}` — delete
**Auth required.** Returns `204`.

### `POST /{webhook_id}/test` — send a test event
**Auth required.** Dispatches one synthetic event; returns
`DeliveryResponse`. Useful for verifying signature verification.

### `GET /{webhook_id}/deliveries` — delivery audit trail
**Auth required.** Query param: `limit=50`. Returns
`list[DeliveryResponse]`.

---

## Privacy & DSR

Prefix `/api/v1/privacy`. Source:
[`engine/api/routes/privacy.py`](../../engine/api/routes/privacy.py).
No `require_legal_acceptance` — by design, a user must be able to
exercise GDPR rights regardless of consent state.

### `POST /export` — GDPR data export
**Auth required.** Returns `ExportResponse(request, data)` with
the user's PII export. Schema version pinned in
[`engine/privacy/export.py`](../../engine/privacy/export.py).
Deny-list: `password_hash, mfa_secret_encrypted, mfa_backup_codes`.

### `POST /delete` — request account deletion
**Auth required.** Body: `{ "note"? }`. Returns
`DeletionStatusResponse`. Marks the user for deletion in 30 days
(GDPR Art. 12 SLA). Creates a `dsr_requests` row with `sla_due_at`.

### `POST /delete/cancel` — cancel pending deletion
**Auth required.** Only valid before the grace period expires.

### `GET /delete/status` — check deletion state
**Auth required.** Returns `DeletionStatusResponse`.

### `GET /requests` — list user's DSR history
**Auth required.** Returns `DSRListResponse`.

### `GET /kinds` — list supported DSR kinds
**Public.** Returns `{"kinds": ["delete","export","object",
"rectify","restrict"]}`.

---

## Tax

Prefix `/api/v1/tax`. Source: [`engine/api/routes/tax.py`](../../engine/api/routes/tax.py).

### `POST /report/{code}` — generate report
**Auth required.** `code` ∈ `{US, GB, DE, FR}` (case-insensitive).
Body `TaxReportRequest`:
```json
{ "disposals": [ { "symbol": "AAPL", "quantity": 10,
                   "sale_price": 195.50, "sale_date": "2024-06-01",
                   "purchase_price": 150.00,
                   "purchase_date": "2023-03-15" } ] }
```
Returns `{"jurisdiction": "US", "summary": { ... }}`. The summary
shape varies by jurisdiction (US Form 8949 / Schedule D, UK HMRC
CGT, Germany Abgeltungsteuer, France PFU).

### `POST /report/{code}/csv` — CSV export
**Auth required.** Same body as above. Returns a `text/csv`
attachment. Implementation in
[`engine/core/tax/reports/dispatcher.py`](../../engine/core/tax/reports/dispatcher.py).

---

## Legal

Prefix `/api/v1/legal`. Source: [`engine/api/routes/legal.py`](../../engine/api/routes/legal.py).

### `GET /documents` — list documents
**Public** (auth optional). Query param: `category?`. Authenticated
calls return per-document `accepted` / `needs_re_acceptance` flags.

### `GET /documents/{slug}` — document content
**Public.** Query param: `version?`. Markdown body with front-matter
stripped, templated with operator settings (`{{OPERATOR_NAME}}`,
`{{OPERATOR_EMAIL}}`, `{{OPERATOR_URL}}`, `{{JURISDICTION}}`,
`{{PLATFORM_FEE_PERCENT}}`, `{{EFFECTIVE_DATE}}`).

### `POST /accept` — record acceptances
**Auth required.** Body `AcceptRequest`:
```json
{ "acceptances": [
    { "document_slug": "terms-of-service", "document_version": "1.2" },
    { "document_slug": "privacy-policy", "document_version": "2.0" }
] }
```
Returns `AcceptResponse` with the persisted rows including IP and
user-agent.

### `GET /acceptances/me` — user's acceptance history
**Auth required.** Query param: `document_slug?`. Returns
`AcceptanceListResponse`.

### `GET /attributions` — data-provider attributions
**Public.** Query param: `context?` (e.g. `dashboard`, `api`).
Returns the active `DataProviderAttribution` rows.

---

## Marketplace

Prefix `/api/v1/marketplace`. Source:
[`engine/api/routes/marketplace.py`](../../engine/api/routes/marketplace.py).
Wired with `require_legal_acceptance`. **Note:** install/uninstall/rate
return stub responses today — see known-issues.

### `GET /browse` — browse strategies
**Auth required.** Query params: `category?, search?, sort_by=downloads,
page=1, per_page=20`. Currently returns `{"strategies": [], "total": 0}`.

### `GET /categories` — list categories
**Auth required.** Hard-coded: `algorithmic, ml, llm, hybrid, income,
macro`.

### `POST /install` — install a strategy
**Auth required, role `developer`.** Body: `{ "strategy_id",
"version": "latest" }`. Stub.

### `DELETE /uninstall/{strategy_id}`
**Auth required, role `developer`.** Stub.

### `POST /{strategy_id}/rate`
**Auth required.** Query params: `rating, review?`. Stub.

---

## Reference data

Prefix `/api/v1/reference`. Source:
[`engine/api/routes/reference.py`](../../engine/api/routes/reference.py).
**Public.**

### `GET /suggest` — instrument search
**Public.** Query params: `q, limit=10, asset_class?`. Returns
`{"suggestions": [...]}`. Falls back from in-memory `SearchIndex`
(~340 curated instruments; see [`engine/reference/seed.py`](../../engine/reference/seed.py))
to the Yahoo Finance search API when no local match.

---

## System

Prefix `/api/v1/system`. Source:
[`engine/api/routes/system.py`](../../engine/api/routes/system.py).

### `GET /status` — system status
**Auth required.** Returns `SystemStatusResponse`:
```json
{ "engine_version": "0.1.0",
  "uptime_seconds": 12345,
  "server_time": "2026-06-06T12:00:00Z",
  "components": [
    { "name": "database", "status": "ok" },
    { "name": "valkey", "status": "ok" }
  ],
  "counts": { "users": 42, "portfolios": 100, "backtests": 1234,
              "webhooks_active": 7, "api_keys_active": 12 } }
```

---

## Client errors

Prefix `/api/v1/client`. Source:
[`engine/api/routes/client_errors.py`](../../engine/api/routes/client_errors.py).

### `POST /errors` — front-end error report
**Public.** Body:
```json
{ "message": "TypeError: Cannot read properties of undefined",
  "stack": "...",
  "component_stack": "...",
  "url": "https://app.example.com/dashboard",
  "user_agent": "...",
  "error_id": "550e8400-..." }
```
Sanitises ANSI / control / C1 characters; strips URL query strings
before logging. Returns `ClientErrorAck(error_id)`. Rate-limited to
30 / min / IP at the route level (see
[`engine/app.py:185-190`](../../engine/app.py)).

---

## WebSocket

`WS /api/v1/ws`. Source:
[`engine/api/routes/websocket.py`](../../engine/api/routes/websocket.py).

### Auth-then-subscribe protocol
**No header auth** — auth is in-band. Client must send within
`AUTH_TIMEOUT_SECONDS = 10.0`:

```json
{ "type": "auth", "token": "<JWT or nxs_...>" }
```

Server responds with `{"type": "auth_ok"}` or closes with code `4401`
(auth failed) / `4400` (malformed message).

### After auth
- `{"type": "subscribe", "topic": "portfolio"}` — start receiving
  events for that topic
- `{"type": "unsubscribe", "topic": "..."}`
- `{"type": "ping"}` — server replies `{"type": "pong"}`

Topics restricted to `{portfolio, backtest, order, alert}` (see
[`engine/api/websocket/manager.py:32-41`](../../engine/api/websocket/manager.py)).

**Single-process only.** Multi-replica broadcasting needs a Redis
pubsub bridge — see [`operations/known-issues.md`](../operations/known-issues.md).

---

## Rate limiting

Global defaults (`engine/config.py:41-43`):
- `NEXUS_RATE_LIMIT_PER_MINUTE = 600`
- `NEXUS_RATE_LIMIT_BURST = 60`
- Exempt paths: `/health, /metrics`

Per-route overrides live in [`engine/app.py:172-191`](../../engine/app.py).
The notable one: `POST /api/v1/client/errors` is capped at 30 / min /
IP so a buggy render loop cannot DoS the log pipeline.

Body size is hard-capped at 1 MiB via `BodySizeLimitMiddleware`
([`engine/app.py:195`](../../engine/app.py)).

## Error responses

All non-2xx responses are JSON with this shape:

```json
{ "detail": "human-readable message",
  "code": "machine_code_if_applicable" }
```

Common machine codes:
- `legal_re_acceptance_required` (HTTP 451) — new legal doc version
  requires explicit acceptance
- `mfa_required` (HTTP 200 with `mfa_required: true`) — login
  requires a TOTP challenge
- `unsupported_jurisdiction` (HTTP 400) — tax report requested for
  an unknown jurisdiction

Validation errors (HTTP 422) follow the FastAPI default `detail:
list[{loc, msg, type}]` shape.
