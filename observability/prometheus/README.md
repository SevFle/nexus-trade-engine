# Prometheus rules

This directory contains Prometheus configuration that operators can pull
into their own monitoring stack.

| File | Purpose |
|------|---------|
| `slo-rules.yaml` | Recording + multi-window multi-burn-rate alert rules for the six SLOs defined in [`docs/operations/slos.md`](../../docs/operations/slos.md). |

## Loading the rules

In your `prometheus.yml`:

```yaml
rule_files:
  - /etc/prometheus/rules/slo-rules.yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]
```

Then mount or copy `slo-rules.yaml` into `/etc/prometheus/rules/`.

## Validating

Before deploying changes, run `promtool` against the file:

```bash
promtool check rules observability/prometheus/slo-rules.yaml
promtool test rules <your-test-file>          # optional, when adding new SLOs
```

`promtool` is shipped with the Prometheus binary release.

## Routing in Alertmanager

Two severities are emitted: `severity: page` (wakes the on-call) and
`severity: ticket` (business-hours follow-up). A minimal Alertmanager
route looks like:

```yaml
route:
  receiver: ticket-default
  group_by: [slo]
  routes:
    - matchers: [severity="page"]
      receiver: pager
      group_wait: 30s
      repeat_interval: 4h
    - matchers: [severity="ticket"]
      receiver: ticket-default
      repeat_interval: 24h
```

## Editing rules

- Keep the SLI table in `docs/operations/slos.md` in sync with whatever
  metric names appear in `slo-rules.yaml`. The table is the contract; the
  rule file is the implementation.
- Burn-rate thresholds are derived from the SLO target and the
  long/short window pair — see [`docs/operations/slos.md#burn-rate-alerts-mwmbr`](../../docs/operations/slos.md#burn-rate-alerts-mwmbr).
- Recording-rule names follow `nexus:slo:<journey>:<kind>:rate<window>`.
  Adding a new journey? Stick to the same shape so dashboards (#146) can
  pattern-match on the prefix.
