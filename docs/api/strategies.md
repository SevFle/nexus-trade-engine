# Strategies API

Base path: `/api/v1/strategies`. Source:
[`engine/api/routes/strategies.py`](../../engine/api/routes/strategies.py).

A *strategy* is a discoverable plugin under `strategies/<name>/` with
a `manifest.yaml` and a `strategy.py` defining a `Strategy` class.
The full plugin format is documented in
[`docs/architecture/plugins.md`](../architecture/plugins.md) and the
[strategy author guide](../PLUGIN_DEV_GUIDE.md).

Every endpoint below is mounted with `require_legal_acceptance`.

## Endpoints

### `GET /api/v1/strategies/`

List every discovered strategy plugin.

**Auth**: Bearer JWT or API key.

**Response**: `200 OK`:

```json
{
  "strategies": [
    {
      "id": "mean_reversion_basic",
      "name": "Mean Reversion Basic",
      "version": "0.1.0",
      "is_loaded": true,
      "is_active": false
    }
  ]
}
```

### `GET /api/v1/strategies/{strategy_id}`

Describe one strategy.

**Path params**: `strategy_id` — matches `manifest.yaml#name`.

**Response**: `200 OK`:

```json
{
  "id": "mean_reversion_basic",
  "name": "Mean Reversion Basic",
  "version": "0.1.0",
  "author": "nexus-team",
  "description": "Simple mean-reversion strategy using Bollinger Bands.",
  "config_schema": { "type": "object", "properties": {} },
  "data_feeds": ["ohlcv"],
  "watchlist": ["AAPL", "MSFT", "GOOGL"],
  "requires_network": false,
  "requires_gpu": false,
  "is_loaded": true
}
```

`404` if the strategy is not registered.

### `POST /api/v1/strategies/{strategy_id}/activate`

Instantiate the strategy with operator-supplied params and mark it
active. The strategy's `initialize` is awaited here; an exception is
returned as `500`.

**Request body**:

```json
{ "params": { "window": 20, "num_std": 2.0 } }
```

**Response**: `200 OK`:

```json
{
  "status": "activated",
  "strategy_id": "mean_reversion_basic",
  "name": "Mean Reversion Basic",
  "version": "0.1.0"
}
```

### `POST /api/v1/strategies/{strategy_id}/deactivate`

Unload a strategy. Safe to call on an already-unloaded strategy.

**Response**: `200 OK`:

```json
{ "status": "deactivated", "strategy_id": "mean_reversion_basic" }
```

### `POST /api/v1/strategies/{strategy_id}/reload`

Hot-reload the strategy module from disk without bouncing the worker
process. Used by the dev compose stack and operators iterating on a
plugin.

**Response**: `200 OK`:

```json
{ "status": "reloaded", "strategy_id": "mean_reversion_basic" }
```

`500` if the new code failed to import or instantiate.

### `GET /api/v1/strategies/{strategy_id}/health`

Lightweight liveness probe for an active strategy instance. Currently
returns `is_loaded`; sandbox metrics will be added here when the
sandbox runtime lands.

**Response**: `200 OK`:

```json
{ "strategy_id": "mean_reversion_basic", "is_loaded": true }
```

`404` if the strategy is not active.

## Manifest fields surfaced

The route exposes a subset of `StrategyManifest`:

| Field              | Source                                  |
|--------------------|-----------------------------------------|
| `id`, `name`, `version`, `author`, `description` | Identity.        |
| `config_schema`    | JSON Schema for operator-supplied params. |
| `data_feeds`       | Required feeds (default `["ohlcv"]`).   |
| `watchlist`        | Default symbols; empty = operator picks. |
| `requires_network` | True iff `network.allowed_endpoints` is non-empty. |
| `requires_gpu`     | True iff `resources.gpu == "required"`. |
| `is_loaded`        | Runtime flag from the registry.         |

The full manifest schema lives in
[`engine/plugins/manifest.py`](../../engine/plugins/manifest.py).
