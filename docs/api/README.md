# API reference

Every public HTTP route exposed by the engine. This directory mirrors
the structure of [`engine/api/routes/`](../../engine/api/routes/):

| File                                                | Surface                                              |
|-----------------------------------------------------|------------------------------------------------------|
| [`auth.md`](auth.md)                                | `/api/v1/auth/*` — register, login, refresh, MFA, OAuth callbacks. |
| [`api-keys.md`](api-keys.md)                        | `/api/v1/auth/api-keys` — long-lived scoped tokens.  |
| [`health.md`](health.md)                            | `/health`, `/ready`, `/metrics`.                     |
| [`legal.md`](legal.md)                              | `/api/v1/legal/*` — documents, acceptances, attributions. |
| [`portfolio.md`](portfolio.md)                      | `/api/v1/portfolio/*` — CRUD on portfolios.          |
| [`strategies.md`](strategies.md)                    | `/api/v1/strategies/*` — list, activate, reload, health. |
| [`backtest.md`](backtest.md)                        | `/api/v1/backtest/*` — submit a run, fetch results.  |
| [`market-data.md`](market-data.md)                  | `/api/v1/market-data/*` — bars + quotes.             |
| [`reference.md`](reference.md)                      | `/api/v1/reference/suggest` — instrument typeahead.  |
| [`scoring.md`](scoring.md)                          | `/api/v1/scoring/*` — scoring strategy runs.         |
| [`webhooks.md`](webhooks.md)                        | `/api/v1/webhooks/*` — outbound webhook CRUD + delivery history. |
| [`tax.md`](tax.md)                                  | `/api/v1/tax/report/*` — per-jurisdiction tax summaries. |
| [`privacy.md`](privacy.md)                          | `/api/v1/privacy/*` — GDPR / CCPA DSR.               |
| [`marketplace.md`](marketplace.md)                  | `/api/v1/marketplace/*` — strategy marketplace (partial). |
| [`system.md`](system.md)                            | `/api/v1/system/status` — headless status probe.     |
| [`client-errors.md`](client-errors.md)              | `/api/v1/client/errors` — frontend ErrorBoundary sink. |
| [`websocket.md`](websocket.md)                      | `WS /api/v1/ws` — subscribe / publish protocol.      |

## Conventions

### Base URL

All examples assume the engine is at `http://localhost:8000`. The
deployed URL is whatever your reverse proxy fronts uvicorn with.

### Content type

Every write endpoint expects `application/json`. The WebSocket
endpoint speaks JSON over the WS frames. There is one CSV-producing
endpoint (`POST /api/v1/tax/report/{code}/csv`); everything else is
JSON in and JSON out.

### Auth

The engine accepts two credential shapes, both via `Authorization:
Bearer <token>`:

1. **JWT** — issued by `/api/v1/auth/login`, `/register`, `/refresh`,
   or any OAuth `/callback`. Default 60-minute TTL. The dependency
   `get_current_user` decodes the JWT, looks up the user, and rejects
   inactive accounts.
2. **API key** — long-lived token of shape `nxs_<env>_<32-hex-chars>`,
   issued at `POST /api/v1/auth/api-keys`. Recognised by the leading
   `nxs_` prefix and looked up by its 12-char display prefix.

Alternatively, an API key can be supplied in the `X-API-Key` header —
useful for clients that cannot set `Authorization` cleanly (e.g. some
SDK clients, browser fetch with a fixed auth scheme).

JWT-authenticated requests are gated by the user's **role** (see
[`auth.md` roles table](auth.md#roles)). API-key requests are gated by
the key's **scopes** (see [`api-keys.md`](api-keys.md)). A request
from an API key will fail with `403` if the key lacks the required
scope, even if the underlying user has the role.

### Common status codes

| Code | Meaning                                                                              |
|------|--------------------------------------------------------------------------------------|
| 200  | Success (GET, PUT).                                                                  |
| 201  | Created (POST that creates a row).                                                   |
| 202  | Accepted — work has been queued (e.g. `POST /api/v1/backtest/run`).                  |
| 204  | No content (DELETE).                                                                 |
| 400  | Bad request — body failed Pydantic validation, or a domain rule rejected the input.  |
| 401  | No credential, or credential did not resolve to an active user.                      |
| 403  | Authenticated but lacking the required role or scope.                                |
| 404  | Resource does not exist *or* is not owned by the caller. We do not leak existence.   |
| 409  | State conflict (e.g. MFA already enrolled, deletion already pending).                |
| 422  | Pydantic validation error response body — see FastAPI's standard error shape.        |
| 503  | Upstream dependency unavailable (DB, Valkey, market-data provider).                  |

### Errors

Validation errors come back in FastAPI's standard shape:

```json
{ "detail": [ { "loc": ["body", "email"], "msg": "...", "type": "..." } ] }
```

Domain errors come back as:

```json
{ "detail": "<human-readable message>" }
```

The string in `detail` is intentionally free-form — clients should
treat it as user-displayable text and key off the HTTP status code
for control flow.

### Rate limiting

The default is **600 req/min per IP** with a burst of 60. The
`/api/v1/client/errors` endpoint has a tighter 30 req/min cap to
prevent a buggy ErrorBoundary from flooding the log pipeline. The
`/health` and `/metrics` endpoints are exempt.

Headers `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and
`X-RateLimit-Reset` are not currently set; rely on the `429 Too Many
Requests` response status. (Tracking issue: the rate-limit middleware
predates our `RateLimitMiddleware` — adding the headers is on the
limitation list.)

### Pagination

There is no global pagination convention yet. Endpoints that return
collections either:

- return the entire list (typical for collections that fit in one
  page, e.g. portfolios for a user), or
- accept `limit` + `offset` query params with documented caps (e.g.
  `GET /api/v1/scoring/{name}/results` — `limit` ≤ 100).

When adding a new collection endpoint, prefer `limit` + `offset`
unless the entire collection is bounded by ownership (one user's
records, expected cardinality < 1000).

### Idempotency

No endpoint currently honours `Idempotency-Key`. Writes that take a
client-supplied `id` (e.g. webhook creation) are de-duplicated by the
unique constraint on `id`; writes that generate server-side ids are
not safe to retry blindly. Add an idempotency layer before going to
multi-instance production (see [`../limitations.md`](../limitations.md)).

### OpenAPI

The live OpenAPI document is at `/openapi.json`; the interactive UI is
at `/docs` (Swagger) and `/redoc` (ReDoc). These are enabled in
development; operators may disable them in production by setting
`app_debug = false` and not exposing the routes through the reverse
proxy.

## Cross-cutting dependencies

A few routes carry an extra dependency beyond `get_current_user`:

- **`require_legal_acceptance`** — applied at the router level to
  `backtest`, `scoring`, `market-data`, `portfolio`, `strategies`,
  `marketplace`. Rejects the request with `403` if the user has not
  accepted the current version of every `requires_acceptance=true`
  legal document.
- **`require_role("developer")`** — applied to
  `POST /api/v1/marketplace/install` and `DELETE /api/v1/marketplace/uninstall/{id}`.
- **`require_api_scope("trade")`** — applied to
  `POST /api/v1/webhooks`.

## Changelog for the API surface

The HTTP surface is versioned (`/api/v1`). Breaking changes ship under
`/api/v2` with a deprecation window — but to date there has been no
v2 cut. Non-breaking changes (new endpoints, new optional fields) are
added under v1 with a CHANGELOG entry.
