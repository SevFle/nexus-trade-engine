# API Reference

This directory documents the HTTP and WebSocket surface exposed by the
FastAPI engine. The application factory is
[`engine/app:create_app`](../../engine/app.py) and every route is
registered through [`engine/api/router.py`](../../engine/api/router.py).

The live OpenAPI document is at `GET /openapi.json` on any running engine
and the interactive UI is at `/docs` (production deploys should disable
the UI via `app_debug=False`). **This Markdown is the human-authored
source of truth for behaviour, auth, error semantics, and rate limits.**
The OpenAPI document is generated from the same Pydantic models but is
not authoritative for non-obvious decisions (e.g. when we return 202 vs
200, or why some routes are gated by `require_legal_acceptance`).

## Conventions

- **Base path:** `/api/v1` for everything except `/health`, `/ready`,
  `/metrics`, and the legal top-level routes (`/api/v1/legal/...`).
- **Auth:** every protected route accepts either
  `Authorization: Bearer <jwt>` or `X-API-Key: <prefix.secret>`. The
  dependency in [`engine/api/auth/dependency.py`](../../engine/api/auth/dependency.py)
  resolves both.
- **Body size:** 1 MiB hard cap enforced by
  [`engine/api/body_size_limit.py`](../../engine/api/body_size_limit.py).
  Larger uploads must go through a pre-signed object store.
- **Rate limit:** global 600 req/min/IP with a 60-req burst
  (configurable via `NEXUS_RATE_LIMIT_*`). `/api/v1/client/errors` is
  pinned to 30 req/min so a buggy frontend cannot DoS the log pipeline.
- **Correlation:** every response carries `x-request-id` set by
  [`engine/observability/middleware.py`](../../engine/observability/middleware.py).
  Echo it back when reporting issues.
- **Errors:** problem-shape `{"detail": "..."}` for FastAPI HTTPException
  routes; some older routes return plain dicts — see the per-route docs.
- **Timestamps:** all datetimes are ISO-8601 with timezone (`Z` or
  `+00:00`). Naive timestamps are bugs.
- **Money:** `Decimal` values cross the wire as **strings** so JSON
  parsers do not silently lose precision. See `DisposalRequest.proceeds`
  in [privacy-legal.md](privacy-legal.md) for the pattern.

## Auth, role hierarchy, and scopes

The dependency resolver maps every request to a `User` row. Roles follow
a strict total order (defined in
[`engine/api/auth/dependency.py:ROLE_HIERARCHY`](../../engine/api/auth/dependency.py)):

```
viewer (0) < user (1) < retail_trader (2) < quant_dev (3)
          < developer (4) < portfolio_manager (5) < admin (6)
```

Routes that need a minimum role use `require_role("developer")` (and
similarly `require_api_scope("trade")` for API-key-scoped operations).
`require_role` checks the *minimum* role — a `portfolio_manager` can hit
a `developer`-gated route.

API keys are issued with one or more of these scopes:
`read`, `trade`, `write`, `admin`. The full allow-list lives in
[`engine/api/auth/api_keys.py:VALID_SCOPES`](../../engine/api/auth/api_keys.py).

For more on login, MFA, OAuth, and API-key lifecycle see
[auth.md](auth.md).

## Index by domain

| Domain | File | Routes |
|--------|------|--------|
| Auth, sessions, MFA, API keys | [auth.md](auth.md) | `/api/v1/auth/*`, `/api/v1/auth/api-keys/*`, `/api/v1/auth/mfa/*` |
| Portfolios, strategies, backtests, scoring | [trading.md](trading.md) | `/api/v1/portfolio`, `/api/v1/strategies`, `/api/v1/backtest`, `/api/v1/scoring` |
| Market data, reference / search, marketplace | [data.md](data.md) | `/api/v1/market-data`, `/api/v1/reference`, `/api/v1/marketplace` |
| Webhooks | [webhooks.md](webhooks.md) | `/api/v1/webhooks` |
| Privacy (GDPR/CCPA), legal docs, tax reports | [privacy-legal.md](privacy-legal.md) | `/api/v1/privacy/*`, `/api/v1/legal/*`, `/api/v1/tax/*` |
| Health, readiness, metrics, system status, WebSocket, client-error reporting | [observability.md](observability.md) | `/health`, `/ready`, `/metrics`, `/api/v1/system`, `/api/v1/client/errors`, `/api/v1/ws` |

## Why this is split

Each file is under 500 lines so it stays reviewable. Adding a new domain?
Add a new file and a row to the table above; do not extend an existing
file past 500 lines.

## Out of scope for these docs

- **Strategy plugin authoring** — see [PLUGIN_DEV_GUIDE.md](../PLUGIN_DEV_GUIDE.md).
- **Internal data-model relationships** — see
  [architecture/database.md](../architecture/database.md).
- **Operational alerting & SLOs** — see [operations/slos.md](../operations/slos.md).
