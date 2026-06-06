# System API

Liveness, readiness, and a headless-friendly status endpoint.
Implementation: [`engine/api/routes/health.py`](../../engine/api/routes/health.py),
[`engine/api/routes/system.py`](../../engine/api/routes/system.py),
[`engine/api/routes/metrics.py`](../../engine/api/routes/metrics.py).

These endpoints are intentionally **not** auth-gated (except
`/system/status`) so they can be hit by load balancers, k8s probes,
and CI scripts that don't have credentials.

## Endpoint summary

| Method | Path | Auth | Rate-limit exempt? | Purpose |
|---|---|---|---|---|
| `GET` | `/health`               | none | ✓ | Liveness — process is up |
| `GET` | `/health/providers`     | none | — | Provider registry health (per-adapter latency / status) |
| `GET` | `/ready`                | none | — | Readiness — DB + Valkey reachable |
| `GET` | `/metrics`              | none | ✓ | Prometheus scrape |
| `GET` | `/api/v1/system/status` | JWT  | — | Headless-friendly status: version, uptime, counts |

## Liveness vs. readiness

| Probe    | Checks                                          | Returns `degraded` if… |
|----------|-------------------------------------------------|------------------------|
| `/health` | Process is alive (always returns `{"status":"ok"}`) | n/a — never degraded |
| `/ready`  | DB round-trip + Valkey `PING`                    | Either dependency fails |

The split is deliberate: a load balancer should keep traffic flowing
to *any* live instance, but only route to instances that are *ready*.
A `degraded` readiness response should pull the pod out of rotation.

## `/health/providers`

Returns per-adapter health for everything in the data-provider
registry. Used by the React dashboard's "data sources" panel.

```json
{
  "status": "ok|degraded|down",
  "providers": {
    "yahoo": {"status": "up", "latency_ms": 123, "detail": null},
    "polygon": {"status": "down", "latency_ms": null, "detail": "401"}
  }
}
```

Overall `status` is `ok` if every adapter is `up`, `degraded` if any
is not, `down` if all are `down`.

## `/ready`

```json
{
  "status": "ok",
  "db": "ok",
  "valkey": "ok"
}
```

The handler uses a short-lived DB session and the
`app.state.valkey` client. Either failure surfaces as `"db": "error"`
or `"valkey": "error"` plus an `error` log entry — never as a 5xx.
The route always returns 200 so flapping during a transient blip does
not cascade into a load-balancer drain.

## `/metrics`

Prometheus text format. The metrics backend is wired in
[`engine/app.py:lifespan`](../../engine/app.py) — production uses
`PrometheusBackend`, tests use `NullBackend` so they don't emit
scrape-able counters. The exposed metric names are listed in
[`docs/operations/slos.md`](../operations/slos.md) under "SLI Reference".

## `/api/v1/system/status`

A single JSON object describing the running engine. Designed for CI
probes and operator scripts that don't want to scrape Prometheus.

```python
class SystemStatusResponse(BaseModel):
    engine_version: str
    uptime_seconds: float
    server_time: datetime
    components: list[ComponentStatus]    # today: just "database"
    counts: dict[str, int]               # users / portfolios / backtests / webhooks_active / api_keys_active
```

```bash
curl http://localhost:8000/api/v1/system/status \
  -H 'authorization: Bearer <access>'
```

Counts that fail (e.g. a model not yet migrated) come back as `-1`
rather than failing the whole call.

## Errors

| Status | When |
|---|---|
| `401` | `/system/status` without a valid token. |
| `503` | `/ready` indirectly, when a dependency is unreachable — but the route still returns 200 with `"status": "degraded"`. |

## Related

- [SLOs](../operations/slos.md) — what to alert on.
- [Runbook — API availability](../operations/runbooks/api-availability.md).
- [Runbook — API latency](../operations/runbooks/api-latency.md).
