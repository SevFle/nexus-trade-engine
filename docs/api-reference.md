# API Reference

Base URL: `http://localhost:8000` (configurable via `NEXUS_APP_HOST` / `NEXUS_APP_PORT`)

Authentication: Bearer token in `Authorization` header for all `/api/v1/*` routes
unless noted otherwise. Public routes: `/health`, `/ready`, `/metrics`.

---

## Health & Readiness

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Liveness probe — returns `{status: "ok"}` |
| GET | `/health/providers` | No | Data provider health — checks each registered provider |
| GET | `/ready` | No | Readiness probe — checks DB + Valkey connectivity |

### GET /health/providers

```json
{
  "status": "ok|degraded|down",
  "providers": {
    "yahoo": { "status": "up", "latency_ms": 142, "detail": null }
  }
}
```

### GET /ready

```json
{ "status": "ok|degraded", "db": "ok|error", "valkey": "ok|error" }
```

---

## Auth (`/api/v1/auth`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/auth/register` | No | Register with email/password |
| POST | `/api/v1/auth/login` | No | Login with email/password |
| POST | `/api/v1/auth/refresh` | No | Rotate refresh token |
| GET | `/api/v1/auth/me` | Bearer | Get current user profile |
| POST | `/api/v1/auth/logout` | Bearer | Revoke refresh token(s) |
| GET | `/api/v1/auth/{provider}/authorize` | No | Get OAuth authorize URL |
| GET | `/api/v1/auth/{provider}/callback` | No | OAuth callback (code + state) |

### POST /api/v1/auth/register

```json
// Request
{ "email": "user@example.com", "password": "secret123", "display_name": "Alice" }

// Response 201
{ "access_token": "eyJ...", "refresh_token": "dG...", "token_type": "bearer", "expires_in": 3600 }
```

### POST /api/v1/auth/login

```json
// Request
{ "email": "user@example.com", "password": "secret123" }

// Response 200 (no MFA)
{ "access_token": "eyJ...", "refresh_token": "dG...", "token_type": "bearer", "expires_in": 3600 }

// Response 200 (MFA enabled)
{ "mfa_required": true, "challenge_token": "..." }
```

### GET /api/v1/auth/me

```json
{
  "id": "uuid",
  "email": "user@example.com",
  "display_name": "Alice",
  "role": "user",
  "auth_provider": "local",
  "is_active": true
}
```

---

## MFA (`/api/v1/auth/mfa`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/auth/mfa/enroll` | Bearer | Start TOTP enrollment |
| POST | `/api/v1/auth/mfa/enroll/confirm` | Bearer | Confirm enrollment with TOTP code |
| POST | `/api/v1/auth/mfa/verify` | No | Verify TOTP code after login challenge |
| POST | `/api/v1/auth/mfa/disable` | Bearer | Disable MFA (requires password + code) |
| POST | `/api/v1/auth/mfa/backup-codes/regen` | Bearer | Regenerate backup codes |

### POST /api/v1/auth/mfa/enroll

```json
// Response 200
{ "secret": "JBSWY3DPEHPK3PXP", "otpauth_uri": "otpauth://totp/..." }
```

### POST /api/v1/auth/mfa/enroll/confirm

```json
// Request
{ "secret": "JBSWY3DPEHPK3PXP", "code": "123456" }

// Response 200
{ "backup_codes": ["abc123", "def456", ...] }
```

### POST /api/v1/auth/mfa/verify

```json
// Request
{ "challenge_token": "...", "code": "123456" }

// Response 200
{ "access_token": "eyJ...", "refresh_token": "dG...", "token_type": "bearer", "expires_in": 3600 }
```

---

## API Keys (`/api/v1/auth/api-keys`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/auth/api-keys` | Bearer | Create API key (token shown once) |
| GET | `/api/v1/auth/api-keys` | Bearer | List API keys (prefix + metadata only) |
| DELETE | `/api/v1/auth/api-keys/{key_id}` | Bearer | Revoke API key |

### POST /api/v1/auth/api-keys

```json
// Request
{ "name": "CI bot", "scopes": ["read", "trade"], "expires_at": "2026-12-31T00:00:00Z", "env": "live" }

// Response 201
{
  "id": "uuid", "name": "CI bot", "prefix": "nxs_abc",
  "scopes": ["read", "trade"], "token": "nxs_abc123...full_token",
  "expires_at": "2026-12-31T00:00:00Z", "revoked_at": null, "created_at": "..."
}
```

Valid scopes: `read`, `trade`, `strategy`, `admin`.

---

## Portfolio (`/api/v1/portfolio`)

*Requires legal document acceptance.*

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/portfolio/` | Bearer + Legal | Create portfolio |
| GET | `/api/v1/portfolio/` | Bearer + Legal | List user's portfolios |
| GET | `/api/v1/portfolio/{portfolio_id}` | Bearer + Legal | Get portfolio details |
| DELETE | `/api/v1/portfolio/{portfolio_id}` | Bearer + Legal | Delete portfolio |

### POST /api/v1/portfolio/

```json
// Request
{ "name": "My Portfolio", "description": "Growth strategy", "initial_capital": 100000.0 }

// Response 200
{ "id": "uuid", "name": "My Portfolio", "description": "Growth strategy", "initial_capital": 100000.0, "created_at": "..." }
```

---

## Strategies (`/api/v1/strategies`)

*Requires legal document acceptance.*

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/strategies/` | Bearer + Legal | List installed strategies |
| GET | `/api/v1/strategies/{strategy_id}` | Bearer + Legal | Get strategy details |
| POST | `/api/v1/strategies/{strategy_id}/activate` | Bearer + Legal | Activate with config |
| POST | `/api/v1/strategies/{strategy_id}/deactivate` | Bearer + Legal | Deactivate strategy |
| POST | `/api/v1/strategies/{strategy_id}/reload` | Bearer + Legal | Hot-reload from disk |
| GET | `/api/v1/strategies/{strategy_id}/health` | Bearer + Legal | Runtime health metrics |

### POST /api/v1/strategies/{strategy_id}/activate

```json
// Request
{ "params": { "sma_period": 20, "z_threshold": 2.0 } }

// Response 200
{ "status": "activated", "strategy_id": "mean_reversion", "name": "Mean Reversion", "version": "1.0.0" }
```

---

## Backtest (`/api/v1/backtest`)

*Requires legal document acceptance.*

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/backtest/run` | Bearer + Legal | Submit backtest (async) |
| GET | `/api/v1/backtest/results/{backtest_id}` | Bearer + Legal | Poll for results |

### POST /api/v1/backtest/run

```json
// Request
{
  "strategy_name": "mean_reversion",
  "symbol": "AAPL",
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "initial_capital": 100000.0,
  "config": { "sma_period": 20 }
}

// Response 202
{ "status": "accepted", "backtest_id": "uuid" }
```

### GET /api/v1/backtest/results/{backtest_id}

Returns `status: "running"` (202), `"completed"` (200), `"failed"` (200), or
`"not_found"` (404). Completed results include:

```json
{
  "status": "completed",
  "strategy_name": "mean_reversion",
  "symbol": "AAPL",
  "initial_capital": 100000.0,
  "final_value": 112340.0,
  "metrics": {
    "total_return_pct": 12.34,
    "annualized_return_pct": 12.34,
    "sharpe_ratio": 1.85,
    "sortino_ratio": 2.41,
    "max_drawdown_pct": 8.2,
    "max_drawdown_duration_days": 23,
    "volatility_annual_pct": 15.6,
    "total_trades": 47,
    "win_rate": 0.62,
    "profit_factor": 2.1,
    "total_costs": 342.50,
    "total_taxes": 1280.0,
    "cost_drag_pct": 1.6,
    "rolling_metrics": [
      { "window_days": 63, "sharpe_ratio": 1.92, "volatility_annual_pct": 14.8, "max_drawdown_pct": 3.1 }
    ]
  },
  "equity_curve": [{"date": "2024-01-02", "value": 100120.0}, ...],
  "drawdown_curve": [0.0, -0.001, ...]
}
```

---

## Webhooks (`/api/v1/webhooks`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/webhooks` | Bearer + `trade` scope | Create webhook |
| GET | `/api/v1/webhooks` | Bearer | List webhooks |
| PUT | `/api/v1/webhooks/{webhook_id}` | Bearer | Update webhook |
| DELETE | `/api/v1/webhooks/{webhook_id}` | Bearer | Delete webhook |
| POST | `/api/v1/webhooks/{webhook_id}/test` | Bearer | Send test delivery |
| GET | `/api/v1/webhooks/{webhook_id}/deliveries` | Bearer | List delivery history |

Valid templates: `generic`, `discord`, `slack`, `telegram`.

### POST /api/v1/webhooks

```json
// Request
{
  "url": "https://example.com/hook",
  "event_types": ["order.filled", "backtest.completed"],
  "template": "discord",
  "max_retries": 3,
  "portfolio_id": "uuid"
}

// Response 201
{ "id": "uuid", "url": "https://example.com/hook", "signing_secret": "abc...", ... }
```

---

## Tax Reports (`/api/v1/tax`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/tax/report/{code}` | Bearer | JSON tax summary |
| POST | `/api/v1/tax/report/{code}/csv` | Bearer | CSV download |

`code` is a two-letter jurisdiction slug (case-insensitive): `US`, `GB`, `DE`, `FR`.

### POST /api/v1/tax/report/US

```json
// Request
{
  "disposals": [
    { "description": "AAPL", "acquired": "2024-01-15", "disposed": "2024-06-15", "proceeds": "15000.00", "cost": "12000.00" }
  ]
}

// Response 200
{ "jurisdiction": "US", "summary": { "short_term_gain": "3000.00", "long_term_gain": "0.00", ... } }
```

---

## Market Data (`/api/v1/market-data`)

*Requires legal document acceptance.*

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/market-data/{symbol}/bars` | Bearer + Legal | OHLCV bars |
| GET | `/api/v1/market-data/{symbol}/quote` | Bearer + Legal | Latest price |

### GET /api/v1/market-data/{symbol}/bars

Query params: `interval` (default `1d`), `period` (default `1y`), `provider`
(optional, pin to specific provider), `asset_class` (optional, override
auto-detection).

```json
{
  "symbol": "AAPL", "interval": "1d", "period": "1y",
  "asset_class": "equity", "provider": "yahoo",
  "bars": [
    { "timestamp": "2024-01-02T00:00:00+00:00", "open": 185.0, "high": 186.5, "low": 184.2, "close": 185.9, "volume": 48230000 }
  ]
}
```

---

## Scoring (`/api/v1/scoring`)

*Requires legal document acceptance.*

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/scoring/{strategy_name}/run` | Bearer + Legal | Run scoring strategy |
| GET | `/api/v1/scoring/{strategy_name}/results` | Bearer + Legal | List scoring results |

### POST /api/v1/scoring/{strategy_name}/run

```json
// Request
{ "universe": ["AAPL", "MSFT", "GOOGL"], "raw_data": { "AAPL": { "roe": 0.45, "pe": 28.5 } } }

// Response 200
{ "strategy_id": "quality_momentum", "scores": [...], "excluded_factors": [], "universe_size": 3 }
```

---

## Marketplace (`/api/v1/marketplace`)

*Requires legal document acceptance.*

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/marketplace/browse` | Bearer + Legal | Browse strategies |
| GET | `/api/v1/marketplace/categories` | Bearer + Legal | List categories |
| POST | `/api/v1/marketplace/install` | Bearer + `developer` role | Install strategy |
| DELETE | `/api/v1/marketplace/uninstall/{strategy_id}` | Bearer + `developer` role | Uninstall strategy |
| POST | `/api/v1/marketplace/{strategy_id}/rate` | Bearer + Legal | Rate strategy (1-5) |

---

## Privacy / DSR (`/api/v1/privacy`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/privacy/export` | Bearer | Export user data (GDPR) |
| POST | `/api/v1/privacy/delete` | Bearer | Request account deletion (30-day grace) |
| POST | `/api/v1/privacy/delete/cancel` | Bearer | Cancel pending deletion |
| GET | `/api/v1/privacy/delete/status` | Bearer | Check deletion status |
| GET | `/api/v1/privacy/requests` | Bearer | List DSR request history |
| GET | `/api/v1/privacy/kinds` | No | List valid DSR kinds |

---

## Legal (`/api/v1/legal`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/legal/documents` | Optional Bearer | List legal documents |
| GET | `/api/v1/legal/documents/{slug}` | No | Get document content |
| POST | `/api/v1/legal/accept` | Bearer | Accept documents |
| GET | `/api/v1/legal/acceptances/me` | Bearer | List user's acceptances |
| GET | `/api/v1/legal/attributions` | No | List data provider attributions |

---

## System (`/api/v1/system`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/system/status` | Bearer | Engine version, uptime, DB health, entity counts |

### GET /api/v1/system/status

```json
{
  "engine_version": "0.1.0",
  "uptime_seconds": 3642.1,
  "server_time": "2026-05-23T14:30:00Z",
  "components": [{ "name": "database", "healthy": true }],
  "counts": { "users": 42, "portfolios": 120, "backtests": 580, "webhooks_active": 8, "api_keys_active": 3 }
}
```

---

## Reference Data (`/api/v1/reference`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/reference/suggest` | No | Instrument typeahead / search |

Query params: `q` (required), `limit` (default 10, max 50), `asset_class` (optional filter).

Returns suggestions from local seed index, falling back to Yahoo Finance search.

---

## Observability

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/metrics` | No | Prometheus exposition format |

---

## WebSocket (`/api/v1/ws`)

WebSocket endpoint for real-time event streaming. Authenticates via query
param or first-message token. Streams events from the EventBus (order fills,
portfolio updates, etc.).

---

## Error Responses

All error responses follow this shape:

```json
{ "detail": "Human-readable error message" }
```

Common status codes:
- `400` — Validation error or bad request
- `401` — Missing or invalid auth token
- `403` — Insufficient permissions or unsigned legal documents
- `404` — Resource not found
- `409` — Conflict (duplicate, already exists)
- `422` — Pydantic validation error (field-level details)
- `429` — Rate limit exceeded
- `500` — Internal server error
- `503` — Upstream provider unavailable
