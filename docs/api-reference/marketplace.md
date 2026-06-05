# Marketplace

Browse, install, and rate strategies from the marketplace.
Source: [`engine/api/routes/marketplace.py`](../../engine/api/routes/marketplace.py).

**Legal gate:** all routes are mounted with
[`require_legal_acceptance`](../../engine/legal/dependencies.py).

The marketplace is **partially implemented**: browse + install
work against the in-repo catalog; ratings are stored locally
rather than federated; payment / revenue share is not yet wired
(`NEXUS_PLATFORM_FEE_PERCENT` exists for the contract but no
clearing partner is integrated).

## Endpoints

### `GET /api/v1/marketplace/browse`

Paginated catalog listing.

**Query params:**

| Param       | Type        | Default | Notes                                   |
|-------------|-------------|---------|-----------------------------------------|
| `category`  | string      | —       | Filter: `algorithmic`, `ml`, `llm`, …   |
| `tags`      | csv string  | —       | Comma-separated tag list. AND-matched.  |
| `min_capital` | integer   | 0       | Filter strategies whose `min_capital` ≤ this value. |
| `page`      | integer     | 1       | 1-indexed.                              |
| `page_size` | integer     | 20      | Capped at 100.                          |

**Response** `200 OK`:

```json
{
  "items": [
    {
      "strategy_id": "quality_momentum",
      "name": "Quality Momentum",
      "author": "nexus",
      "category": "algorithmic",
      "tags": ["momentum", "large-cap"],
      "min_capital": 25000,
      "rating_average": 4.6,
      "rating_count": 28,
      "installed": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 47
}
```

`installed` is computed per-caller — true if the calling user has
the strategy active in any portfolio.

### `GET /api/v1/marketplace/categories`

Return the distinct `category` values currently in the catalog.

### `POST /api/v1/marketplace/install`

Install a marketplace strategy into the caller's namespace.

**Request body:**

```json
{
  "strategy_id": "quality_momentum",
  "portfolio_id": "<uuid>",
  "config": {}
}
```

`409 Conflict` if already installed for that portfolio.

### `DELETE /api/v1/marketplace/uninstall/{strategy_id}`

Remove the strategy from the caller's namespace.

**Query:** `portfolio_id` (required).

### `POST /api/v1/marketplace/{strategy_id}/rate`

Rate a strategy 1–5 stars. One rating per user per strategy;
re-rating overwrites.

**Request body:**

```json
{ "rating": 5, "comment": "Works exactly as advertised." }
```

**Response** `200 OK` — echoes the stored rating with timestamps.
