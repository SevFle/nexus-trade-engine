# API reference

The HTTP surface is mounted on the FastAPI app from
[`engine/api/router.py`](../../engine/api/router.py). All routes are
prefixed `/api/v1/...` except health, metrics, and legal-doc reads
which sit at the root for probe convenience.

## Conventions

- **Content type:** `application/json` for everything that takes a
  body. The lone exception is `POST /api/v1/tax/report/{code}/csv`
  which returns `text/csv`.
- **Authentication:** `Authorization: Bearer <jwt>` for interactive
  sessions, or `X-API-Key: <key>` for headless clients. The auth
  dependency in
  [`engine/api/auth/dependency.py`](../../engine/api/auth/dependency.py)
  resolves both; a JWT-authenticated request bypasses the per-key
  scope check, an API-key request must declare a sufficient scope.
- **Authorization:** role-based via `require_role("...")` and
  scope-based via `require_api_scope("...")`. See [auth](auth.md).
- **Errors:** plain `{"detail": "..."}` JSON for HTTP errors. The
  FastAPI default. Validation errors come back as RFC 7807-style
  `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`.
- **Timestamps:** all `*_at` fields are ISO-8601 with timezone
  offset (UTC on the server side).
- **Pagination:** list endpoints take an unstructured `limit`
  query param (default 50, capped at 200). Cursor pagination is not
  yet implemented — see [known limitations](../operations/known-limitations.md).
- **Idempotency:** none of the write endpoints are idempotent
  today. Repeat submissions create duplicate rows.
- **Body size:** requests are capped at 1 MiB by
  [`BodySizeLimitMiddleware`](../../engine/api/body_size_limit.py)
  before they reach the route handler. Per-route Pydantic models may
  tighten this further.

## Route inventory

| Prefix                                | Module                                          | Auth                                 |
|---------------------------------------|-------------------------------------------------|--------------------------------------|
| `/health`, `/ready`, `/health/providers` | [`health.py`](../../engine/api/routes/health.py) | Public                              |
| `/metrics`                            | [`metrics.py`](../../engine/api/routes/metrics.py) | Public (Prometheus scrape)         |
| `/api/v1/legal/*`                     | [`legal.py`](../../engine/api/routes/legal.py)  | Public read; user for `accept`       |
| `/api/v1/auth/*`                      | [`auth.py`](../../engine/api/routes/auth.py)    | Provider-dependent                   |
| `/api/v1/auth/mfa/*`                  | [`mfa.py`](../../engine/api/routes/mfa.py)      | JWT                                  |
| `/api/v1/auth/api-keys`               | [`api_keys.py`](../../engine/api/routes/api_keys.py) | JWT                              |
| `/api/v1/system/*`                    | [`system.py`](../../engine/api/routes/system.py) | JWT                                  |
| `/api/v1/privacy/*`                   | [`privacy.py`](../../engine/api/routes/privacy.py) | JWT                              |
| `/api/v1/ws`                          | [`websocket.py`](../../engine/api/routes/websocket.py) | JWT query param                |
| `/api/v1/backtest/*`                  | [`backtest.py`](../../engine/api/routes/backtest.py) | JWT + legal accept            |
| `/api/v1/client/*`                    | [`client_errors.py`](../../engine/api/routes/client_errors.py) | Public (rate-limited)   |
| `/api/v1/portfolio/*`                 | [`portfolio.py`](../../engine/api/routes/portfolio.py) | JWT + legal accept         |
| `/api/v1/strategies/*`                | [`strategies.py`](../../engine/api/routes/strategies.py) | JWT + legal accept     |
| `/api/v1/webhooks`                    | [`webhooks.py`](../../engine/api/routes/webhooks.py) | JWT (create requires `trade` scope) |
| `/api/v1/marketplace/*`               | [`marketplace.py`](../../engine/api/routes/marketplace.py) | JWT + legal accept    |
| `/api/v1/reference/*`                 | [`reference.py`](../../engine/api/routes/reference.py) | Public                     |
| `/api/v1/tax/*`                       | [`tax.py`](../../engine/api/routes/tax.py)      | JWT                                  |
| `/api/v1/scoring/*`                   | [`scoring.py`](../../engine/api/routes/scoring.py) | JWT + legal accept              |
| `/api/v1/market-data/*`               | [`market_data.py`](../../engine/api/routes/market_data.py) | JWT + legal accept         |

## Authentication model

The engine accepts two credential kinds on the same routes:

1. **JWT** (`Authorization: Bearer <jwt>`). Issued by `POST /auth/login`
   (or any federated provider callback). 1-hour access, 7-day refresh.
   JWT auth implies full scope — role checks gate access.
2. **API key** (`X-API-Key: <key>`, or `Authorization: Bearer nxs_...`).
   Long-lived, bcrypt-hashed at rest. The plaintext token is returned
   **once** on creation. API keys carry a scope (`read`, `trade`,
   `admin`) that is enforced independently of role.

A request without either credential is rejected with `401
Authentication required` by
[`get_current_user`](../../engine/api/auth/dependency.py:102).

### Role hierarchy

```
viewer (0) < user (1) < retail_trader (2) < quant_dev (3) < developer (4)
        < portfolio_manager (5) < admin (6)
```

Roles are enforced via `Depends(require_role("quant_dev"))`. The check
is monotonic — a higher role satisfies a lower requirement. See
[`ROLE_HIERARCHY`](../../engine/api/auth/dependency.py:27).

### Scope hierarchy (API keys only)

```
read (0) < trade (1) < admin (2)
```

`read` allows `GET`/`HEAD` only. `trade` adds mutating endpoints on
the user's owned resources (portfolios, webhooks, backtests).
`admin` is equivalent to the `admin` role and supersedes both.

JWT-authenticated requests **bypass** the scope check (they are
gated by role alone). See
[`require_api_scope`](../../engine/api/auth/dependency.py:168).

### Legal acceptance gate

Most data routes are mounted with
[`require_legal_acceptance`](../../engine/legal/dependencies.py)
as a router-level dependency. The dependency rejects requests with
`403 Legal acceptance required` if the user has not signed the
current version of every document marked `requires_acceptance=True`.
The check is per-request; new acceptances take effect immediately.

## Rate limiting

Global defaults: **600 req/min/IP, 60 req burst**. Configurable via
`NEXUS_RATE_LIMIT_PER_MINUTE` and `NEXUS_RATE_LIMIT_BURST`. Health
and metrics are exempt (`NEXUS_RATE_LIMIT_EXEMPT_PATHS`).

Per-route overrides live in
[`engine/app.py:create_app`](../../engine/app.py:175). The current
overrides:

| Path                          | Limit            | Why                                                                  |
|-------------------------------|------------------|----------------------------------------------------------------------|
| `/api/v1/client/errors`       | 30 req/min, 30 burst | Frontend `ErrorBoundary` log spam containment (gh#318).          |

Implementation: in-memory sliding window per client IP, keyed on the
rightmost non-private `X-Forwarded-For` hop when
`trusted_proxy_depth > 0`. The default is `0`, which means the engine
treats the socket peer as the client; only raise it after a trusted
reverse proxy is the only path in.

## Body size, security headers, CORS

- **Body size cap:** 1 MiB (`BodySizeLimitMiddleware`,
  [`engine/api/body_size_limit.py`](../../engine/api/body_size_limit.py)).
- **Security headers:** `SecurityHeadersMiddleware` adds
  `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`,
  HSTS in production, plus a `Content-Security-Policy` header
  tuned for the React frontend.
- **CORS:** defaults to `http://localhost:3000`. Production values
  come from `NEXUS_CORS_ORIGINS` (JSON list).

## Errors by status

| Status | When                                                                                       |
|--------|--------------------------------------------------------------------------------------------|
| 400    | Pydantic validation failure, malformed UUID, unsupported template name.                    |
| 401    | Missing/invalid JWT or API key.                                                            |
| 403    | Insufficient role/scope, missing legal acceptance, attempt to read another user's resource. |
| 404    | Resource not found, or found but not owned by caller (we deliberately do not leak existence). |
| 409    | Registration conflict (duplicate email).                                                   |
| 422    | Semantically invalid payload that passed Pydantic (rare — usually a 400).                  |
| 429    | Rate limit exceeded. `Retry-After` set.                                                    |
| 500    | Unhandled exception. Sentry capture attempted.                                             |
| 502/504 | Upstream IdP or market-data provider failure.                                              |

Per-route deviations are noted in each subsection below.

## Per-area reference

- [Auth & MFA](auth.md)
- [Portfolios](portfolios.md)
- [Backtest](backtest.md)
- [Market data](market-data.md)
- [Strategies](strategies.md)
- [Marketplace](marketplace.md)
- [Tax reports](tax.md)
- [Strategy scoring](scoring.md)
- [Webhooks](webhooks.md)
- [WebSocket stream](websocket.md)
- [Privacy (GDPR / CCPA)](privacy.md)
- [Legal documents](legal.md)
- [Reference data (instrument search)](reference.md)
- [System & health](system.md)
