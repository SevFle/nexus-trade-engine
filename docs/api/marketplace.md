# Marketplace API

Mounted at `/api/v1/marketplace`. Implementation:
`engine/api/routes/marketplace.py`. Wrapped in
`Depends(require_legal_acceptance)`.

> **Status:** partial. Browse and category listing work end-to-end.
> Install / uninstall / rate return `{"status": "not_implemented"}`.
> See [`limitations.md`](../limitations.md).

## GET /browse

List marketplace entries.

**Auth** — required.

**Query params**

| Param      | Type   | Default       | Notes                                  |
|------------|--------|---------------|----------------------------------------|
| `category` | string | null          | Filter by category id (see /categories)|
| `search`   | string | null          | Full-text search (not yet implemented) |
| `sort_by`  | string | `"downloads"` | Sort key                               |
| `page`     | int    | 1             | 1-indexed                              |
| `per_page` | int    | 20            |                                        |

**Response**:
```json
{
  "strategies": [],
  "total": 0,
  "page": 1,
  "per_page": 20,
  "filters": { "category": null, "search": null, "sort_by": "downloads" }
}
```

The empty `strategies` array is honest: the marketplace registry
backend is not yet wired. This is the supported shape for clients to
build against.

## GET /categories

Returns the fixed category taxonomy.

**Response**:
```json
{
  "categories": [
    { "id": "algorithmic", "name": "Fixed Algorithm",
      "description": "Deterministic rule-based strategies" },
    { "id": "ml", "name": "Machine Learning", "description": "..." },
    { "id": "llm", "name": "LLM-Powered", "description": "..." },
    { "id": "hybrid", "name": "Hybrid / Multi-Model", "description": "..." },
    { "id": "income", "name": "Income / Yield", "description": "..." },
    { "id": "macro", "name": "Macro / Regime", "description": "..." }
  ]
}
```

## POST /install

**Auth** — requires `developer` role.

**Request body** `InstallRequest`:
```json
{ "strategy_id": "mean_reversion_pro", "version": "latest" }
```

**Response** — `{"status": "not_implemented", ...}`.

The handler is in place to enforce role gating; the actual download,
manifest validation, and install-to-plugin-dir logic is the
follow-up tracked in the roadmap.

## DELETE /uninstall/{strategy_id}

**Auth** — requires `developer` role.

**Response** — `{"status": "not_implemented", "strategy_id": "..."}`.

## POST /{strategy_id}/rate

**Auth** — required.

**Body** — `rating` (int, 1–5), `review` (string, optional).

**Response** — `{"status": "not_implemented"}`. `400` if rating is
outside 1–5.
