# Backtest

Submit and inspect backtest runs. Source:
[`engine/api/routes/backtest.py`](../../engine/api/routes/backtest.py).

The backtest pipeline is **asynchronous**: `POST /run` enqueues a
background task and returns immediately with a `backtest_id`. The
client polls `GET /results/{backtest_id}` until the status is
`completed` or `failed`.

## Endpoints

### `POST /api/v1/backtest/run`

**Auth:** JWT (legal-acceptance required).

**Request body** — `BacktestRequest`:

```json
{
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "start_date": "2022-01-01",
  "end_date": "2024-12-31",
  "initial_capital": 100000.0,
  "config": {}
}
```

`config` is an opaque dict forwarded to the strategy's
`initialize()` call. Strategy-specific schema lives in the
strategy manifest; the engine validates only that the JSON is
syntactically valid here.

**Response** `200 OK` — `BacktestResponse`:

```json
{ "status": "accepted", "backtest_id": "uuid" }
```

The handler delegates to `BackgroundTasks` (FastAPI's in-process
runner). For long-running backtests in production, route through
the TaskIQ worker instead — see
[`engine/tasks/worker.py`](../../engine/tasks/worker.py).

### `GET /api/v1/backtest/results/{backtest_id}`

**Auth:** JWT. Only the user that submitted the backtest may read
the result.

**Response** — varies by status:

| Status       | HTTP | Body shape                                                            |
|--------------|------|-----------------------------------------------------------------------|
| `running`    | 202  | `BacktestResultResponse` with empty `metrics`, `equity_curve`.        |
| `completed`  | 200  | `BacktestResultResponse` with full metrics + curves.                  |
| `failed`     | 200  | `BacktestResultResponse` with `error` populated.                      |
| `not_found`  | 404  | `BacktestResultResponse` with `error="Backtest <id> not found"`.      |
| `forbidden`  | 403  | `BacktestResultResponse` with `error="Access denied"`.                |

The `metrics` field on a completed result is `MetricsSummary`:

```json
{
  "total_return_pct": 24.7,
  "annualized_return_pct": 11.6,
  "sharpe_ratio": 1.42,
  "sortino_ratio": 1.78,
  "max_drawdown_pct": -8.4,
  "max_drawdown_duration_days": 67,
  "max_drawdown_recovery_days": 41,
  "calmar_ratio": 1.38,
  "volatility_annual_pct": 14.2,
  "total_trades": 87,
  "win_rate": 0.55,
  "profit_factor": 1.62,
  "avg_trade_pnl": 284.10,
  "avg_winner": 940.0,
  "avg_loser": -410.0,
  "best_trade": 3200.0,
  "worst_trade": -1800.0,
  "max_consecutive_wins": 6,
  "max_consecutive_losses": 4,
  "total_costs": 1245.30,
  "total_taxes": 820.50,
  "cost_drag_pct": 1.25,
  "turnover_ratio": 4.2,
  "exposure_pct": 78.5,
  "rolling_metrics": [
    { "window_days": 30, "sharpe_ratio": 1.2, "sortino_ratio": 1.5,
      "volatility_annual_pct": 13.0, "max_drawdown_pct": -2.1 }
  ]
}
```

`evaluation` (optional) carries the strategy evaluator's composite
score and per-dimension breakdown; see
[`engine/core/strategy_evaluator.py`](../../engine/core/strategy_evaluator.py).

## Result lifecycle & TTL

Results are stored in an in-process dict keyed by `backtest_id`
(see `_backtest_results` in
[`backtest.py:22`](../../engine/api/routes/backtest.py:22)). Entries
are evicted 1 hour after they were written. **Restarting the
process loses in-flight and completed results that have not been
persisted elsewhere.** The roadmap item is to move these into
Postgres (`backtest_results` table already exists for the
evaluator's outputs but is not currently written by `/run`).

## What happens inside the worker

The background task in
[`_run_backtest_background`](../../engine/api/routes/backtest.py:130):

1. Resolves the strategy via
   [`PluginRegistry.load_strategy`](../../engine/plugins/registry.py).
2. Resolves the data provider via
   [`engine.data.feeds.get_data_provider`](../../engine/data/feeds.py)
   (currently hardcoded to `yahoo`).
3. Constructs a `BacktestRunner` from
   [`engine/core/backtest_runner.py`](../../engine/core/backtest_runner.py).
4. Runs `await runner.run()` and stores the result.
5. The runner emits signals through the strategy, simulates fills
   with the configured `ICostModel`, books tax lots via
   `engine/core/tax/`, and computes the metrics report.

Errors are caught and stored as `"failed"` with `error` and
`error_type`; the worker does not crash.

## Limits

- Only one symbol per backtest in the v1 request schema. The
  `Strategy.evaluate()` interface itself supports a watchlist, but
  the API surface does not expose multi-symbol backtests yet.
- `config` is unvalidated at the engine level. Strategy authors
  are responsible for declaring and validating their own schema.
- No pagination on `equity_curve`. A 10-year daily backtest returns
  ~2 500 points; that's fine. Sub-minute backtests will need
  pagination before they're useful.
