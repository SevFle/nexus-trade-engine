# Strategies API

List, activate, deactivate, hot-reload, and inspect runtime health of
strategy plugins. Implementation:
[`engine/api/routes/strategies.py`](../../engine/api/routes/strategies.py),
[`engine/plugins/registry.py`](../../engine/plugins/registry.py).

A *strategy* is a plugin that implements the engine's strategy
interface (see [`docs/architecture/plugins.md`](../architecture/plugins.md)
and [`docs/PLUGIN_DEV_GUIDE.md`](../PLUGIN_DEV_GUIDE.md)). The
registry discovers strategies at startup from
`NEXUS_PLUGIN_DIR` (default `./strategies`) and from
`engine/plugins/`.

## Endpoint summary

| Method | Path | Auth | Legal | Purpose |
|---|---|---|---|---|
| `GET`  | `/api/v1/strategies/`                  | JWT/API key | required | List installed strategies + load state |
| `GET`  | `/api/v1/strategies/{strategy_id}`     | JWT/API key | required | Detail (manifest fields, capabilities) |
| `POST` | `/api/v1/strategies/{strategy_id}/activate`   | JWT/API key | required | Instantiate with config |
| `POST` | `/api/v1/strategies/{strategy_id}/deactivate` | JWT/API key | required | Unload instance |
| `POST` | `/api/v1/strategies/{strategy_id}/reload`     | JWT/API key | required | Hot-reload code from disk |
| `GET`  | `/api/v1/strategies/{strategy_id}/health`     | JWT/API key | required | Runtime health (today: `is_loaded` only) |

## Schemas

```python
class StrategyConfigRequest(BaseModel):
    params: dict = {}      # forward to strategy's __init__ / configure
```

Responses are not Pydantic-typed (the route returns plain dicts) but
the shape is stable:

```json
// GET /api/v1/strategies/
{ "strategies": [
    {"id": "mean_reversion_basic",
     "name": "Mean Reversion Basic",
     "version": "0.1.0",
     "is_loaded": true,
     "kind": "strategy"}
] }

// GET /api/v1/strategies/{id}
{ "id": "mean_reversion_basic",
  "name": "Mean Reversion Basic",
  "version": "0.1.0",
  "author": "...",
  "description": "...",
  "config_schema": {...},          // JSON Schema for the params dict
  "data_feeds": ["ohlcv"],
  "watchlist": ["AAPL", "MSFT"],
  "requires_network": false,
  "requires_gpu": false,
  "is_loaded": true }

// POST /api/v1/strategies/{id}/activate
{ "status": "activated",
  "strategy_id": "mean_reversion_basic",
  "name": "Mean Reversion Basic",
  "version": "0.1.0" }
```

## Examples

```bash
# List
curl http://localhost:8000/api/v1/strategies/ \
  -H 'authorization: Bearer <access>'

# Detail
curl http://localhost:8000/api/v1/strategies/mean_reversion_basic \
  -H 'authorization: Bearer <access>'

# Activate with custom params
curl -X POST http://localhost:8000/api/v1/strategies/mean_reversion_basic/activate \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"params":{"lookback":20,"threshold":2.0}}'

# Hot-reload after editing the strategy file
curl -X POST http://localhost:8000/api/v1/strategies/mean_reversion_basic/reload \
  -H 'authorization: Bearer <access>'
```

## Reload semantics

`reload` re-imports the strategy module from disk and rebuilds the
registry entry. **Active instances are not preserved** — call
`/deactivate` then `/activate` if you need to retain state. The
operation is best-effort and returns `500 Reload failed` if the new
module raises on import.

## Health

The `/health` endpoint today returns only `{"strategy_id": "...",
"is_loaded": true|false}`. The richer sandbox metrics (memory,
CPU, error counts) live in `engine/plugins/sandbox.py` but are not
exposed via this route yet — see [`../known-limitations.md`](../known-limitations.md).

## Errors

| Status | When |
|---|---|
| `400` | Activate failed (e.g. invalid params for the strategy's schema). |
| `401` | Missing/invalid token. |
| `403` | Legal acceptance incomplete. |
| `404` | Unknown `strategy_id`, or `/health` on a strategy that isn't loaded. |
| `500` | `/reload` failed; or activate raised. |

## Related

- [`docs/PLUGIN_DEV_GUIDE.md`](../PLUGIN_DEV_GUIDE.md) — how to
  author a strategy.
- [`docs/architecture/plugins.md`](../architecture/plugins.md) —
  discovery, sandboxing, registry internals.
- [Scoring API](scoring.md) — for strategies that are scoring
  strategies (sub-type).
