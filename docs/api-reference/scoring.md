# Strategy scoring

Cross-strategy composite scoring. Source:
[`engine/api/routes/scoring.py`](../../engine/api/routes/scoring.py),
[`engine/plugins/scoring_executor.py`](../../engine/plugins/scoring_executor.py).

The strategy evaluator in
[`engine/core/strategy_evaluator.py`](../../engine/core/strategy_evaluator.py)
assigns a per-strategy composite score from a backtest metrics
report. This API runs the same evaluator across a user's strategy
catalog and persists snapshots for trend analysis.

**Legal gate:** all routes are mounted with
[`require_legal_acceptance`](../../engine/legal/dependencies.py).

## Endpoints

### `POST /api/v1/scoring/{strategy_name}/run`

Run the evaluator over a strategy's recent results and persist a
`scoring_snapshots` row.

**Path:** `strategy_name` — strategy id.

**Request body** — `ScoringRunRequest`:

```json
{
  "universe": ["AAPL", "MSFT", "GOOGL"],
  "excluded_factors": ["market_timing"],
  "as_of": "2024-12-31"
}
```

`as_of` is optional; defaults to today (UTC).

**Response** `200 OK` — `ScoringRunResponse`:

```json
{
  "snapshot_id": "<uuid>",
  "strategy_name": "mean_reversion_basic",
  "composite_score": 0.78,
  "score_breakdown": {
    "return": 0.85,
    "risk_adjusted": 0.71,
    "drawdown_control": 0.68,
    "cost_efficiency": 0.80,
    "consistency": 0.74
  },
  "universe_size": 3,
  "excluded_factors": ["market_timing"],
  "created_at": "2026-06-05T12:00:00Z"
}
```

### `GET /api/v1/scoring/{strategy_name}/results`

List historical snapshots for the strategy.

**Query params:**

| Param  | Type    | Default | Notes                              |
|--------|---------|---------|------------------------------------|
| `limit` | integer | 50      | Capped at 200.                     |

**Response** `200 OK` — `ScoringRunResponse[]`, newest first.

## How scores are computed

The evaluator blends six factors (defined as
`EvaluationDimension` in
[`engine/core/strategy_evaluator.py:52`](../../engine/core/strategy_evaluator.py:52))
via weighted average. Default weights:

```python
RETURN            = 0.25
RISK_ADJUSTED     = 0.20  # Sharpe + Sortino blend
DRAWDOWN_CONTROL  = 0.20
COST_EFFICIENCY   = 0.15  # Inverse of cost drag
CONSISTENCY       = 0.15  # Rolling Sharpe variance
MARKET_TIMING     = 0.05  # Optional, can be excluded
```

Each factor is normalized to `[0, 1]` against the strategy
universe before blending. Override weights in the strategy's
manifest (`scoring.weights` block).

## Excluded factors

Some strategies are designed to time the market (factor `market_timing`
would unfairly penalize long-only strategies during bull runs). Pass
`excluded_factors` in the request body to drop a factor from the
blend; the engine renormalizes the remaining weights.
