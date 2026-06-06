# Backtest API

Submit and poll backtest runs. Implementation:
[`engine/api/routes/backtest.py`](../../engine/api/routes/backtest.py).

The backtest surface is asynchronous: `POST /run` enqueues a background
task (via FastAPI `BackgroundTasks`, **not** TaskIQ today) and returns
an id immediately. Poll `GET /results/{id}` until `status=completed`
or `status=failed`. Results are kept in-process for 1 hour after the
run finishes; an eviction sweep runs at every poll.

> **Note — execution model.** Despite the TaskIQ worker being plumbed
> in `engine/tasks/` and `docker-compose.yml`, the backtest route does
> not actually enqueue onto the broker yet — it uses FastAPI's
> in-process `BackgroundTasks`. This is a known limitation; see
> [`../known-limitations.md`](../known-limitations.md).

## Endpoint summary

| Method | Path | Auth | Legal acceptance | Purpose |
|---|---|---|---|---|
| `POST` | `/api/v1/backtest/run` | JWT or API key | required | Enqueue a backtest |
| `GET`  | `/api/v1/backtest/results/{backtest_id}` | JWT or API key | required | Poll for results |

## Schemas

```python
class BacktestRequest(BaseModel):
    strategy_name: str
    symbol: str
    start_date: str           # ISO-8601 (YYYY-MM-DD)
    end_date: str
    initial_capital: float = 100_000.0
    config: dict | None = None  # forward to strategy

class BacktestResponse(BaseModel):
    status: str               # "accepted"
    backtest_id: str | None

class BacktestResultResponse(BaseModel):
    status: str               # "running" | "completed" | "failed" | "not_found" | "forbidden"
    strategy_name: str
    symbol: str
    initial_capital: float
    final_value: float
    metrics: MetricsSummary
    equity_curve: list[dict]
    drawdown_curve: list[float]
    error: str | None = None
    evaluation: dict | None = None

class MetricsSummary(BaseModel):
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float | None
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    max_drawdown_recovery_days: int | None
    calmar_ratio: float | None
    volatility_annual_pct: float
    total_trades: int
    win_rate: float
    profit_factor: float | None
    avg_trade_pnl: float
    avg_winner: float
    avg_loser: float
    best_trade: float
    worst_trade: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    total_costs: float
    total_taxes: float
    cost_drag_pct: float
    turnover_ratio: float
    exposure_pct: float
    rolling_metrics: list[RollingMetricsSnapshot] = []
```

`MetricsSummary` mirrors what `engine.core.metrics` plus
`engine.core.cost_model` produce for the run. Every metric is computed
*net of cost* — that is the engine's central design choice (see
[`README.md`](../../README.md)).

## Examples

```bash
# Enqueue
RES=$(curl -sS -X POST http://localhost:8000/api/v1/backtest/run \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{
        "strategy_name": "mean_reversion_basic",
        "symbol": "AAPL",
        "start_date": "2023-01-01",
        "end_date": "2024-01-01",
        "initial_capital": 100000
      }')
echo "$RES"
# {"status": "accepted", "backtest_id": "..."}

# Poll until done
BID=$(echo "$RES" | jq -r .backtest_id)
curl -sS "http://localhost:8000/api/v1/backtest/results/$BID" \
  -H 'authorization: Bearer <access>' | jq '.status, .metrics.sharpe_ratio'
```

## Lifecycle

```
accepted ──▶ running ──▶ completed
                     └──▶ failed
```

- **accepted** — `POST /run` returned. `status="accepted"` on the
  response. Background task may not have started yet.
- **running** — handler started but not finished. `GET /results/{id}`
  returns `202 Accepted` with `status="running"`.
- **completed** — handler finished successfully. `GET /results/{id}`
  returns `200 OK` with full `MetricsSummary`.
- **failed** — handler raised. `error` and `error_type` are populated
  in the in-memory record; `GET` returns `200 OK` with
  `status="failed"` (the result is observable, the run is over).
- **not_found** — id unknown or evicted (>1 h after the run finished).
  `GET` returns `404`.
- **forbidden** — caller is not the user that submitted the run. `GET`
  returns `403`.

## Errors

| Status | When |
|---|---|
| `400` | Bad request body (Pydantic validation). |
| `401` | Missing/invalid token. |
| `403` | Caller has not accepted required legal documents, or trying to read another user's result. |
| `404` | `backtest_id` not in the in-memory map (never existed, or evicted). |
| `500` | Strategy not found by the registry, or any unhandled exception in the background task (also reflected in `status="failed"` on subsequent polls). |

## Cost-model hook

`BacktestRunner.run()` instantiates the strategy with the engine's
default `ICostModel` (commission + spread + slippage + tax + market
impact). The strategy receives that cost model as part of its
`evaluate()` call so it can pre-filter signals that would be
uneconomic after costs. See
[`engine/core/cost_model.py`](../../engine/core/cost_model.py).

## See also

- [Strategies API](strategies.md) — list/activate/reload strategy
  plugins.
- [Architecture — plugins](../architecture/plugins.md).
