# Backtest Golden-File Regression Tests

Pinned snapshots of backtest outputs. If a future code change shifts these
numbers without an explicit accompanying golden update, CI fails — so silent
regressions in the hot path (`engine/core/backtest_runner.py`,
`engine/core/portfolio.py`, `engine/core/order_manager.py`,
`engine/core/cost_model.py`, `engine/core/metrics.py`) get caught immediately.

## How to run

```bash
pytest tests/test_backtest_golden.py -v
```

## How to update a golden (after an intentional behavior change)

```bash
UPDATE_GOLDEN=1 pytest tests/test_backtest_golden.py -v
```

That regenerates the JSON files in this directory in place. **Inspect the
diff before committing** — that's the whole point of the test, the diff
documents the behavior change for the reviewer.

## What's pinned per scenario

Each `<scenario>.json` captures:

- `final_capital` — portfolio total value at end of backtest
- `total_return_pct` — derived from final vs initial cash
- `realized_pnl` — sum of closed-position P&Ls
- `total_trades` — number of filled orders (buys + sells)
- `closed_trades` — number of sell trades
- `equity_curve_checksum` — sha256 of `(timestamp, total_value, cash)` tuples,
  rounded, joined. Catches subtle drift in the equity path even when endpoints match.
- `trades_signature` — sha256 of `(symbol, side, qty, fill_price)` tuples,
  rounded. Catches changes in trade selection / sequencing.

## Adding a scenario

1. Define the inputs as a deterministic pure-function fixture (see `_make_*`
   helpers in `tests/test_backtest_golden.py`).
2. Add a test method that constructs the config, runs the backtest, calls
   `compare_golden("your_scenario_name", result, portfolio)`.
3. Run with `UPDATE_GOLDEN=1` to seed the file.
4. Commit both the test and the golden JSON.

## When to NOT update a golden

If your change isn't supposed to affect backtest math (refactor, lint fix,
import cleanup), and the golden test fails, **that's a real bug**. Don't
regenerate to make the test green — investigate the regression.
