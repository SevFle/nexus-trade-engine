# Scoring API

Base path: `/api/v1/scoring`. Source:
[`engine/api/routes/scoring.py`](../../engine/api/routes/scoring.py),
[`engine/plugins/scoring_executor.py`](../../engine/plugins/scoring_executor.py).

Run a *scoring* strategy (a strategy that emits a vector of scores
over a universe rather than a single equity curve) and persist the
result. Used by the dashboard's screener page.

This router is mounted with `require_legal_acceptance`.

## Endpoint

### `POST /api/v1/scoring/{strategy_name}/run`

Run a scoring strategy synchronously.

**Auth**: Bearer JWT or API key with `trade`+ scope.

**Path params**: `strategy_name` — must match a registered strategy
that implements `IScoringStrategy` (see
[`sdk/nexus_sdk/scoring.py`](../../sdk/nexus_sdk/scoring.py)). `404`
if the strategy is missing; `400` if it is not a scoring strategy.

**Request body**:

```json
{
  "universe": ["AAPL", "MSFT", "GOOGL"],
  "raw_data": {
    "AAPL": { "momentum_12_1": 0.18, "volatility": 0.21 },
    "MSFT": { "momentum_12_1": 0.22, "volatility": 0.19 }
  }
}
```

| Field       | Type                  | Notes                                            |
|-------------|-----------------------|--------------------------------------------------|
| `universe`  | array of strings      | Non-empty. Symbols to score.                     |
| `raw_data`  | object of per-symbol objects | Factor values for each symbol. Defaults to `{}`. |

**Response**: `200 OK`:

```json
{
  "strategy_id": "quality_momentum",
  "scores": [
    { "symbol": "AAPL", "score": 0.82, "rank": 1 },
    { "symbol": "MSFT", "score": 0.71, "rank": 2 }
  ],
  "excluded_factors": ["volatility"],
  "universe_size": 2
}
```

A row is persisted to `scoring_snapshots` per run.

### `GET /api/v1/scoring/{strategy_name}/results`

List past runs for one strategy.

**Auth**: Bearer JWT or API key with `read`+ scope.

**Query params**:

| Name         | Type   | Default | Notes                                  |
|--------------|--------|---------|----------------------------------------|
| `limit`      | int    | 20      | 1–100.                                 |
| `offset`     | int    | 0       |                                        |
| `sort_by`    | string | `created_at` | Today only `created_at` is honoured. |
| `sort_order` | string | `desc`  | `asc` or `desc`.                       |

**Response**: `200 OK`:

```json
{
  "strategy_id": "quality_momentum",
  "results": [
    {
      "id": "uuid",
      "universe_size": 50,
      "excluded_factors": ["volatility"],
      "scores": [{ "symbol": "AAPL", "score": 0.82 }],
      "created_at": "2026-06-06T12:00:00Z"
    }
  ],
  "count": 1
}
```

## Persistence

Every successful run produces one `scoring_snapshots` row, with the
full result blob in `results` JSONB. The composite index
`ix_scoring_snapshot_strategy_time (strategy_id, created_at)` is the
primary read path.
