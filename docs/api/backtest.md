# Backtest API

Base path: `/api/v1/backtest`. Source:
[`engine/api/routes/backtest.py`](../../engine/api/routes/backtest.py),
[`engine/core/backtest_runner.py`](../../engine/core/backtest_runner.py).

Submit a backtest run and poll for its results. Runs are
asynchronous: the route accepts the request, generates a `backtest_id`,
and returns `202` immediately. The actual computation runs in a
background task and the result is cached in-process for
`_RESULTS_TTL_SECONDS` (1 hour).

> **Note**: the in-process cache is a known limitation — see
> [`../limitations.md`](../limitations.md). A multi-instance deploy
> must move results into Valkey or persist them to `backtest_results`
> synchronously. Until then, route all backtest traffic through one
> engine instance.

This router is mounted with `require_legal_acceptance`.

## Endpoints

### `POST /api/v1/backtest/run`

Submit a backtest.

**Auth**: Bearer JWT or API key with `trade`+ scope.

**Request body**:

```json
{
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "start_date": "2023-01-01",
  "end_date": "2024-01-01",
  "initial_capital": 100000.0,
  "config": { "window": 20 }
}
```

| Field             | Type   | Default     | Notes                              |
|-------------------|--------|-------------|------------------------------------|
| `strategy_name`   | string | required    | Must exist in the plugin registry. |
| `symbol`          | string | required    | Resolved via the configured data provider. |
| `start_date`      | string | required    | ISO date.                          |
| `end_date`        | string | required    | ISO date.                          |
| `initial_capital` | number | `100_000`   |                                    |
| `config`          | object | null        | Strategy params, validated against the manifest's `config_schema`. |

**Response**: `202 Accepted`:

```json
{ "status": "accepted", "backtest_id": "uuid" }
```

The strategy is loaded by `PluginRegistry.load_strategy(name)`. `500`
if the strategy cannot be loaded (not found, import error,
instantiation error).

### `GET /api/v1/backtest/results/{backtest_id}`

Poll for the result of a backtest.

**Auth**: Bearer JWT or API key with `read`+ scope.

**Path params**: `backtest_id` — UUID.

**Response (still running)**: `200 OK`:

```json
{
  "status": "running",
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "initial_capital": 0.0,
  "final_value": 0.0,
  "metrics": { "...": "see MetricsSummary" },
  "equity_curve": [],
  "drawdown_curve": [],
  "error": null,
  "evaluation": null
}
```

**Response (completed)**: `200 OK` — full metrics payload:

```json
{
  "status": "completed",
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "initial_capital": 100000.0,
  "final_value": 112345.67,
  "metrics": {
    "total_return_pct": 12.35,
    "annualized_return_pct": 12.35,
    "sharpe_ratio": 1.42,
    "sortino_ratio": 1.78,
    "max_drawdown_pct": -7.21,
    "max_drawdown_duration_days": 38,
    "max_drawdown_recovery_days": 22,
    "calmar_ratio": 1.71,
    "volatility_annual_pct": 8.6,
    "total_trades": 24,
    "win_rate": 0.583,
    "profit_factor": 2.12,
    "avg_trade_pnl": 514.34,
    "avg_winner": 1320.0,
    "avg_loser": -810.0,
    "best_trade": 3200.0,
    "worst_trade": -1800.0,
    "max_consecutive_wins": 5,
    "max_consecutive_losses": 3,
    "total_costs": 235.10,
    "total_taxes": 412.50,
    "cost_drag_pct": 0.235,
    "turnover_ratio": 2.41,
    "exposure_pct": 78.4,
    "rolling_metrics": [
      {
        "window_days": 30,
        "sharpe_ratio": 1.55,
        "sortino_ratio": 1.92,
        "volatility_annual_pct": 8.1,
        "max_drawdown_pct": -3.4
      }
    ]
  },
  "equity_curve": [
    { "timestamp": "2023-01-03T00:00:00Z", "equity": 100000.0 },
    { "timestamp": "2023-01-04T00:00:00Z", "equity": 100230.0 }
  ],
  "drawdown_curve": [-0.0, -0.0023, -0.0045, -0.012],
  "error": null,
  "evaluation": {
    "composite_score": 7.4,
    "breakdown": { "returns": 8, "risk": 7, "consistency": 6 }
  }
}
```

**Response (failed)**: `200 OK`:

```json
{
  "status": "failed",
  "strategy_name": "mean_reversion_basic",
  "symbol": "AAPL",
  "initial_capital": 0.0,
  "final_value": 0.0,
  "metrics": { "...empty summary..." },
  "equity_curve": [],
  "drawdown_curve": [],
  "error": "ProviderError: Yahoo Finance returned 502",
  "evaluation": null
}
```

**Response (not found / expired)**: `404 Not Found`. Results older
than `_RESULTS_TTL_SECONDS` (1 hour) are evicted.

## Polling guidance

Clients should poll `GET /results/{id}` at 1–2 s intervals while
`status == "running"` and stop as soon as `status` is `"completed"` or
`"failed"`. The route is cheap (in-memory dict lookup) — there is no
exponential back-off requirement, but please don't tight-loop it
either.

For real-time progress updates, subscribe to the
`backtest.completed` and `backtest.failed` events via the WebSocket
(see [`websocket.md`](websocket.md)).

## Cost model

The backtest runner injects the engine's cost model into every
strategy `evaluate()` call. The metrics `total_costs`, `total_taxes`,
`cost_drag_pct` reflect this. Strategies that ignore the cost model
will see these metrics underestimate real drag — see
[`docs/architecture/plugins.md`](../architecture/plugins.md) for the
contract.

## Persistance

Today the result is **not** persisted to the `backtest_results` table
on completion; the in-process cache is the only store. The
`BacktestResult` SQLAlchemy model exists for the eventual migration
to durable storage. Operators that need persistence today should
capture the response of `GET /results/{id}` once `status ==
"completed"` and store it externally.
