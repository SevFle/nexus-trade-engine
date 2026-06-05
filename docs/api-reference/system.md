# System & health

Liveness, readiness, Prometheus scrape, and system info.
Source: [`engine/api/routes/health.py`](../../engine/api/routes/health.py),
[`engine/api/routes/metrics.py`](../../engine/api/routes/metrics.py),
[`engine/api/routes/system.py`](../../engine/api/routes/system.py).

## Endpoints

### `GET /health`

Liveness probe — returns `200` as long as the process is alive
and uvicorn can route the request. **Does not check dependencies.**

```json
{ "status": "ok" }
```

### `GET /health/providers`

Probe every registered market-data provider in parallel and
return per-provider status + latency.

**Response** `200 OK`:

```json
{
  "status": "ok",
  "providers": {
    "yahoo": { "status": "up", "latency_ms": 142, "detail": null },
    "polygon": { "status": "down", "latency_ms": null,
                 "detail": "HTTP 502 from upstream" }
  }
}
```

`status` is one of `ok` (every provider up), `degraded` (some
up), `down` (every provider down).

### `GET /ready`

Readiness probe — confirms the engine can talk to its dependencies
(Postgres, Valkey). Use this for k8s readiness gates and load
balancer health checks; do **not** use `/health` for that.

**Response** `200 OK`:

```json
{ "status": "ok", "db": "ok", "valkey": "ok" }
```

If any check fails the response is `503 Service Unavailable` with
`status: "degraded"` and the failing component marked `error`.
The probe is non-fatal — a flapping dependency will not crash the
process.

### `GET /metrics`

Prometheus scrape endpoint. Public (the engine does not enforce
auth on it) but typically fronted by an internal-only ingress.
Exposes `nexus.*` counters/histograms matching the
[SLI reference](../operations/slos.md#sli-reference).

### `GET /api/v1/system/status`

**Auth:** JWT.

Returns version + build metadata + enabled feature flags. Used by
the dashboard's status banner.

**Response** `200 OK` — `SystemStatusResponse`:

```json
{
  "version": "0.1.0",
  "git_sha": "bd4a7fa",
  "build_time": "2026-06-05T10:00:00Z",
  "features": {
    "live_trading": "partial",
    "plugin_sandbox": "partial",
    "multi_provider_auth": "partial",
    "websocket": "partial",
    "multi_asset": "partial"
  }
}
```

## Probe usage guidance

| Probe                  | Use for                                            |
|------------------------|----------------------------------------------------|
| `/health`              | Container liveness (does the process need a restart?). |
| `/ready`               | Ingress / load balancer routing (can the process serve real requests?). |
| `/health/providers`    | Market-data provider status dashboard.             |
| `/metrics`             | Prometheus scrape.                                 |
| `/api/v1/system/status` | UI status banner (user-facing).                   |

`/health` returning `200` is **not** sufficient evidence the
system is healthy — a process with a broken DB pool will still
serve `/health` while every other request fails. Always use
`/ready` for routing decisions.
