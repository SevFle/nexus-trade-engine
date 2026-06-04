# Runbooks

Operational playbooks for the alerts we actually page on. Each
runbook lives in
[`operations/runbooks/`](operations/runbooks/) and follows the
shape below.

## Index

| Runbook | When to use |
|---|---|
| [operations/runbooks/README.md](operations/runbooks/README.md) | Master index, links each runbook to its alert group. |
| [operations/runbooks/api-availability.md](operations/runbooks/api-availability.md) | Error budget burn on success rate. |
| [operations/runbooks/api-latency.md](operations/runbooks/api-latency.md) | Error budget burn on p95/p99 latency. |
| [operations/runbooks/auth-mfa.md](operations/runbooks/auth-mfa.md) | Spike in MFA failures. |
| [operations/runbooks/backtest-submit.md](operations/runbooks/backtest-submit.md) | Backtest enqueue rate or latency regression. |
| [operations/runbooks/webhook-delivery.md](operations/runbooks/webhook-delivery.md) | Outbound webhook failures or queue backlog. |
| [operations/runbooks/task-pipeline.md](operations/runbooks/task-pipeline.md) | TaskIQ queue stalled or piling up. |
| [operations/runbooks/database.md](operations/runbooks/database.md) | DB unreachability, slow queries, migration failures. |
| [operations/runbooks/upgrade.md](operations/runbooks/upgrade.md) | Engine upgrade / rollback procedure. |
| [operations/backup-and-recovery.md](operations/backup-and-recovery.md) | Restore from backup. |
| [operations/dr-drill-checklist.md](operations/dr-drill-checklist.md) | DR drill checklist (run quarterly). |

## How to use a runbook

Every operational runbook follows the same shape so on-call can
scan them at 03:00:

1. **What this means** — one paragraph, plain language.
2. **First 60 seconds** — confirm the alert is real, not a probe
   failure.
3. **Triage** — how to localise the cause (logs, dashboards,
   recent merges).
4. **Common causes** — known failure modes with the fix path each
   maps to.
5. **Escalation** — who to ping when the cause is not in this
   runbook.
6. **Post-incident** — what to capture so the runbook gets better.

Don't ship an alert without a runbook. The `runbook` annotation
on every Prometheus alert in
[`observability/prometheus/slo-rules.yaml`](../observability/prometheus/)
must resolve to a page in this index.
