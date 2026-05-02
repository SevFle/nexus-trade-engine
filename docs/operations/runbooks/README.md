# Alert runbooks

One runbook per SLO, matched 1:1 to the alert groups in
[`observability/prometheus/slo-rules.yaml`](../../../observability/prometheus/slo-rules.yaml).
The `runbook` annotation on every alert points back here.

| Runbook                                   | Linked alerts                                                                       |
|-------------------------------------------|--------------------------------------------------------------------------------------|
| [api-availability.md](api-availability.md) | `APIAvailabilityFastBurn`, `APIAvailabilityMediumBurn`, `APIAvailabilitySlowBurn`, `APIAvailabilityBudgetExhaustion` |
| [api-latency.md](api-latency.md)          | `APILatencyFastBurn`, `APILatencyMediumBurn`, `APILatencySlowBurn`, `APILatencyBudgetExhaustion` |
| [auth-mfa.md](auth-mfa.md)                | `AuthMFAFastBurn`, `AuthMFAMediumBurn`, `AuthMFASlowBurn`, `AuthMFABudgetExhaustion` |
| [backtest-submit.md](backtest-submit.md)  | `BacktestSubmitSlowBurn`, `BacktestSubmitBudgetExhaustion`                          |
| [webhook-delivery.md](webhook-delivery.md) | `WebhookDeliverySlowBurn`, `WebhookDeliveryBudgetExhaustion`                        |
| [task-pipeline.md](task-pipeline.md)      | `TaskPipelineSlowBurn`, `TaskPipelineBudgetExhaustion`                              |

## Runbook structure

Every runbook follows the same shape so on-call can scan them at 03:00:

1. **What this means** — one paragraph, plain language.
2. **First 60 seconds** — confirm the alert is real, not a probe failure.
3. **Triage** — how to localize the cause (logs, dashboards, recent merges).
4. **Common causes** — known failure modes with the fix path each maps to.
5. **Escalation** — who to ping when the cause is not in this runbook.
6. **Post-incident** — what to capture so the runbook gets better.

Add new runbooks when a new SLO lands. Don't ship an alert without a
runbook — the `runbook` annotation is part of the contract.
