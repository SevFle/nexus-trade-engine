# System / health / metrics API

Implementation: `engine/api/routes/health.py`,
`engine/api/routes/system.py`, `engine/api/routes/metrics.py`.

Unauthenticated observability surface. Operators typically wire
these into load-balancer health checks, Prometheus scrape config,
and CI/CD deploy probes.

## GET /health

Liveness probe. Returns `{"status": "ok"}` as long as the FastAPI
process is serving requests.

**Auth** — none. **Rate-limit exempt**.

## GET /health/providers

Health of every registered data provider. Hits each adapter's
`health()` method.

**Auth** — none.

**Response**:
```json
{
  "status": "ok | degraded | down",
  "providers": {
    "yahoo":   { "status": "up",       "latency_ms": 42,  "detail": null },
    "polygon": { "status": "down",     "latency_ms": null,
                 "detail": "401 Unauthorized" }
  }
}
```

`status` is the worst-of across providers: `ok` only if every
registered provider is `up`; `down` only if every one is `down`;
otherwise `degraded`.

## GET /ready

Readiness probe. Pings the database (`SELECT 1`) and Valkey
(`PING`).

**Auth** — none.

**Response**:
```json
{
  "status": "ok | degraded",
  "db": "ok | error",
  "valkey": "ok | error"
}
```

`status: "degraded"` if any dependency reports `error`. The
individual keys are present even on failure so monitoring can
distinguish which one is broken.

## GET /metrics

Prometheus exposition format. Backed by `PrometheusBackend` (set
during app lifespan via `set_metrics(PrometheusBackend())`).

**Auth** — none. **Rate-limit exempt**.

**Response** — `text/plain; version=0.0.4; charset=utf-8` containing
Prometheus counter / gauge / histogram lines.

The endpoint is intentionally unauthenticated. Restrict it at the
network or reverse-proxy layer if your deployment requires it.

If the active `MetricsBackend` is not a `RecordingBackend`, the
endpoint returns a placeholder comment (`# metrics backend does not
support exposition`) with HTTP 200 so Prometheus scrapes don't fail.

## GET /api/v1/system/status

Operational snapshot: engine version, uptime, dependency health,
row counts. Intended for CI/CD probes and operator scripts that
don't want to parse `/metrics`.

**Auth** — required.

**Response** `SystemStatusResponse`:
```json
{
  "engine_version": "0.1.0",
  "uptime_seconds": 12345.678,
  "server_time": "2026-06-05T12:00:00Z",
  "components": [
    { "name": "database", "healthy": true, "detail": null }
  ],
  "counts": {
    "users": 42,
    "portfolios": 117,
    "backtests": 3891,
    "webhooks_active": 8,
    "api_keys_active": 14
  }
}
```

Counts are best-effort: if a count query fails, the value is `-1`
rather than propagating an error.

## Observability stack

Beyond the public endpoints, the engine exposes:

- **structlog** — JSON in production, console in development.
  Configured by `engine/observability/logging.py`. The `event`
  field name is reserved — pass `event_type=` to log calls.
- **OpenTelemetry** — OTLP exporter wired via
  `engine/observability/tracing.py`. Enable by setting
  `NEXUS_OTLP_ENDPOINT`.
- **Sentry** — `NEXUS_SENTRY_DSN` enables the FastAPI integration.
- **Correlation IDs** — `engine/observability/middleware.py` stamps
  `X-Request-ID` (inbound or generated) onto every log line.

See [`observability/logging.md`](../observability/logging.md) for the
logging conventions and [`operations/slos.md`](../operations/slos.md)
for the SLO definitions backed by `/metrics`.
