# Scoring API

Run scoring strategies over a universe and retrieve the snapshot
history. Implementation:
[`engine/api/routes/scoring.py`](../../engine/api/routes/scoring.py),
executor: [`engine/plugins/scoring_executor.py`](../../engine/plugins/scoring_executor.py).

A *scoring strategy* is a strategy plugin whose `compute_scores`
method returns one score per asset in the universe. Today the only
canonical use case is multi-factor ranking; the same machinery is
general enough for any per-asset scoring.

## Endpoint summary

| Method | Path | Auth | Legal | Purpose |
|---|---|---|---|---|
| `POST` | `/api/v1/scoring/{strategy_name}/run`     | JWT/API key | required | Run the strategy over a universe |
| `GET`  | `/api/v1/scoring/{strategy_name}/results` | JWT/API key | required | List historical snapshots |

## Schemas

```python
class ScoringRunRequest(BaseModel):
    universe: list[str]                            # ≥ 1 asset id
    raw_data: dict[str, dict[str, float | None]] = {}
    # raw_data[symbol][factor_name] = value

class ScoringRunResponse(BaseModel):
    strategy_id: str
    scores: list[dict[str, Any]]     # one dict per asset
    excluded_factors: list[str]      # factors the strategy dropped
    universe_size: int
```

The GET endpoint paginates with `limit`, `offset`, `sort_by`,
`sort_order` (default: `created_at desc`).

## Examples

```bash
# Run
curl -X POST http://localhost:8000/api/v1/scoring/momentum/run \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{
        "universe": ["AAPL","MSFT","NVDA","GOOG"],
        "raw_data": {
          "AAPL":  {"momentum_12_1": 0.34, "volatility": 0.22},
          "MSFT":  {"momentum_12_1": 0.41, "volatility": 0.19},
          "NVDA":  {"momentum_12_1": 0.78, "volatility": 0.45},
          "GOOG":  {"momentum_12_1": 0.18, "volatility": 0.27}
        }
      }'

# History
curl 'http://localhost:8000/api/v1/scoring/momentum/results?limit=10' \
  -H 'authorization: Bearer <access>'
```

## Storage

Each run writes a row to `scoring_snapshots`:

| Column            | Type   | Notes |
|-------------------|--------|-------|
| `id`              | UUID   | primary key |
| `strategy_id`     | string | matches `strategy_name` |
| `universe_size`   | int    | length of input universe |
| `excluded_factors`| JSONB  | list of dropped factor names |
| `results`         | JSONB  | full score breakdown |
| `created_at`      | timestamptz | when the run finished |

The index `(strategy_id, created_at)` backs the history query. There
is no retention policy today — operators should add a periodic
cleanup if their snapshot volume is high.

## Errors

| Status | When |
|---|---|
| `400` | Strategy exists but isn't a scoring strategy (`is_scoring_strategy(instance)` is `False`). |
| `401` | Missing/invalid token. |
| `403` | Legal acceptance incomplete. |
| `404` | Unknown `strategy_name`. |

## Related

- [`docs/PLUGIN_DEV_GUIDE.md`](../PLUGIN_DEV_GUIDE.md)
- [Strategies API](strategies.md)
