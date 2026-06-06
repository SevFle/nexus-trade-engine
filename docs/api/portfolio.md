# Portfolio API

Base path: `/api/v1/portfolio`. Source:
[`engine/api/routes/portfolio.py`](../../engine/api/routes/portfolio.py).

A portfolio is the container for one trading account: it scopes
positions, orders, tax lots, installed strategies, backtests, and
webhooks. Portfolios are owned by exactly one user; the user's role
governs what they can do across all of their portfolios.

Every endpoint below is mounted with `require_legal_acceptance` — the
caller must have accepted every `requires_acceptance=true` legal
document at its current version. `403 LegalAcceptanceRequired`
otherwise.

## Endpoints

### `POST /api/v1/portfolio/`

Create a portfolio.

**Auth**: Bearer JWT or API key with `trade`+ scope.

**Request body**:

```json
{
  "name": "Tech Stocks",
  "description": "Long-only tech equity allocation.",
  "initial_capital": 100000.0
}
```

| Field             | Type   | Default   | Constraints                       |
|-------------------|--------|-----------|-----------------------------------|
| `name`            | string | required  | 1–100 chars.                      |
| `description`     | string | `""`      |                                   |
| `initial_capital` | number | `100_000` | `>= 0`. Stored as `NUMERIC(18, 4)`. |

**Response**: `201 Created` (returned as `200` by the current
handler):

```json
{
  "id": "uuid",
  "name": "Tech Stocks",
  "description": "Long-only tech equity allocation.",
  "initial_capital": 100000.0,
  "created_at": "2026-06-06T12:00:00Z"
}
```

### `GET /api/v1/portfolio/`

List the caller's portfolios. Ordered by `created_at` ascending.

**Auth**: Bearer JWT or API key with `read`+ scope.

**Response**: `200 OK`:

```json
[
  {
    "id": "uuid",
    "name": "Tech Stocks",
    "description": "...",
    "initial_capital": 100000.0,
    "created_at": "2026-06-06T12:00:00Z"
  }
]
```

Only portfolios owned by the caller are returned. There is no admin
override route today.

### `GET /api/v1/portfolio/{portfolio_id}`

Fetch one portfolio. Returns `404` if the portfolio does not exist;
returns `403` if it exists but is owned by another user (we do not
leak existence across ownership boundaries).

**Path params**: `portfolio_id` — UUID. `400` if the string is not a
valid UUID.

**Response**: `200 OK` — same shape as the create response.

### `DELETE /api/v1/portfolio/{portfolio_id}`

Hard-delete a portfolio. Cascades to `positions`, `orders`,
`tax_lot_records`, `installed_strategies`, `backtest_results`, and
any portfolio-scoped `webhook_configs`. **This is not reversible.**

**Response**: `200 OK`:

```json
{ "status": "deleted", "id": "uuid" }
```

## Notes

- The endpoint set is intentionally minimal: there is no
  `PATCH /api/v1/portfolio/{id}` for renaming or updating description
  yet. Add it when the UI starts asking for it; keep the body shape
  consistent with the create request.
- Positions, orders, and tax lots are *not* exposed through this
  router; they belong to the OMS surface which is still landing.
  Until then, use `GET /api/v1/system/status` for a count and query
  the database directly for inspection.
