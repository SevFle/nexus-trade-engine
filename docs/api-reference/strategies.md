# Strategies

Discover, configure, and operate installed strategy plugins.
Source: [`engine/api/routes/strategies.py`](../../engine/api/routes/strategies.py).

The strategy plugin model is documented at length in
[Plugin developer guide](../PLUGIN_DEV_GUIDE.md) and
[architecture/plugins](../architecture/plugins.md). This page only
covers the HTTP API.

**Legal gate:** all routes are mounted with
[`require_legal_acceptance`](../../engine/legal/dependencies.py).

## Endpoints

### `GET /api/v1/strategies/`

List every strategy currently visible to the engine — bundled
examples, marketplace-installed, and operator-dropped into the
plugin directory. Returns metadata only.

**Response** `200 OK`:

```json
[
  {
    "id": "mean_reversion_basic",
    "name": "Mean Reversion (basic)",
    "version": "1.0.0",
    "author": "nexus",
    "description": "Buys when price < SMA × 0.95 …",
    "is_active": true,
    "installed_at": "2026-04-15T10:00:00Z"
  }
]
```

### `GET /api/v1/strategies/{strategy_id}`

Return full metadata plus the manifest's `config_schema` (JSON
Schema) and the declared `data_feeds` and `watchlist`.

`404 Not Found` if the strategy is not registered.

### `POST /api/v1/strategies/{strategy_id}/activate`

Mark an installed strategy as `is_active=true` for the calling
user's portfolio (passed in the body).

**Request body:**

```json
{ "portfolio_id": "<uuid>", "config": {} }
```

### `POST /api/v1/strategies/{strategy_id}/deactivate`

Inverse of activate. The strategy remains installed but no longer
evaluates.

**Request body:**

```json
{ "portfolio_id": "<uuid>" }
```

### `POST /api/v1/strategies/{strategy_id}/reload`

Hot-reload a strategy from disk without restarting the engine.
Used in development. The new code runs in the same sandbox as the
old code; if the strategy changed its `id` property the engine
treats it as a new registration and keeps the old one until the
next restart.

### `GET /api/v1/strategies/{strategy_id}/health`

Per-strategy health probe — runs `dispose()` then `initialize()`
in a side sandbox and returns the latency. Used by the dashboard
to spot strategies that crash on cold start.

**Response** `200 OK`:

```json
{
  "strategy_id": "mean_reversion_basic",
  "healthy": true,
  "initialize_ms": 38,
  "dispose_ms": 1,
  "error": null
}
```

## Operational notes

- Strategy lookup is by `strategy_id` (the manifest's `id` field),
  not by file path.
- `config` is opaque JSON at the engine layer; each strategy
  validates its own schema in `initialize()`.
- There is no per-user ACL on strategies — every authenticated
  user can see every installed strategy. Scope restrictions are
  planned post-marketplace launch.
