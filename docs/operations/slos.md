# Service Level Objectives

This document defines the Service Level Objectives (SLOs) and error
budgets that operators of Nexus Trade Engine should treat as the floor
for "production-acceptable" service. The accompanying Prometheus
recording and alerting rules live at
[`observability/prometheus/slo-rules.yaml`](../../observability/prometheus/slo-rules.yaml).

The SLO model follows Google SRE multi-window multi-burn-rate (MWMBR)
alerting. It is deliberately simple — six SLOs, one error-budget window,
two alert severities — so it survives contact with the on-call rotation.

## Critical User Journeys

| Journey         | SLI                                                   | SLO (28d)   | Severity if violated |
|-----------------|-------------------------------------------------------|-------------|----------------------|
| API availability | Non-5xx HTTP responses / total HTTP responses        | **99.5 %**  | Page                 |
| API latency      | HTTP responses with `duration < 1.0s` / total        | **99.0 %**  | Page                 |
| Auth & MFA       | Non-5xx auth + MFA-verify responses / total          | **99.9 %**  | Page                 |
| Backtest submit  | Successful `POST /api/v1/backtest` / total submissions | **99.0 %**  | Ticket               |
| Webhook delivery | Webhook deliveries terminating in `delivered` / total terminal deliveries | **99.0 %** | Ticket |
| Task pipeline    | TaskIQ jobs reaching `completed` / total scheduled   | **99.5 %**  | Ticket               |

"Page" means an alert wakes the on-call. "Ticket" means it lands in the
backlog within business hours.

## Why these SLOs

- **API availability + latency** — direct user experience. 99.5 % over 28
  days = ≈ 3.6 hours of downtime per month, which is consistent with a
  single self-hosted node without HA.
- **Auth & MFA** — security-critical and unforgiving (a failure means
  legitimate users cannot log in). One nine higher than general API.
- **Backtest submit** — a write path tied to user money decisions; we want
  to know quickly if it stops accepting work. Latency is intentionally
  not SLOed here — backtests are async; latency belongs to the task
  pipeline SLO.
- **Webhook delivery** — outbound side-effect; the dispatcher in
  [`engine/events/webhook_dispatcher.py`](../../engine/events/webhook_dispatcher.py)
  retries 5xx but gives up on 4xx. Drops in delivered-rate that aren't
  attributable to bad operator config indicate either a registry of bad
  endpoints or a bug in retry handling.
- **Task pipeline** — taskiq workers must drain. Stuck queues silently
  break backtests, scheduled rebalances, and reporting.

## Error Budget

For an SLO of `S` over a 28-day window the error budget is `(1 − S)` of
the qualifying events, equivalent in time to:

```
budget_minutes = (1 − S) × 28 × 24 × 60
```

| SLO       | Budget per 28d (events) | Time-equivalent (uptime view) |
|-----------|--------------------------|-------------------------------|
| 99.0 %    | 1.00 % of events         | ≈ 6 h 43 min                  |
| 99.5 %    | 0.50 % of events         | ≈ 3 h 22 min                  |
| 99.9 %    | 0.10 % of events         | ≈ 40 min                      |

When the budget is exhausted the on-call is expected to:

1. Stop pushing risky changes (no migrations, no infra swaps) until the
   budget recovers.
2. Open a follow-up issue tagged `priority-high` to prevent recurrence.
3. Optionally relax the SLO temporarily if the breach was caused by a
   one-off external event — note the relaxation in the on-call log so it
   is not silently absorbed.

## Burn-Rate Alerts (MWMBR)

We use four burn-rate alerts per SLO, paired by severity:

| Severity | Long window | Short window | Burn rate | Triggers if budget would be exhausted in… |
|----------|-------------|--------------|-----------|-------------------------------------------|
| Page     | 1 h         | 5 min        | 14.4 ×    | ≈ 2 days                                  |
| Page     | 6 h         | 30 min       | 6 ×       | ≈ 5 days                                  |
| Ticket   | 24 h        | 2 h          | 3 ×       | ≈ 10 days                                 |
| Ticket   | 72 h        | 6 h          | 1 ×       | ≈ 30 days                                 |

The "short window" gate prevents alerts from firing on a brief blip
already absorbed by the budget; the "long window" gate prevents alerts
from firing on noise. Both windows must exceed the burn-rate threshold
simultaneously for the alert to fire — see Google SRE Workbook chapter 5
for the original derivation.

The Prometheus rule file at
[`observability/prometheus/slo-rules.yaml`](../../observability/prometheus/slo-rules.yaml)
encodes these alerts as four `alert:` blocks per SLO. Recording rules
pre-compute the SLI numerator + denominator at 5 m, 1 h, 6 h, 24 h, and
72 h windows so the alert expressions are cheap.

## Wiring

The metrics backend is currently `NullBackend` by default — the engine
emits to a no-op until an exporter is configured at deploy time. To
collect real SLI data you need:

1. A Prometheus-compatible scrape endpoint. The intended path is to set
   `metrics_backend = "prometheus"` once the exporter lands and expose
   `/metrics` from the FastAPI app. The metric names below are what the
   recording rules expect; they should match what the eventual exporter
   emits.
2. The Prometheus rule file loaded into your Prometheus / Alertmanager
   stack. See [`observability/prometheus/README.md`](../../observability/prometheus/README.md).
3. A receiver in Alertmanager for both `severity: page` and
   `severity: ticket`. Page receivers should be paging-grade
   (PagerDuty / Opsgenie / on-call SMS), ticket receivers can be Slack
   / GitHub issue / email.

## SLI Reference

The SLOs above expect metrics in this shape (units in parentheses):

| Metric                                  | Type        | Tags                                     | Notes                                                                 |
|-----------------------------------------|-------------|------------------------------------------|-----------------------------------------------------------------------|
| `nexus.http.requests_total`             | counter     | `route`, `method`, `status_code`         | One per FastAPI response.                                             |
| `nexus.http.request_duration_seconds`   | histogram   | `route`, `method`, `status_code`         | Total handler duration.                                               |
| `nexus.auth.attempts_total`             | counter     | `outcome` (`success` / `failure` / `mfa_required`) | Auth + MFA verify combined.                                  |
| `nexus.backtest.submissions_total`      | counter     | `outcome` (`accepted` / `rejected` / `error`)      | One per `POST /api/v1/backtest`.                            |
| `nexus.webhook.deliveries_terminal_total` | counter   | `outcome` (`delivered` / `failed`)       | Emitted by the dispatcher when a delivery row reaches a terminal state. |
| `nexus.task.runs_total`                 | counter     | `task`, `outcome` (`completed` / `failed` / `dead`) | One per taskiq worker run.                                  |

Add or change metrics? Update both the rules file and the SLI table here
in the same change so they don't drift.

## Reviewing & Evolving SLOs

- Review SLO targets quarterly. Adjust if the SLI shape changed (e.g. a
  new endpoint dominates traffic) or if the targets are demonstrably
  wrong (chronically over- or under-shooting without corresponding user
  impact).
- Treat each adjustment as a code change — bump the rule file and this
  document together, and capture the rationale in the PR description.
- New journeys (live trading loop, OMS) added in #109 / #111 will need
  their own SLOs. Add them to this table when they land; do not ship a
  new write-path without one.

## Related

- [`observability/prometheus/slo-rules.yaml`](../../observability/prometheus/slo-rules.yaml)
- [`docs/operations/backup-and-recovery.md`](backup-and-recovery.md) —
  RPO/RTO live in that runbook; they are not SLOs in the strict sense
  but feed the same alert pipeline.
- [`docs/observability/logging.md`](../observability/logging.md) — log
  schema that pairs with these metrics.
