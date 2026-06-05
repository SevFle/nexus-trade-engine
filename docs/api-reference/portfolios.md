# Portfolios

A portfolio is the top-level container for one strategy stack: it
holds positions, orders, tax lots, installed strategies, and
scoped webhook configs. Source:
[`engine/api/routes/portfolio.py`](../../engine/api/routes/portfolio.py).

**Legal gate:** all routes are mounted with
[`require_legal_acceptance`](../../engine/legal/dependencies.py).

## Endpoints

### `POST /api/v1/portfolio/`

Create a portfolio for the calling user.

**Auth:** JWT or API key with `trade` scope.

**Request body** — `CreatePortfolioRequest`:

```json
{
  "name": "Long-term US equity",
  "description": "Buy-and-hold tech + healthcare",
  "initial_capital": 100000.0
}
```

Validation: `name` 1–100 chars; `initial_capital ≥ 0`.

**Response** `200 OK` — `PortfolioResponse`:

```json
{
  "id": "uuid",
  "name": "Long-term US equity",
  "description": "...",
  "initial_capital": 100000.0,
  "created_at": "2026-06-05T12:00:00Z"
}
```

### `GET /api/v1/portfolio/`

List the calling user's portfolios. The list is unordered by
default (insertion order is whatever Postgres returns).

**Response** `200 OK` — `PortfolioResponse[]`.

### `GET /api/v1/portfolio/{portfolio_id}`

Fetch one portfolio by id.

**Path:** `portfolio_id` (UUID string).

`400 Invalid portfolio ID` on malformed UUID.
`404 Portfolio not found` if missing.
`403 Access denied` if the portfolio belongs to another user
— we do **not** leak existence; `403` and `404` are returned for
the same case in different code paths, see
[`portfolio.py:75-100`](../../engine/api/routes/portfolio.py:75).

### `DELETE /api/v1/portfolio/{portfolio_id}`

Hard-delete the portfolio row. CASCADE removes positions, orders,
tax lots, installed strategies, scoped webhook configs, and
backtest results tied to it. **This is not an archive; the data is
gone.** A soft-delete / archive flag is on the roadmap (see
[known limitations](../operations/known-limitations.md)).

**Response** `200 OK`:

```json
{ "status": "deleted", "id": "<uuid>" }
```

## Notes

- All four endpoints require a signed-in user. There is no
  shared / team portfolio concept yet.
- `initial_capital` is a `Numeric(18, 4)` in Postgres and is
  serialized as `float` on the wire. If you need full precision
  for downstream calculations, prefer reading the column directly
  (the SDK does this).
- The route handler does not commit the transaction itself; the
  FastAPI dependency in
  [`engine/deps.py`](../../engine/deps.py) commits at request exit
  and rolls back on exception.
