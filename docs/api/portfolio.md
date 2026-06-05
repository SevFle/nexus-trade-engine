# Portfolio API

Mounted at `/api/v1/portfolio`. Implementation:
`engine/api/routes/portfolio.py`. The router is wrapped in
`Depends(require_legal_acceptance)` — the user must have accepted the
current version of every required legal document before any call
succeeds.

A *portfolio* is the organizing unit for trading state. Each user may
have any number of portfolios. A portfolio owns positions, orders,
installed strategies, tax lots, and backtest results.

## POST /

Create a portfolio.

**Auth** — required. Legal acceptance required.

**Request body** `CreatePortfolioRequest`:
```json
{
  "name": "Long-term growth",
  "description": "Buy-and-hold equities",
  "initial_capital": 100000.0
}
```

| Field             | Type    | Default      | Constraints        |
|-------------------|---------|--------------|--------------------|
| `name`            | string  | required     | 1–100 chars        |
| `description`     | string  | `""`         |                    |
| `initial_capital` | number  | `100000.0`   | `>= 0`             |

**Response** `PortfolioResponse` (200):
```json
{
  "id": "uuid",
  "name": "Long-term growth",
  "description": "Buy-and-hold equities",
  "initial_capital": 100000.0,
  "created_at": "2026-06-05T12:00:00Z"
}
```

## GET /

List the caller's portfolios. Not paginated — bounded by per-user
cardinality.

**Response** — `list[PortfolioResponse]`.

## GET /{portfolio_id}

Read one portfolio.

**Path** — `portfolio_id` (UUID string).

**Response** — `PortfolioResponse`.

**Errors**
- `400 Bad Request` — `{"detail": "Invalid portfolio ID"}` if the path
  param isn't a UUID.
- `403 Forbidden` — portfolio exists but belongs to another user.
- `404 Not Found` — no portfolio with that id.

A `403` versus `404` distinction is intentional: it leaks the
existence of a portfolio owned by another user, which is acceptable
here because UUIDs are 128 bits of entropy — enumeration is
infeasible.

## DELETE /{portfolio_id}

Delete a portfolio. Cascades to positions, orders, tax lots,
installed strategies, and backtest results (`ON DELETE CASCADE` in the
schema).

**Response** — `{"status": "deleted", "id": "<uuid>"}`.

This is a hard delete. There is no soft-archive state today; see
[`limitations.md`](../limitations.md).

## What lives *under* a portfolio

| Related entity        | Owner module                      | API surface                              |
|-----------------------|-----------------------------------|------------------------------------------|
| Positions             | `engine.db.models.Position`       | (none yet — read via backtest results)   |
| Orders                | `engine.db.models.Order`          | (none yet — surfaced via OMS internals)  |
| Installed strategies  | `engine.db.models.InstalledStrategy` | `POST /api/v1/strategies/{id}/activate` |
| Tax lots              | `engine.db.models.TaxLotRecord`   | (none yet — surfaced via tax reports)    |
| Backtest results      | `engine.db.models.BacktestResult` | `GET /api/v1/backtest/results/{id}`      |
| Webhook configs       | `engine.db.models.WebhookConfig`  | `POST /api/v1/webhooks` (optional)       |

The portfolio routes intentionally expose only the minimum CRUD.
Operations on the children happen through their dedicated routes so
auth and rate-limit policies can be tuned per area.
