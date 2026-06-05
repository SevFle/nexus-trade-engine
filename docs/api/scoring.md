# Scoring API

Mounted at `/api/v1/scoring`. Implementation:
`engine/api/routes/scoring.py`. Wrapped in
`Depends(require_legal_acceptance)`.

A *scoring strategy* is a plugin that implements
`nexus_sdk.scoring.IScoringStrategy` instead of the trading
`IStrategy`. It takes a universe of symbols + raw factor data and
returns per-symbol composite scores. Snapshots are persisted to
`scoring_snapshots` for historical comparison.

## POST /{strategy_name}/run

Compute scores for a universe.

**Auth** — required. Legal acceptance required.

**Path** — `strategy_name` (must exist in `strategies/` and implement
`IScoringStrategy`).

**Request body** `ScoringRunRequest`:
```json
{
  "universe": ["AAPL", "MSFT", "GOOGL", "NVDA"],
  "raw_data": {
    "AAPL":  { "momentum": 0.12, "value": 0.04, "quality": 0.81 },
    "MSFT":  { "momentum": 0.08, "value": 0.02, "quality": 0.92 },
    "GOOGL": { "momentum": null, "value": 0.05, "quality": 0.78 }
  }
}
```

`raw_data` is the factor matrix; missing or null entries are passed
through to the strategy, which is responsible for imputation /
exclusion.

**Response** `ScoringRunResponse`:
```json
{
  "strategy_id": "quality_momentum",
  "scores": [
    { "symbol": "NVDA", "score": 0.91, "rank": 1, "factors": {...} },
    { "symbol": "MSFT", "score": 0.84, "rank": 2, "factors": {...} }
  ],
  "excluded_factors": ["value"],
  "universe_size": 4
}
```

`excluded_factors` is the list of input factors the strategy ignored
(e.g. due to too many nulls or near-zero variance). This makes
subsequent re-runs reproducible.

**Errors**
- `400 Bad Request` — `{"detail": "Strategy 'X' is not a scoring strategy"}`.
- `404 Not Found` — strategy not registered.

A `ScoringSnapshot` row is committed in the same transaction as the
response. The persisted shape includes the full score list and
excluded factors in a JSONB `results` column.

## GET /{strategy_name}/results

Page through historical scoring runs for a strategy.

**Query params**

| Param        | Type   | Default | Notes                       |
|--------------|--------|---------|-----------------------------|
| `limit`      | int    | 20      | 1–100                       |
| `offset`     | int    | 0       | `>= 0`                      |
| `sort_by`    | string | `created_at` | Only `created_at` honored |
| `sort_order` | string | `desc`  | `desc` or `asc`             |

**Response**:
```json
{
  "strategy_id": "quality_momentum",
  "results": [
    {
      "id": "uuid",
      "universe_size": 4,
      "excluded_factors": ["value"],
      "scores": [ { "symbol": "NVDA", "score": 0.91, ... } ],
      "created_at": "2026-06-05T12:00:00Z"
    }
  ],
  "count": 1
}
```

Snapshots are returned newest-first by default. There is no
delete endpoint; operators purge old snapshots via direct DB access.

## What a scoring strategy looks like

```python
from nexus_sdk.scoring import IScoringStrategy, ScoringResult, Score

class Strategy(IScoringStrategy):
    @property
    def id(self) -> str: return "quality_momentum"
    @property
    def name(self) -> str: return "Quality + Momentum"
    @property
    def version(self) -> str: return "0.1.0"

    def compute_scores(self, universe, raw_data):
        scores = []
        for sym in universe:
            f = raw_data.get(sym, {})
            score = 0.5 * (f.get("quality") or 0) + 0.5 * (f.get("momentum") or 0)
            scores.append(Score(symbol=sym, score=score))
        return ScoringResult(
            strategy_id=self.id,
            scores=scores,
            excluded_factors=[],
        )
```

See `engine/plugins/scoring_executor.py:ScoringExecutor` for the
runtime, and `sdk/nexus_sdk/scoring.py` for the interface.
