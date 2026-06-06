# Marketplace API

Browse, install, and rate community strategies. Implementation:
[`engine/api/routes/marketplace.py`](../../engine/api/routes/marketplace.py).

> **Status:** partial. `GET /browse` and `GET /categories` return
> shapes; `POST /install`, `DELETE /uninstall`, and `POST /{id}/rate`
> return `{"status": "not_implemented"}` and a 200 today. The
> marketplace backend (registry, packaging, signing) is on the roadmap
> — see [`../known-limitations.md`](../known-limitations.md).

## Endpoint summary

| Method | Path | Auth | Purpose | Status |
|---|---|---|---|---|
| `GET`    | `/api/v1/marketplace/browse`             | JWT/API key | Browse + filter | returns empty list |
| `GET`    | `/api/v1/marketplace/categories`         | JWT/API key | Static category list | live |
| `POST`   | `/api/v1/marketplace/install`            | JWT (`developer` role) | Install strategy | stub |
| `DELETE` | `/api/v1/marketplace/uninstall/{strategy_id}` | JWT (`developer` role) | Uninstall | stub |
| `POST`   | `/api/v1/marketplace/{strategy_id}/rate` | JWT/API key | Rate + review | stub |

## Browse query parameters

| Param    | Default | Notes |
|----------|---------|-------|
| `category`   | unset | Filter by category id (see `/categories`) |
| `search`     | unset | Free-text search (TBD backend) |
| `sort_by`    | `downloads` | One of `downloads`, `rating`, `recent` |
| `page`       | 1     | 1-indexed |
| `per_page`   | 20    | Capped server-side |

## Categories

The static set returned by `/categories`:

| id            | Name                  | Description |
|---------------|-----------------------|-------------|
| `algorithmic` | Fixed Algorithm       | Deterministic rule-based strategies |
| `ml`          | Machine Learning      | Neural nets, ensemble models, deep learning |
| `llm`         | LLM-Powered           | Strategies using large language models |
| `hybrid`      | Hybrid / Multi-Model  | Combinations of multiple approaches |
| `income`      | Income / Yield        | Dividend and options income strategies |
| `macro`       | Macro / Regime        | Macro-driven allocation strategies |

These are the canonical categories; new strategies should pick one.
The taxonomy may grow; if you add a category here, also update the
`MarketplaceEntry.category` documentation.

## Schemas

```python
class MarketplaceEntry(BaseModel):
    id: str
    name: str
    version: str
    author: str
    description: str
    category: str
    tags: list[str] = []
    rating: float = 0.0
    downloads: int = 0
    backtest_sharpe: float | None = None
    min_capital: float = 0.0

class InstallRequest(BaseModel):
    strategy_id: str
    version: str = "latest"
```

## Examples

```bash
# Browse
curl 'http://localhost:8000/api/v1/marketplace/browse?category=ml&per_page=10' \
  -H 'authorization: Bearer <access>'

# Categories (works today)
curl http://localhost:8000/api/v1/marketplace/categories \
  -H 'authorization: Bearer <access>'

# Install (stub — returns not_implemented)
curl -X POST http://localhost:8000/api/v1/marketplace/install \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"strategy_id":"mean_reversion_basic","version":"latest"}'
```

## Errors

| Status | When |
|---|---|
| `400` | Rating outside 1-5 on `/{id}/rate`. |
| `401` | Missing/invalid token. |
| `403` | Legal acceptance incomplete; or `/install` / `/uninstall` without `developer` role. |

## Roadmap

- Real registry backend (S3 + sigstore signing) — see roadmap.
- Async install pipeline that downloads + validates + sandbox-tests
  before exposing the strategy through `/strategies`.
- Per-user rating history + deduplication.
