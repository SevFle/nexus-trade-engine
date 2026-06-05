# API reference

The engine exposes a FastAPI app at `engine.app:create_app`. The full
machine-readable surface is at `/openapi.json` and `/docs` (Swagger UI)
when the engine is running; this directory is the human-readable
counterpart that explains *why* each route exists and how to use it
correctly.

## Base URLs

| Environment   | Base URL                      |
|---------------|-------------------------------|
| Local dev     | `http://localhost:8000`       |
| Docker dev    | `http://localhost:8000`       |
| Production    | Operator-configured (TLS terminated at the reverse proxy) |

All routes documented here are prefixed with `/api/v1` unless noted.
Health and metrics live at the root: `/health`, `/ready`, `/metrics`.

## Authentication

Two credential shapes are accepted on protected routes:

- **JWT** in `Authorization: Bearer <token>`. Issued by `POST
  /api/v1/auth/login` or `/refresh`. Default TTL: 60 minutes (access)
  / 7 days (refresh).
- **API key** in `X-API-Key: nxs_<env>_<random>`. Issued by `POST
  /api/v1/auth/api-keys`. The full token is shown **exactly once**.

Some routes enforce a **role** (`require_role`) and others enforce a
**scope** (`require_api_scope`). JWT-authenticated sessions bypass
scope checks; API-key sessions bypass role checks. See
[`architecture/data-flow.md`](../architecture/data-flow.md#auth-dependency-resolution)
for the resolution order.

## Roles and scopes

| Role              | Level | Notes                                                      |
|-------------------|-------|------------------------------------------------------------|
| `viewer`          | 0     | Read-only                                                  |
| `user`            | 1     | Default for new local signups                              |
| `retail_trader`   | 2     | Owns portfolios, places orders                             |
| `quant_dev`       | 3     | Author strategies                                          |
| `developer`       | 4     | Marketplace install / uninstall                            |
| `portfolio_manager` | 5   | Cross-portfolio operations                                 |
| `admin`           | 6     | Full access                                                |

| Scope   | Allows                                                |
|---------|-------------------------------------------------------|
| `read`  | GET-only                                              |
| `trade` | POST / PUT / PATCH for backtest, portfolio, webhooks  |
| `admin` | Equivalent to the admin role                          |

Role mapping for federated providers (OAuth/OIDC/LDAP) is **never
implicitly promoted** — see [`engine/api/auth/base.py:map_roles`](../../engine/api/auth/base.py).

## Rate limits

Global: 600 req/min/IP, 60-burst. Per-route overrides:

| Path                          | Limit       |
|-------------------------------|-------------|
| `/api/v1/client/errors`       | 30 req/min  |
| `/health`, `/metrics`         | Exempt      |

Body size hard cap: 1 MiB on every route.

## Coverage by area

| Area          | File                              | Highlights                                                  |
|---------------|-----------------------------------|-------------------------------------------------------------|
| Auth          | [`auth.md`](auth.md)              | `/register`, `/login`, `/refresh`, `/me`, `/logout`, OAuth  |
| MFA           | [`mfa.md`](mfa.md)                | TOTP enroll/verify, backup codes                            |
| API keys      | [`api-keys.md`](api-keys.md)      | Long-lived scoped tokens                                    |
| Portfolio     | [`portfolio.md`](portfolio.md)    | CRUD on portfolios                                          |
| Backtest      | [`backtest.md`](backtest.md)      | Submit + poll                                               |
| Market data   | [`market-data.md`](market-data.md)| OHLCV bars + quotes across providers                         |
| Strategies    | [`strategies.md`](strategies.md)  | List, activate, reload                                      |
| Marketplace   | [`marketplace.md`](marketplace.md)| Browse, install (partial)                                   |
| Scoring       | [`scoring.md`](scoring.md)        | Run cross-sectional scoring                                 |
| Tax           | [`tax.md`](tax.md)                | Per-jurisdiction reports + CSV                              |
| Webhooks      | [`webhooks.md`](webhooks.md)      | CRUD, test fire, delivery history                           |
| Privacy / DSR | [`privacy.md`](privacy.md)        | Export, deletion-with-grace                                 |
| Legal         | [`legal.md`](legal.md)            | Documents, acceptance, attributions                         |
| Reference     | [`reference.md`](reference.md)    | Instrument search / typeahead                               |
| System        | [`system.md`](system.md)          | Health, ready, status, metrics                              |
| WebSocket     | [`websocket.md`](websocket.md)    | Subscribe to topics                                         |

## Common response shapes

All errors are returned as `{"detail": "<message>"}` with the
appropriate HTTP status. Validation errors include Pydantic's
structured `details` array.

Success responses are documented per-endpoint. The engine never
returns a list at the root of a JSON response without a count or
pagination — most list endpoints return `{"<resource>s": [...],
"count": N}` or accept `?limit` / `?offset` query params.

## Idempotency and concurrency

- The engine does **not** implement idempotency keys. Retry-safe
  endpoints are read-only. Mutating endpoints (`POST /portfolio`,
  `POST /webhooks`, etc.) should not be retried blindly — the second
  call will create a duplicate row.
- Optimistic concurrency control is **not** applied; last-write-wins
  on PUT/PATCH.
- Long-running work (backtests, scoring) returns a job id immediately
  and the result is fetched separately.

## Pagination

Where supported, list endpoints use `?page` / `?per_page` or
`?limit` / `?offset`. Default page size is 20; max is 50–200 per
endpoint (enforced by Pydantic). Endpoints with bounded cardinality
(portfolios, webhooks) often return the full list with no pagination.

## OpenAPI / Swagger

FastAPI auto-generates:

- `/openapi.json` — machine-readable schema.
- `/docs` — Swagger UI.
- `/redoc` — ReDoc UI.

These are enabled in every environment today. Operators who want to
disable them in production should add a toggle to `engine/config.py`
and conditionally set `FastAPI(docs_url=None, redoc_url=None,
openapi_url=None)` based on `settings.is_production` — there is an
open item in [`limitations.md`](../limitations.md).
