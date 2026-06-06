# Marketplace API (partial)

Base path: `/api/v1/marketplace`. Source:
[`engine/api/routes/marketplace.py`](../../engine/api/routes/marketplace.py).

> **Status: partial.** Browse and category listing work today; install
> / uninstall / rate are stubs that return `{"status":
> "not_implemented"}`. This router is here so the frontend can wire
> up the UI before the backend lands. See
> [`../limitations.md`](../limitations.md) for the build-out order.

This router is mounted with `require_legal_acceptance`.

## Endpoints

### `GET /api/v1/marketplace/browse`

Browse the strategy catalogue. Today returns an empty list — there is
no remote registry yet.

**Auth**: Bearer JWT or API key.

**Query params**:

| Name       | Type   | Default | Notes                                                |
|------------|--------|---------|------------------------------------------------------|
| `category` | string | null    | Filter to one category.                              |
| `search`   | string | null    | Free-text search.                                    |
| `sort_by`  | string | `downloads` | `downloads`, `rating`, `recent`. (Future.)      |
| `page`     | int    | 1       |                                                      |
| `per_page` | int    | 20      |                                                      |

**Response**: `200 OK`:

```json
{
  "strategies": [],
  "total": 0,
  "page": 1,
  "per_page": 20,
  "filters": { "category": null, "search": null, "sort_by": "downloads" }
}
```

### `GET /api/v1/marketplace/categories`

Static list of strategy categories.

**Response**: `200 OK`:

```json
{
  "categories": [
    { "id": "algorithmic", "name": "Fixed Algorithm", "description": "..." },
    { "id": "ml",          "name": "Machine Learning", "description": "..." },
    { "id": "llm",         "name": "LLM-Powered", "description": "..." },
    { "id": "hybrid",      "name": "Hybrid / Multi-Model", "description": "..." },
    { "id": "income",      "name": "Income / Yield", "description": "..." },
    { "id": "macro",       "name": "Macro / Regime", "description": "..." }
  ]
}
```

### `POST /api/v1/marketplace/install` *(not implemented)*

**Auth**: requires `developer` role. Returns a stub today.

```json
{ "status": "not_implemented", "strategy_id": "...", "message": "Marketplace installation coming soon." }
```

### `DELETE /api/v1/marketplace/uninstall/{strategy_id}` *(not implemented)*

**Auth**: requires `developer` role. Returns a stub today.

```json
{ "status": "not_implemented", "strategy_id": "..." }
```

### `POST /api/v1/marketplace/{strategy_id}/rate` *(not implemented)*

Rate a strategy 1–5 with optional review text.

**Query params**: `rating` (1–5), `review` (string).

Returns `{ "status": "not_implemented" }` today. `400` if `rating` is
outside `[1, 5]`.

## Roadmap

The build-out is tracked in [`../limitations.md`](../limitations.md).
The minimum viable shape is:

1. A hosted package registry (or a pinned Git repo + a manifest
   index).
2. Download → signature verification → install to the local
   `strategies/` directory.
3. Per-user ratings table; expose the average in `/browse`.
4. Permissions: only `developer`+ can install; per-portfolio
   restriction is a follow-up.
