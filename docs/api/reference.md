# API reference

The full HTTP surface of Nexus Trade Engine. Routes are grouped by
their FastAPI tag, which is also the OpenAPI tag in `/docs`. Every
route listed here is mounted in
[`engine/api/router.py`](../../engine/api/router.py) at the prefix
shown.

## Conventions

- **Base path.** All API routes are prefixed `/api/v1/`. The only
  un-prefixed routes are `/health`, `/ready`, `/metrics`.
- **Auth.** Most routes require a bearer token (`Authorization: Bearer
  <jwt>`) or an engine API key (`X-API-Key: nxs_*`). The auth model
  is described in [`../architecture/decisions.md#adr-0003`](../architecture/decisions.md).
- **Legal gate.** Routes marked **legal-gated** additionally require
  the user to have accepted the current Terms / EULA. Mounted at the
  router level via `Depends(require_legal_acceptance)`.
- **Rate limit.** Every route passes through `RateLimitMiddleware`
  (`engine/app.py:175`). Default is 600 req/min/IP with a burst of 60.
  Per-route overrides exist where noted.
- **Body cap.** All request bodies are capped at 1 MiB
  (`BodySizeLimitMiddleware`). Endpoints that accept larger payloads
  would need to raise this.
- **Errors.** Standard HTTP status codes. Error bodies are JSON
  `{"detail": "..."}`. Structured logs include the correlation id from
  `X-Request-Id`.

## Interactive docs

FastAPI's auto-generated docs are available at:

- `/docs` — Swagger UI
- `/redoc` — ReDoc
- `/openapi.json` — raw OpenAPI spec

Use those for the exact request/response schema of any endpoint. The
tables below are the engineer's index, not a replacement.

---

## Health and observability

Mounted at root (no `/api/v1/` prefix). No auth.

| Method | Path                  | Source                                    | Description |
|--------|-----------------------|-------------------------------------------|-------------|
| GET    | `/health`             | `engine/api/routes/health.py:19`          | Liveness probe. Returns `{"status":"ok"}`. |
| GET    | `/health/providers`   | `engine/api/routes/health.py:24`          | Health of every registered data provider. Returns per-provider status + latency. |
| GET    | `/ready`              | `engine/api/routes/health.py:42`          | Readiness probe. Checks DB + Valkey reachability. Returns `degraded` if any check fails. |
| GET    | `/metrics`            | `engine/api/routes/metrics.py`            | Prometheus scrape endpoint. Exposed when `PrometheusBackend` is wired. |

---

## Auth (`/api/v1/auth`)

Source: `engine/api/routes/auth.py`

| Method | Path                          | Auth         | Description |
|--------|-------------------------------|--------------|-------------|
| POST   | `/api/v1/auth/register`       | public       | Email + password registration. Returns access + refresh tokens. Disabled when `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=false`. |
| POST   | `/api/v1/auth/login`          | public       | Email + password login. If MFA is enabled, returns `{"mfa_required":true,"challenge_token":"..."}` instead of tokens. |
| POST   | `/api/v1/auth/refresh`        | public       | Rotate refresh token. Detects replay: any reuse revokes every active session for the user. |
| GET    | `/api/v1/auth/me`             | bearer       | Current user profile. |
| POST   | `/api/v1/auth/logout`         | bearer       | Revoke the supplied refresh token (or all of the user's refresh tokens if no body is sent). |
| GET    | `/api/v1/auth/{provider}/authorize`  | public | Get the OAuth2/OIDC authorize URL + state cookie for the named provider. |
| GET    | `/api/v1/auth/{provider}/callback`   | public | OAuth2/OIDC callback. Validates state cookie, exchanges code for tokens, returns access + refresh. |

### Schemas

```json
// POST /api/v1/auth/register
{
  "email": "user@example.com",
  "password": "...",         // ≥ 8 chars
  "display_name": "..."      // optional
}

// POST /api/v1/auth/login
{
  "email": "user@example.com",
  "password": "..."
}

// Response 200 (or 201 from register)
{
  "access_token": "eyJ...",
  "refresh_token": "<opaque>",
  "token_type": "bearer",
  "expires_in": 3600
}

// Response 200 from /login when MFA is enabled
{
  "mfa_required": true,
  "challenge_token": "<opaque>"
}
```

---

## MFA (`/api/v1/auth/mfa`)

Source: `engine/api/routes/mfa.py`

| Method | Path                                       | Auth   | Description |
|--------|--------------------------------------------|--------|-------------|
| POST   | `/api/v1/auth/mfa/enroll`                  | bearer | Begin enrollment. Returns the shared secret + `otpauth://` URI. |
| POST   | `/api/v1/auth/mfa/enroll/confirm`          | bearer | Confirm enrollment with a 6-digit code. Stores the encrypted secret and returns 10 backup codes (shown once). |
| POST   | `/api/v1/auth/mfa/verify`                  | public*| Verify a challenge token from `/login` with a TOTP or backup code. Returns real access + refresh tokens on success. |
| POST   | `/api/v1/auth/mfa/disable`                 | bearer | Disable MFA. Requires password + current TOTP code. |
| POST   | `/api/v1/auth/mfa/backup-codes/regen`      | bearer | Regenerate backup codes. Requires current TOTP code. |

\* `verify` accepts the opaque challenge token issued by `/login`,
not a bearer token.

---

## API keys (`/api/v1/auth/api-keys`)

Source: `engine/api/routes/api_keys.py:30`

Long-lived tokens for headless / automation use. Format:
`nxs_<prefix>_<secret>`. The plaintext is shown **exactly once** on
creation.

| Method | Path                                    | Auth         | Scope required | Description |
|--------|-----------------------------------------|--------------|----------------|-------------|
| POST   | `/api/v1/auth/api-keys`                 | bearer       | —              | Issue a new API key. Body: `{name, scopes[], expires_at?, env?}`. |
| GET    | `/api/v1/auth/api-keys`                 | bearer/api-key | `read`       | List the caller's keys. Plaintext secret is never returned. |
| DELETE | `/api/v1/auth/api-keys/{key_id}`        | bearer       | —              | Revoke a key. Idempotent. |

Valid scopes: `read`, `trade`, `admin` (hierarchy; higher satisfies lower).

---

## System (`/api/v1/system`)

Source: `engine/api/routes/system.py:27`

| Method | Path                          | Auth         | Description |
|--------|-------------------------------|--------------|-------------|
| GET    | `/api/v1/system/status`       | bearer/api-key | Engine version, uptime, component health, counts of major entities. Intended for CI/CD probes. |

---

## Legal (`/api/v1/legal`)

Source: `engine/api/routes/legal.py`. Note: these routes are mounted
without the `/api/v1/` prefix on the path of the list / detail / accept
endpoints for historical reasons; the path in the table below is the
full mounted path.

| Method | Path                                       | Auth         | Description |
|--------|--------------------------------------------|--------------|-------------|
| GET    | `/api/v1/legal/documents`                  | optional     | List documents. Optional `category` filter. Includes per-user acceptance status if authenticated. |
| GET    | `/api/v1/legal/documents/{slug}`           | optional     | Document body in Markdown. Templates (`{{OPERATOR_NAME}}`, etc.) are substituted at read time. |
| POST   | `/api/v1/legal/accept`                     | bearer       | Record acceptance of one or more documents. Body: `{acceptances:[{slug, version}, ...]}`. |
| GET    | `/api/v1/legal/acceptances/me`             | bearer       | The caller's acceptance history. Optional `document_slug` filter. |
| GET    | `/api/v1/legal/attributions`               | public       | Data-provider attribution list (for legal display). Optional `context` filter. |

---

## Portfolio (`/api/v1/portfolio`)

Source: `engine/api/routes/portfolio.py`. Legal-gated.

| Method | Path                              | Auth         | Scope | Description |
|--------|-----------------------------------|--------------|-------|-------------|
| POST   | `/api/v1/portfolio/`              | bearer/api-key | `trade` | Create a portfolio. Body: `{name, description?, initial_capital?}`. |
| GET    | `/api/v1/portfolio/`              | bearer/api-key | `read`  | List the caller's portfolios. |
| GET    | `/api/v1/portfolio/{portfolio_id}`| bearer/api-key | `read`  | Get one portfolio. 403 if not owner. |
| DELETE | `/api/v1/portfolio/{portfolio_id}`| bearer/api-key | `trade` | Archive (cascade-delete) a portfolio. |

---

## Strategies (`/api/v1/strategies`)

Source: `engine/api/routes/strategies.py`. Legal-gated.

| Method | Path                                       | Auth         | Description |
|--------|--------------------------------------------|--------------|-------------|
| GET    | `/api/v1/strategies/`                      | bearer/api-key | List installed strategies and load state. |
| GET    | `/api/v1/strategies/{strategy_id}`         | bearer/api-key | Strategy details — manifest, config schema, watchlist, capabilities. |
| POST   | `/api/v1/strategies/{strategy_id}/activate`| bearer/api-key | Initialise and activate with the given config. |
| POST   | `/api/v1/strategies/{strategy_id}/deactivate` | bearer/api-key | Deactivate and unload. |
| POST   | `/api/v1/strategies/{strategy_id}/reload`  | bearer/api-key | Hot-reload from disk. |
| GET    | `/api/v1/strategies/{strategy_id}/health`  | bearer/api-key | Runtime health metrics. |

---

## Backtest (`/api/v1/backtest`)

Source: `engine/api/routes/backtest.py`. Legal-gated.

| Method | Path                              | Auth         | Description |
|--------|-----------------------------------|--------------|-------------|
| POST   | `/api/v1/backtest/run`            | bearer/api-key | Submit a backtest. Returns `202 Accepted` with `backtest_id`. |
| GET    | `/api/v1/backtest/results/{backtest_id}` | bearer/api-key | Poll for results. Status: `running`, `completed`, `failed`, `not_found`. |

### Request

```json
POST /api/v1/backtest/run
{
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "start_date": "2020-01-01",
  "end_date": "2024-12-31",
  "initial_capital": 100000.0,
  "config": { /* strategy-specific, optional */ }
}
```

### Response (run)

```json
{ "status": "accepted", "backtest_id": "<uuid>" }
```

### Response (results — completed)

```json
{
  "status": "completed",
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "initial_capital": 100000.0,
  "final_value": 142356.78,
  "metrics": {
    "total_return_pct": 42.36,
    "annualized_return_pct": 9.21,
    "sharpe_ratio": 1.34,
    "sortino_ratio": 1.78,
    "max_drawdown_pct": 12.4,
    "max_drawdown_duration_days": 87,
    "max_drawdown_recovery_days": 42,
    "calmar_ratio": 0.74,
    "volatility_annual_pct": 15.3,
    "total_trades": 47,
    "win_rate": 0.57,
    "profit_factor": 1.62,
    "avg_trade_pnl": 901.23,
    "avg_winner": 2104.50,
    "avg_loser": -1180.40,
    "best_trade": 5230.10,
    "worst_trade": -3410.20,
    "max_consecutive_wins": 6,
    "max_consecutive_losses": 4,
    "total_costs": 842.10,
    "total_taxes": 3120.45,
    "cost_drag_pct": 0.84,
    "turnover_ratio": 2.4,
    "exposure_pct": 71.2,
    "rolling_metrics": [
      { "window_days": 30, "sharpe_ratio": 1.2, "volatility_annual_pct": 14.0, "max_drawdown_pct": 3.1 }
    ]
  },
  "equity_curve": [{"t":"2020-01-02","v":100100.5}, ...],
  "drawdown_curve": [0.0, -0.001, -0.0023, ...]
}
```

---

## Scoring (`/api/v1/scoring`)

Source: `engine/api/routes/scoring.py`. Legal-gated.

| Method | Path                                       | Auth         | Description |
|--------|--------------------------------------------|--------------|-------------|
| POST   | `/api/v1/scoring/{strategy_name}/run`      | bearer/api-key | Run a scoring strategy over a universe. Body: `{universe:string[], raw_data?:{symbol:{factor:value}}}`. Persists a `ScoringSnapshot`. |
| GET    | `/api/v1/scoring/{strategy_name}/results`  | bearer/api-key | Paginated scoring history. Query: `limit`, `offset`, `sort_by`, `sort_order`. |

---

## Marketplace (`/api/v1/marketplace`)

Source: `engine/api/routes/marketplace.py`. Legal-gated.

| Method | Path                                       | Auth          | Description |
|--------|--------------------------------------------|---------------|-------------|
| GET    | `/api/v1/marketplace/browse`               | bearer/api-key | Browse strategies. Filters: `category`, `search`, `sort_by`, `page`, `per_page`. |
| GET    | `/api/v1/marketplace/categories`           | bearer/api-key | List strategy categories. |
| POST   | `/api/v1/marketplace/install`              | `developer` role | Install a strategy. **Stub — returns `not_implemented`.** |
| DELETE | `/api/v1/marketplace/uninstall/{strategy_id}` | `developer` role | Uninstall a strategy. **Stub.** |
| POST   | `/api/v1/marketplace/{strategy_id}/rate`   | bearer/api-key | Rate a strategy (1–5). **Stub.** |

---

## Market data (`/api/v1/market-data`)

Source: `engine/api/routes/market_data.py`. Legal-gated.

| Method | Path                                       | Auth         | Description |
|--------|--------------------------------------------|--------------|-------------|
| GET    | `/api/v1/market-data/{symbol}/bars`        | bearer/api-key | OHLCV bars. Query: `period`, `interval`, `provider?`, `asset_class?`. |
| GET    | `/api/v1/market-data/{symbol}/quote`       | bearer/api-key | Latest price. Query: `provider?`, `asset_class?`. |

The `provider` query param pins a specific adapter (useful for
parity testing). Without it, the registry routes by asset class and
priority.

Errors:

- `400` — invalid symbol (regex mismatch) or fatal provider error.
- `502` — provider returned an error.
- `503` — every candidate provider is down or timed out.
- `501` — capability not supported by any provider.

---

## Reference (`/api/v1/reference`)

Source: `engine/api/routes/reference.py`. No legal gate (so typeahead
works during onboarding).

| Method | Path                              | Auth | Description |
|--------|-----------------------------------|------|-------------|
| GET    | `/api/v1/reference/suggest`       | none | Typeahead. Query: `q`, `limit`, `asset_class?`. Falls back from local index → Yahoo search API. |

---

## Webhooks (`/api/v1/webhooks`)

Source: `engine/api/routes/webhooks.py`.

| Method | Path                                       | Auth         | Scope | Description |
|--------|--------------------------------------------|--------------|-------|-------------|
| POST   | `/api/v1/webhooks`                         | bearer/api-key | `trade` | Create a webhook. Signing secret is generated server-side and returned once. |
| GET    | `/api/v1/webhooks`                         | bearer/api-key | `read`  | List the caller's webhooks. |
| PUT    | `/api/v1/webhooks/{webhook_id}`            | bearer/api-key | `trade` | Update URL, event_types, headers, template, retries, active flag. |
| DELETE | `/api/v1/webhooks/{webhook_id}`            | bearer/api-key | `trade` | Delete a webhook. |
| POST   | `/api/v1/webhooks/{webhook_id}/test`       | bearer/api-key | `trade` | Send a test event. Returns the resulting `WebhookDelivery`. |
| GET    | `/api/v1/webhooks/{webhook_id}/deliveries` | bearer/api-key | `read`  | Delivery history. Query: `limit`. |

Valid templates: `generic`, `discord`, `slack`, `telegram`.

Webhook payloads are signed with HMAC-SHA256 using the signing
secret. Receivers should verify the `X-Nexus-Signature` header.

---

## Tax (`/api/v1/tax`)

Source: `engine/api/routes/tax.py`.

| Method | Path                                | Auth         | Description |
|--------|-------------------------------------|--------------|-------------|
| POST   | `/api/v1/tax/report/{code}`         | bearer/api-key | Per-jurisdiction tax summary. `code` ∈ `us`, `gb`, `de`, `fr` (case-insensitive). |
| POST   | `/api/v1/tax/report/{code}/csv`     | bearer/api-key | Same dispatch as above, returns CSV (header + values) for spreadsheet round-trips. |

### Request

```json
POST /api/v1/tax/report/us
{
  "disposals": [
    {
      "description": "AAPL 100 sh",
      "acquired": "2023-01-15",
      "disposed": "2024-02-20",
      "proceeds": "18250.00",   // string preserves Decimal precision
      "cost":    "15100.00"
    }
  ]
}
```

The route is jurisdiction-neutral: the same payload is valid for
every supported `code`; the response shape varies by jurisdiction.

---

## Privacy (`/api/v1/privacy`)

Source: `engine/api/routes/privacy.py`.

GDPR / CCPA data-subject request endpoints. All require bearer auth.

| Method | Path                                  | Description |
|--------|---------------------------------------|-------------|
| POST   | `/api/v1/privacy/export`              | Synchronous export of the caller's data (JSON). Creates a `DSRequest` of kind `export`. |
| POST   | `/api/v1/privacy/delete`              | Initiate account deletion. Returns `202`. 30-day grace window. |
| POST   | `/api/v1/privacy/delete/cancel`       | Cancel a pending deletion. 404 if not pending. |
| GET    | `/api/v1/privacy/delete/status`       | Pending flag + SLA due-at. |
| GET    | `/api/v1/privacy/requests`            | The caller's DSR history. |
| GET    | `/api/v1/privacy/kinds`               | Allow-list of valid DSR kinds (for OpenAPI clients). |

---

## Client errors (`/api/v1/client`)

Source: `engine/api/routes/client_errors.py`.

| Method | Path                          | Auth | Rate limit | Description |
|--------|-------------------------------|------|------------|-------------|
| POST   | `/api/v1/client/errors`       | none | 30 req/min | Frontend ErrorBoundary sink. CRLF/ANSI sanitised, URL stripped of query string. Returns `error_id`. |

This is the one route that is intentionally unauthenticated —
authenticated sessions are when error reporting is most likely to
fail. Abuse is bounded by the per-route rate cap.

---

## WebSocket (`/api/v1/ws`)

Source: `engine/api/routes/websocket.py:46`.

Protocol: auth-then-subscribe.

1. Client opens `WS /api/v1/ws`. Server accepts.
2. Client must send `{"type": "auth", "token": "<JWT or nxs_*>"}` within
   10 seconds. Server validates.
3. On success: `{"type": "auth.ok", "user_id": "..."}`.
4. Client subscribes with `{"type": "subscribe", "topics": ["portfolio", "order"]}`.
5. Server replies `{"type": "subscribed", "topics": [...]}`.
6. Server pushes events as `{"type": "event", "topic": "...", "payload": {...}}`.
7. Either side may send `{"type": "ping"}`; recipient replies `{"type": "pong"}`.

Valid topics: `portfolio`, `backtest`, `order`, `alert`. Unknown
topics are silently dropped from the subscribe list.

Custom close codes:

- `4400` — bad request (missing auth message, missing token).
- `4401` — auth timeout, invalid token, or unknown user.

JWT in the URL is **not** supported (query strings end up in proxy logs).

---

## Pagination conventions

- `limit` (default 20, max 100 unless noted).
- `offset` (default 0).
- `sort_by` / `sort_order` where supported.
- Total counts are returned in the response body where the cost is
  acceptable; for high-cardinality lists (deliveries, scoring
  results), only the page is returned.

## Idempotency

POST routes that create resources are **not** idempotent. Re-submitting
the same body creates a second resource. Clients should generate a
client-side id and use it to dedupe at the application layer if
needed.

## Rate limit headers

The rate-limit middleware does not currently emit `X-RateLimit-*`
headers; the rejection body is `{"detail":"Rate limit exceeded"}`. If
you need headed responses for SDK consumers, that is a tracked
follow-up.

## Cross-cutting behaviors

- **CORS** — `NEXUS_CORS_ORIGINS` (default `["http://localhost:3000"]`).
- **Security headers** — HSTS in production, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `Permissions-Policy: geolocation=(), microphone=(), camera=()`.
- **Correlation id** — Echoed as `X-Request-Id` on every response.
  Forward inbound `X-Request-Id` to extend a trace.
