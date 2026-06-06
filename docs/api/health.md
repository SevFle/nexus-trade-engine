# Health and metrics

Source: [`engine/api/routes/health.py`](../../engine/api/routes/health.py),
[`engine/api/routes/metrics.py`](../../engine/api/routes/metrics.py).

## `GET /health`

Liveness probe. Always returns `200` while the Python process is
running and the FastAPI app is mounted. **Does not** touch the
database or Valkey.

**Auth**: none. Exempt from rate limiting.

**Response**: `200 OK`:

```json
{ "status": "ok" }
```

Use this for container-orchestrator liveness probes (Kubernetes
`livenessProbe`, ECS, Nomad). Do not use it as a readiness gate.

## `GET /health/providers`

Health of the configured market-data providers.

**Auth**: none.

**Response**: `200 OK`:

```json
{
  "status": "ok",
  "providers": {
    "yahoo": { "status": "up", "latency_ms": 142, "detail": null },
    "polygon": { "status": "down", "latency_ms": null, "detail": "401 Unauthorized" }
  }
}
```

| Field      | Meaning                                                       |
|------------|---------------------------------------------------------------|
| `status`   | `up`, `degraded`, `down`. Top-level is `ok` if every provider is `up`, `degraded` if any is `degraded`, `down` if all are `down`. |
| `latency_ms` | Round-trip of the provider's `health()` probe, or `null` on failure. |

## `GET /ready`

Readiness probe. Pings both the database (`SELECT 1`) and Valkey
(`PING`). Returns `200` only if both reply.

**Auth**: none.

**Response** (all ok): `200 OK`:

```json
{ "status": "ok", "db": "ok", "valkey": "ok" }
```

**Response** (degraded): `200 OK` (the body is the source of truth,
not the status code):

```json
{ "status": "degraded", "db": "ok", "valkey": "error" }
```

Use this for the container-orchestrator readiness probe — it gates
whether the load balancer should route traffic to this instance.

## `GET /metrics`

Prometheus-formatted metrics. Served by the in-process Prometheus
backend (`engine/observability/prometheus.py`).

**Auth**: none. Exempt from rate limiting.

**Response**: `200 OK`, `Content-Type: text/plain; version=0.0.4` —
the Prometheus exposition format.

The metric catalogue is documented in
[`docs/operations/slos.md`](../operations/slos.md#sli-reference).
The set is small and additive; do not introduce a metric that isn't
referenced by either an SLO or a Grafana dashboard.
