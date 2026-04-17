# Backtest Golden-File Regression Tests

Pinned snapshots of representative backtest scenarios. If a future code
change shifts the math in `engine/core/backtest_runner.py`,
`engine/core/portfolio.py`, `engine/core/order_manager.py`,
`engine/core/cost_model.py`, or `engine/core/metrics.py`, these tests
fail with a clear diff.

## Directory Structure

Each fixture is a directory containing:

```
<fixture_name>/
  config.yaml                  Strategy, data params, tolerances
  expected_metrics.json        Pinned metric values
  expected_equity_curve.csv.gz Sparse equity curve checkpoints
  expected_trades.csv.gz       Trade list (count, timing, side)
  README.md                    Scenario description
```

## How to Run

```bash
pytest tests/test_golden_backtests.py -v
```

## How to Update Baselines

After an **intentional** math change (bug fix, cost-model update):

```bash
pytest tests/test_golden_backtests.py --update-golden
```

Inspect the diff before committing. The `update-golden` GitHub workflow
requires maintainer approval via `REQUIRE_APPROVAL` env var.

## Tolerance Philosophy

Tight by default; widen only with documented justification in the fixture's
`tolerances` section of `config.yaml`:

| Metric | Default Tolerance | Notes |
|--------|------------------|-------|
| Sharpe ratio | +/- 0.001 | |
| CAGR | +/- 0.01% | |
| Total return | +/- 0.01% | |
| Max drawdown | +/- 1% | |
| Equity checkpoints | +/- 0.01% | Sparse, every ~10th bar |
| Trade count | Exact | |
| Trade side/qty | Exact | |

A PR that widens tolerance without a linked cause fails review.

## Active Fixtures

| Fixture | Strategy | Range | Bars |
|---------|----------|-------|------|
| buy_and_hold_spy_2020_2024 | Buy & hold | 2020-2024 | ~1300 |
| sma_crossover_spy_50_200 | SMA 50/200 | 2020-2024 | ~1300 |
| mean_reversion_aapl_2022 | Z-score MR | 2022 | ~300 |
| crypto_dca_btc_2023_2024 | DCA weekly | 2023-2024 | ~550 |

## Pending Fixtures

These are configured but skipped until the engine supports the required features:

| Fixture | Blocked By |
|---------|-----------|
| pairs_trading_ko_pep_2023 | Multi-symbol support (#96) |
| covered_call_spy_2023 | Options chain support (#97) |
| forex_carry_2023 | Forex asset class support |
| multi_strategy_portfolio_2023 | Multi-strategy aggregation (#1) |

## Adding a New Fixture

1. Create a directory under `tests/golden/backtests/<name>/`
2. Write `config.yaml` with strategy class, params, data config, tolerances
3. Write `README.md` describing the scenario
4. If the strategy class is new, add it to `STRATEGY_REGISTRY` in
   `tests/test_golden_backtests.py`
5. Add a test method to `TestGoldenBacktests`
6. Run with `--update-golden` to generate baselines
7. Commit config, README, and generated expected files together

## Data Snapshots

Input data is generated deterministically from a seeded RNG (no network).
Snapshots are cached in `tests/data/snapshots/` as compressed CSV. The seed
and generation parameters are pinned in each fixture's `config.yaml`.

## Runtime Budget

Each fixture must complete within 30 seconds. Fixtures that exceed this
are split or have their data range reduced.
