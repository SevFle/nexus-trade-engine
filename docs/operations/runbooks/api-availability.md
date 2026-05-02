# Runbook — API availability

**Alerts**: `APIAvailabilityFastBurn`, `APIAvailabilityMediumBurn`,
`APIAvailabilitySlowBurn`, `APIAvailabilityBudgetExhaustion`

**SLO**: 99.5% non-5xx HTTP responses over 28 days
([slos.md](../slos.md#critical-user-journeys)).

## What this means

A non-trivial fraction of HTTP responses from the engine API are 5xx.
Real users (or other systems) are seeing failed requests. Page severity
because this is the most visible failure mode.

## First 60 seconds

1. Open the **Nexus / SLO overview** dashboard. Confirm the 1h error
   ratio is genuinely above the threshold (not a single-scrape blip).
2. Curl the public health endpoint:
   ```bash
   curl -fsS https://<your-host>/api/v1/health
   ```
   If this fails, the engine is down — skip ahead to "Common causes →
   Process down".
3. If health is fine, open the **Nexus / API traffic (RED)** dashboard
   and look for which `route` or `status_code` is dominating the 5xx
   share.

## Triage

- **Which routes are failing?** `topk(5, sum by (route, status_code)
  (rate(nexus_http_requests_total{status_code=~"5.."}[5m])))`
- **Did this start with a deploy?** Cross-reference the spike on the
  dashboard with the `release: published` events in
  [`docs/RELEASING.md`](../../RELEASING.md). If a release just rolled
  out, consider pinning to the previous image tag.
- **Are downstream dependencies healthy?** Postgres, Redis/Valkey,
  TaskIQ broker, any broker integrations. The
  `engine.observability.lineage` middleware tags these in logs.
- **Is the error concentrated to one user / tenant?** If so, the cause is
  more likely user-visible bad data than a systemic failure.

## Common causes

- **Process down** — one of the engine replicas is crash-looping. Check
  pod / container logs, restart policies, and recent config changes.
  Roll back to the previous image if a recent deploy is the cause.
- **DB unavailable** — `asyncpg` or SQLAlchemy connection errors in the
  logs. See [`backup-and-recovery.md`](../backup-and-recovery.md) if
  this looks like a primary failure rather than a network blip.
- **Migration mid-flight** — a long-running `alembic upgrade` is
  blocking. Either let it finish or roll the deploy back.
- **Bad request shape from a new client** — 5xx where the engine should
  have returned 4xx. Open a follow-up to harden validation; in the
  meantime, the alert will resolve once the bad client backs off.
- **Resource exhaustion** — OOM, file-descriptor limit, or worker pool
  saturation. Look at host metrics; bump replica count or limits as a
  short-term mitigation.

## Escalation

If the cause is not obvious within 15 minutes:
- Ping the on-call engineer for the affected component (storage / API /
  deploy).
- Open an incident channel and start a timeline.
- If user-visible for more than 30 minutes, post a status update.

## Post-incident

- Capture the start time, end time, root cause, and mitigation in the
  on-call log.
- Open issues for any gap that extended detection or recovery.
- If a class of cause is missing from "Common causes" above, add it.
- If the alert fired but the root cause was a probe / monitoring artifact,
  consider tightening the recording-rule denominator or excluding the
  probe from the SLI.
