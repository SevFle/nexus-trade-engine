# Runbook — Task pipeline (TaskIQ)

**Alerts**: `TaskPipelineSlowBurn`, `TaskPipelineBudgetExhaustion`

**SLO**: 99.5% of TaskIQ jobs reach `completed` (vs. `failed` / `dead`)
over 28 days
([slos.md](../slos.md#critical-user-journeys)).

## What this means

The async pipeline that runs backtests, scheduled rebalances, and
reporting is producing failed or dead jobs at a rate that erodes the
SLO budget. Ticket severity — most failures are recoverable on retry —
but a stuck pipeline silently breaks user-facing features.

## First 60 seconds

1. Confirm workers are alive: count of TaskIQ worker pods / processes
   matches the configured replica count.
2. `sum by (outcome) (rate(nexus_task_runs_total[5m]))` — is the
   `dead` outcome dominating? Dead = exceeded retry budget.

## Triage

- **Which task is failing?**
  `topk(5, sum by (task) (rate(nexus_task_runs_total{outcome=~"failed|dead"}[1h])))`
- **Are workers actually consuming?** Look at the broker (Redis/Valkey)
  for queue depth. If depth is climbing while the failure ratio is low,
  workers may be wedged rather than failing — check pod logs for
  hangs / coroutine stuck states.
- **Does the failed task share a dependency?** A class of tasks that
  all hit the same broker / data provider failing together points at
  that downstream rather than the task itself.

## Common causes

- **Broker (Redis/Valkey) outage** — `taskiq_redis` errors in worker
  logs. Workers can't enqueue or fetch; everything stalls. Restore the
  broker first, then drain the backlog.
- **Worker pool too small for the spike** — sustained backlog with
  healthy completion ratio. Bump replica count temporarily; file a
  capacity follow-up.
- **Bad code path on the task** — exception in the task body that
  retries until exhaustion. Look for the structlog `event_type`
  pattern from the offending task and a stable stack trace. Add a
  regression test, ship the fix, replay the dead-letter queue once
  the new code is live.
- **DB lock contention** — tasks that write to the same row pile up
  and time out. Mitigation: serialize via a queue per resource, or
  switch to an upsert pattern.
- **Memory leak** — workers slowly grow until OOM-killed mid-task,
  marking the in-flight job dead. Restart cadence is the workaround;
  fix is to find and remove the leak.

## Escalation

If the broker is down or queue depth keeps climbing despite a healthy
worker count, page the platform / infra on-call. Backtest results
and webhook fan-out depend on this pipeline; a stuck queue manifests
to users as "I submitted a backtest 20 minutes ago and nothing
happened".

## Post-incident

- Capture the root cause and the dead-letter handling decision (replayed,
  dropped, manually fixed) in the on-call log.
- If a task is not idempotent, file a follow-up to make it so —
  retries should be safe by default.
- If the SLO target is wrong (chronically violated by a known-flaky
  task class), either fix the task or move it out of the SLO surface.
