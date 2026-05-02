# Runbook — API latency

**Alerts**: `APILatencyFastBurn`, `APILatencyMediumBurn`,
`APILatencySlowBurn`, `APILatencyBudgetExhaustion`

**SLO**: 99% of HTTP responses complete in `< 1.0 s` over 28 days
([slos.md](../slos.md#critical-user-journeys)).

## What this means

A meaningful fraction of HTTP responses are slower than 1 second. Users
will perceive the API as sluggish even if no requests outright fail. Page
severity at fast and medium burn rates because slow APIs are usually the
first sign of a deeper resource problem.

## First 60 seconds

1. Open the **Nexus / API traffic (RED)** dashboard — look at the
   "Latency p99 by route" panel. One outlier route is a different
   problem than every route slowing down at once.
2. Check `nexus:slo:api_latency:slow_ratio:rate5m` against
   `:rate1h`. If the 5m rate is much higher than the 1h, you're at the
   leading edge of a degradation.

## Triage

- **Which route?** `topk(5, histogram_quantile(0.99, sum by (route, le)
  (rate(nexus_http_request_duration_seconds_bucket[5m]))))`
- **Is there latency on the DB side?** Postgres `pg_stat_activity` for
  long-running queries. The `engine.observability.lineage` span tags will
  show whether the time is spent in Python, awaiting DB, or awaiting an
  outbound HTTP call.
- **Has traffic spiked?** A 4× RPS increase against a constant pool
  size will look like latency degradation. Compare RPS to historical
  baseline on the same dashboard.
- **TaskIQ backlog?** If async-handing routes are slow, the workers may
  be saturated — see [`task-pipeline.md`](task-pipeline.md).

## Common causes

- **Slow query** — a recently added query lacks an index, or an existing
  one is using a bad plan. Run `EXPLAIN (ANALYZE, BUFFERS)` and add the
  appropriate index in a follow-up migration.
- **Connection pool exhaustion** — `asyncpg` errors like "too many
  clients" in logs, or sustained queue depth. Bump the pool ceiling or
  shrink the worker count to match.
- **Outbound dependency slow** — broker / data-provider API on the wrong
  side of an SLA. Mitigation: shorten timeouts, add a circuit breaker,
  fail fast.
- **GC / event-loop stall** — Python event loop is blocked by sync code
  on the hot path. Profile with `py-spy` to find the offender; convert
  to async or offload to a thread.
- **Cold start after deploy** — first requests after a roll are slow
  because connections / caches haven't warmed up. Should self-heal
  within 5 min; otherwise treat as one of the above.

## Escalation

- If p99 doubles for any route within a 5-minute window and stays there,
  page the API on-call.
- If multiple routes spike together, suspect a shared dependency
  (DB, queue) and page that on-call instead.

## Post-incident

- Record the slowest route and the cause in the on-call log.
- If a query was the culprit, file the index migration before closing
  the incident.
- If the SLO target itself is wrong (chronically violated under healthy
  traffic), open a PR adjusting `docs/operations/slos.md` and the rule
  file together.
