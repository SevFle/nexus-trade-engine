# API reference

Every HTTP and WebSocket endpoint exposed by the FastAPI app. The
source of truth is
[`engine/api/router.py`](../../engine/api/router.py) and the
per-area routers under
[`engine/api/routes/`](../../engine/api/routes/).

FastAPI also exposes interactive docs at runtime:

- Swagger UI — `http://localhost:8000/docs`
- ReDoc — `http://localhost:8000/redoc`
- OpenAPI JSON — `http://localhost:8000/openapi.json`

This page exists for engineers who want to read the surface
without standing up the server. For per-endpoint details see
[endpoints.md](endpoints.md).

## Auth model

Every protected route accepts one of two credentials in the
`Authorization` header:

| Credential                | Header                              | Use case                                   |
|---------------------------|--------------------------------------|---------------------------------------------|
| JWT access token          | `Authorization: Bearer <jwt>`        | Interactive sessions from `/api/v1/auth/login` |
| Long-lived API key        | `Authorization: Bearer nxs_<prefix>_<secret>` | Headless automation, CI, SDK clients |

JWTs are HS256-signed with `NEXUS_SECRET_KEY`. Access tokens live
`NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default 60); refresh tokens
live `NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS` (default 7) and are
rotated on every `/refresh` — reuse is detected and revokes the
user's whole session tree.

API keys are stored as a bcrypt hash of the secret portion. The full
token is returned **exactly once** on creation. Scopes are an
allow-list — see [`engine/api/auth/api_keys.py:50`](../../engine/api/auth/api_keys.py):

```python
VALID_SCOPES = frozenset({"read", "trade", "admin"})
```

Three roles exist:

| Role        | Read | Write | Live trading      | Admin |
|-------------|------|-------|-------------------|-------|
| `viewer`    | ✓    | —     | —                 | —     |
| `developer` | ✓    | ✓     | ✓ (own portfolios)| —     |
| `admin`     | ✓    | ✓     | ✓ (any portfolio) | ✓     |

Gated via `Depends(get_current_user)`,
`Depends(require_role("admin"))`, or
`Depends(require_api_scope("trade"))` — see
[`engine/api/auth/dependency.py`](../../engine/api/auth/dependency.py).

Some routes also require legal acceptance
(`Depends(require_legal_acceptance)`): the caller must have an
unrevoked `LegalAcceptance` row for every document currently marked
`requires_acceptance`. The list of documents is returned by
`GET /api/v1/legal/documents`. Affected routes include backtest,
portfolio, marketplace, strategies, market-data, reference,
scoring, and webhooks.

## Conventions

- All routes are JSON in / JSON out unless noted (CSV on
  `POST /api/v1/tax/report/{code}/csv`).
- Money and quantity fields are `Decimal`-precision. JSON encoding
  is either `float` (read paths where the engine rounds for
  display) or `string` (write paths where the caller must preserve
  precision — see the `DisposalRequest` schema in
  [`engine/api/routes/tax.py`](../../engine/api/routes/tax.py)).
- All timestamps are ISO 8601 with timezone offset; UTC server-side.
- Standard error shape: `{"detail": "<human readable>"}` with the
  appropriate HTTP status. Validation errors are FastAPI's default
  `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`.
- The server enforces a global rate limit (default 600 req/min/IP,
  burst 60) via
  [`engine/api/rate_limit.py`](../../engine/api/rate_limit.py).
  Per-route overrides live in `engine/app.py:create_app`. The body
  size is hard-capped at 1 MiB.

## Endpoint groups

| Group | Base path | Auth | Notes |
|---|---|---|---|
| Health & observability | `/health`, `/ready`, `/metrics` | none | Liveness / readiness / Prometheus. |
| Auth | `/api/v1/auth` | mixed | Register, login, refresh, logout, OAuth. |
| MFA | `/api/v1/auth/mfa` | JWT | TOTP enrollment + verification. |
| API keys | `/api/v1/auth/api-keys` | JWT | Long-lived bearer tokens. |
| Legal | `/api/v1/legal` | optional | Documents, acceptance, attributions. |
| Portfolio | `/api/v1/portfolio` | JWT + legal | CRUD on portfolios. |
| Backtest | `/api/v1/backtest` | JWT + legal | Run + fetch results. |
| Strategies | `/api/v1/strategies` | JWT + legal | Installed-plugin lifecycle. |
| Marketplace | `/api/v1/marketplace` | JWT + legal | **Stub** — see [limitations](../limitations.md). |
| Market data | `/api/v1/market-data` | JWT + legal | OHLCV bars + quote. |
| Reference | `/api/v1/reference` | (none today) | Instrument search / suggest. |
| Scoring | `/api/v1/scoring` | JWT + legal | Run + history for scoring strategies. |
| Tax | `/api/v1/tax` | JWT | Per-jurisdiction reports (US/GB/DE/FR). |
| Webhooks | `/api/v1/webhooks` | JWT + `trade` scope | Outbound delivery with HMAC signing. |
| Privacy / DSR | `/api/v1/privacy` | JWT | GDPR / CCPA export + delete. |
| System | `/api/v1/system` | JWT | Headless / CI probes. |
| Client errors | `/api/v1/client` | none (rate-limited) | Frontend `ErrorBoundary` sink. |
| WebSocket | `/api/v1/ws` | JWT or API key (in first frame) | Pub/sub event stream. |

See [endpoints.md](endpoints.md) for the full per-endpoint reference.

## Status codes

| Code | Meaning in this API |
|------|---------------------|
| 200  | OK. |
| 201  | Created (POST on a resource that persists). |
| 202  | Accepted — work continues asynchronously. |
| 204  | No content (DELETE, sink endpoints). |
| 400  | Validation / bad request. |
| 401  | Missing or invalid credentials. |
| 403  | Authenticated but not allowed (RBAC, legal acceptance, ownership). |
| 404  | Resource not found. |
| 405  | Method not allowed. |
| 409  | Conflict (duplicate email, MFA already enabled). |
| 429  | Rate limited (global or per-route). |
| 500  | Unhandled server error (Sentry captures it). |
| 503  | Upstream provider unavailable — usually transient. |
| 501  | Capability not implemented (provider doesn't serve this asset class). |
