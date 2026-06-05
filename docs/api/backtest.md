# Backtest API

Mounted at `/api/v1/backtest`. Implementation:
`engine/api/routes/backtest.py`. Wrapped in
`Depends(require_legal_acceptance)`.

Submit a backtest as an asynchronous job and poll for completion.
Results are kept in an **in-process dict** keyed by `backtest_id` with
a 1-hour TTL — restart loses them and they are not shared between
replicas. The persistent `BacktestResult` row in Postgres is a
separate write path that fires from the same job.

## POST /run

Enqueue a backtest.

**Auth** — required. Legal acceptance required.

**Request body** `BacktestRequest`:
```json
{
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "start_date": "2020-01-01",
  "end_date": "2024-12-31",
  "initial_capital": 100000.0,
  "config": {}
}
```

| Field             | Type   | Default       | Notes                                      |
|-------------------|--------|---------------|--------------------------------------------|
| `strategy_name`   | string | required      | Must exist under `strategies/`             |
| `symbol`          | string | required      | Resolved via the data provider             |
| `start_date`      | string | required      | ISO date; range applied to provider data   |
| `end_date`        | string | required      | ISO date                                   |
| `initial_capital` | number | `100000.0`    |                                            |
| `config`          | object | `null`        | Forwarded to the strategy (currently unused)|

**Response** `BacktestResponse` (202):
```json
{ "status": "accepted", "backtest_id": "uuid" }
```

The route immediately returns `202 Accepted` and the actual work runs
in a FastAPI `BackgroundTasks` handler inside the engine process.
The persistent TaskIQ path
(`engine.tasks.worker.run_backtest_task`) is wired but not the
primary path; see [`limitations.md`](../limitations.md).

## GET /results/{backtest_id}

Poll for the result. Repeat until `status != "running"`.

**Auth** — required. The result is owned by the user who submitted
the backtest; other users get `403`.

**Response** `BacktestResultResponse`:

```json
{
  "status": "completed | running | failed | not_found | forbidden",
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "initial_capital": 100000.0,
  "final_value": 142356.78,
  "metrics": { /* see MetricsSummary */ },
  "equity_curve": [ { "timestamp": "...", "value": 100120.0 }, ... ],
  "drawdown_curve": [ 0.0, -0.012, -0.035, ... ],
  "error": null,
  "evaluation": { /* strategy evaluator score breakdown */ }
}
```

| HTTP status | `status`    | Meaning                                              |
|-------------|-------------|------------------------------------------------------|
| 200         | `completed` | Done, metrics and equity curve populated             |
| 200         | `failed`    | Exception in the background task; `error` is set     |
| 202         | `running`   | Still in flight                                      |
| 403         | `forbidden` | Caller doesn't own the backtest                      |
| 404         | `not_found` | Unknown id or result TTL'd out (1h)                  |

### MetricsSummary shape

| Field                          | Type             |
|--------------------------------|------------------|
| `total_return_pct`             | float            |
| `annualized_return_pct`        | float            |
| `sharpe_ratio`                 | float            |
| `sortino_ratio`                | float \| null    |
| `max_drawdown_pct`             | float            |
| `max_drawdown_duration_days`   | int              |
| `max_drawdown_recovery_days`   | int \| null      |
| `calmar_ratio`                 | float \| null    |
| `volatility_annual_pct`        | float            |
| `total_trades`                 | int              |
| `win_rate`                     | float            |
| `profit_factor`                | float \| null    |
| `avg_trade_pnl`                | float            |
| `avg_winner`                   | float            |
| `avg_loser`                    | float            |
| `best_trade`                   | float            |
| `worst_trade`                  | float            |
| `max_consecutive_wins`         | int              |
| `max_consecutive_losses`       | int              |
| `total_costs`                  | float            |
| `total_taxes`                  | float            |
| `cost_drag_pct`                | float            |
| `turnover_ratio`               | float            |
| `exposure_pct`                 | float            |
| `rolling_metrics`              | RollingMetricsSnapshot[] |

`RollingMetricsSnapshot` is `{window_days, sharpe_ratio, sortino_ratio,
volatility_annual_pct, max_drawdown_pct}`.

## Background task internals

`_run_backtest_background` does:

1. Mark the result slot `"running"`.
2. Load the strategy via `PluginRegistry.load_strategy` (manifest
   discovery in `strategies/*/manifest.yaml`).
3. Resolve the data provider (currently hardcoded to Yahoo — see
   `engine/api/routes/backtest.py:147`).
4. Construct a `BacktestRunner` and call `runner.run()`.
5. Store `{status: "completed", final_capital, metrics, equity_curve,
   trades}`.
6. Run `_evict_expired_results` to evict entries older than 3600s.

On exception, the slot is marked `"failed"` with `error` and
`error_type`. The exception traceback is logged via structlog but not
returned to the client.

## Concurrency and capacity

- A backtest holds a single uvicorn worker for its full duration.
- The in-process result dict is not bounded; only the TTL evicts.
- Concurrent backtests share the dict; collision is impossible
  (UUID4 keys).
- No queue. Submitting 100 backtests concurrently runs ~100 tasks in
  the engine process — operators are expected to enforce limits at
  the reverse proxy or via rate-limit overrides.

## Persistence

The async path writes a `BacktestResult` row to Postgres via the
TaskIQ worker. The synchronous path (default today) does **not**.
Until the cutover completes, treat the dict as the only source of
truth. See [`limitations.md`](../limitations.md).
