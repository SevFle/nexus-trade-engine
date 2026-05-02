# Runbook — Backtest submission

**Alerts**: `BacktestSubmitSlowBurn`, `BacktestSubmitBudgetExhaustion`

**SLO**: 99% of `POST /api/v1/backtest` calls accepted (no 4xx/5xx error)
over 28 days
([slos.md](../slos.md#critical-user-journeys)).

## What this means

Users submitting backtests are getting rejected or erroring out. This
is a write path tied to user research — failed submissions waste user
time and obscure deeper problems with the strategy DSL or input data.
Ticket severity (not page); investigate within business hours.

## First 60 seconds

1. Open the **Nexus / API traffic (RED)** dashboard, filter to
   `route="/api/v1/backtest"`. Confirm the rejected/error count is
   actually rising versus the baseline.
2. Pull the last 10 failed submissions from logs by request id and
   skim their response bodies. If they're all the same validation
   error, the cause is a bad client deploy or schema change.

## Triage

- **What's the breakdown of `outcome` labels?**
  `sum by (outcome) (rate(nexus_backtest_submissions_total[1h]))`
- **Is `error` (server-side) dominating, or `rejected` (validation)?**
  - `rejected` means the user got a 4xx — usually a DSL or input-data
    problem. Not on-call's job to fix; file as a product bug.
  - `error` means the engine itself failed. Look at log entries with
    `event_type="backtest.evaluation_failed"` (the path that wraps
    `StrategyEvaluator().evaluate`).
- **Did the schema change?** Look at recent merges that touched
  `engine/api/routes/backtest.py` or its Pydantic models.

## Common causes

- **`StrategyEvaluator` regression** — a recent change to
  [`engine/core/strategy_evaluator.py`](../../../engine/core/strategy_evaluator.py)
  fails on a class of input. The submission still saves the result row
  but populates `error` because the runner wraps evaluation in
  try/except (intentional — see
  [`backtest_runner.py`](../../../engine/core/backtest_runner.py)). The
  evaluator failure is logged but does not fail the request, so this
  case will *not* trip this SLO unless the entire route 5xxs.
  Investigate why the route itself is failing.
- **Bad input data** — data provider returned an unexpected schema
  (missing column, NaN-only series). Confirm by attempting one of the
  failing submissions against a known-good provider.
- **DB write failure** — `backtest_results` insert blocked. Check the
  schema is at the right Alembic head (gh#8 added two columns,
  `composite_score` + `score_breakdown`).
- **Quota / rate limit** — submissions blocked by the rate-limit
  surface. Check `engine/api/rate_limit.py` configuration; if the limit
  was recently lowered, this is a config problem.

## Escalation

Backtest failures rarely page-able; if they spike to >25% of
submissions, ping the strategy / backtest engineers in addition to the
API on-call.

## Post-incident

- If a class of input now reliably 5xxs, add a regression test under
  `tests/test_backtest_runner.py` or `tests/test_strategy_evaluator.py`.
- If the SLO violation was caused by an upstream client deploy
  (e.g. the React frontend now sends a new field), file the issue on
  the frontend side and consider tightening Pydantic validation.
