# Portfolio API

CRUD for portfolios owned by the calling user. Implementation:
[`engine/api/routes/portfolio.py`](../../engine/api/routes/portfolio.py).

A `Portfolio` is the top-level container for trading state: it owns
positions, orders, tax lots, and installed strategies. The model lives
at [`engine/db/models.py:Portfolio`](../../engine/db/models.py).

## Endpoint summary

| Method | Path | Auth | Legal | Purpose |
|---|---|---|---|---|
| `POST`   | `/api/v1/portfolio/`             | JWT/API key | required | Create a portfolio |
| `GET`    | `/api/v1/portfolio/`             | JWT/API key | required | List caller's portfolios |
| `GET`    | `/api/v1/portfolio/{portfolio_id}` | JWT/API key | required | Get one portfolio |
| `DELETE` | `/api/v1/portfolio/{portfolio_id}` | JWT/API key | required | Hard-delete a portfolio (cascades to positions / orders / tax lots / installed_strategies) |

All routes return `403` if the caller tries to access a portfolio they
do not own. There is no `PUT`/`PATCH` today — to rename, delete and
recreate. (This is a known limitation; see
[`../known-limitations.md`](../known-limitations.md).)

## Schemas

```python
class CreatePortfolioRequest(BaseModel):
    name: str                  # 1-100 chars
    description: str = ""
    initial_capital: float = 100_000.0   # ≥ 0

class PortfolioResponse(BaseModel):
    id: str                    # UUID as string
    name: str
    description: str
    initial_capital: float
    created_at: str            # ISO-8601 UTC
```

## Examples

```bash
# Create
curl -X POST http://localhost:8000/api/v1/portfolio/ \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"name":"Tech Long","initial_capital":250000}'
# => {"id": "...", "name": "Tech Long", ...}

# List
curl http://localhost:8000/api/v1/portfolio/ \
  -H 'authorization: Bearer <access>'

# Get one
curl http://localhost:8000/api/v1/portfolio/<id> \
  -H 'authorization: Bearer <access>'

# Delete (hard delete — cascades)
curl -X DELETE http://localhost:8000/api/v1/portfolio/<id> \
  -H 'authorization: Bearer <access>'
# => {"status": "deleted", "id": "..."}
```

## Errors

| Status | When |
|---|---|
| `400` | `portfolio_id` is not a UUID; `initial_capital < 0`; name violates length. |
| `401` | Missing/invalid token. |
| `403` | Portfolio exists but belongs to another user; or legal acceptance incomplete. |
| `404` | Portfolio does not exist. |

## Cascades on delete

`ON DELETE CASCADE` is set on every owned table:

- `positions`
- `orders`
- `installed_strategies`
- `tax_lot_records`
- `webhook_configs` (when scoped to this portfolio)
- `backtest_results` (when scoped to this portfolio)

Audit-only tables (`legal_acceptances`, `webhook_deliveries`) are
`ON DELETE RESTRICT` or reference the user rather than the portfolio
so they survive a portfolio reset.

## Related

- [`engine/db/models.py`](../../engine/db/models.py)
- [Data model](../architecture/data-model.md)
