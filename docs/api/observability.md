# Observability, Health, System, WebSocket, Client Errors

> **Source:**
> [`engine/api/routes/health.py`](../../engine/api/routes/health.py),
> [`engine/api/routes/metrics.py`](../../engine/api/routes/metrics.py),
> [`engine/api/routes/system.py`](../../engine/api/routes/system.py),
> [`engine/api/routes/client_errors.py`](../../engine/api/routes/client_errors.py),
> [`engine/api/routes/websocket.py`](../../engine/api/routes/websocket.py),
> [`engine/observability/`](../../engine/observability/)

## Health & readiness

Unauthenticated, no rate limit (the rate-limiter exempts `/health` and
`/metrics` by default — see `NEXUS_RATE_LIMIT_EXEMPT_PATHS`).

| Method | Path | Behaviour |
|--------|------|-----------|
| `GET` | `/health` | Liveness — returns `{ status: "ok" }` without touching any dependency. Use for "should this pod be in the load-balancer pool". |
| `GET` | `/health/providers` | Probes every data provider's `health()`. Returns `{ status, providers: { name: { status, latency_ms, detail } } }`. `status` is `ok` if every provider is `up`, `degraded` if any are flaky, `down` if all are down. |
| `GET` | `/ready` | Readiness — checks DB (`SELECT 1`) and Valkey (`PING`). Returns `{ status, db, valkey }`. Use to gate traffic: a pod with `db: error` should not receive user requests. |

`/health` and `/ready` are intentionally separate: liveness is "the
process is up", readiness is "the process can serve". They map to
distinct Kubernetes probe types — do not conflate them.

## Prometheus metrics

### `GET /metrics`

Standard Prometheus text exposition. Backed by
[`engine/observability/prometheus.py:PrometheusBackend`](../../engine/observability/prometheus.py),
which is the process-wide `MetricsBackend` switched in during
[`engine/app.py:lifespan`](../../engine/app.py).

The set of metrics includes:

- `http_requests_total{method, path, status}` — counter.
- `http_request_duration_seconds{method, path, status}` — histogram,
  default Prometheus buckets.
- `nexus_backtest_runs_total{outcome}` — counter.
- `nexus_webhook_deliveries_total{status}` — counter.
- `nexus_event_bus_publish_total{event_type}` — counter.
- `nexus_db_pool_in_use`, `nexus_db_pool_available` — gauges.
- `nexus_valkey_pool_in_use` — gauge.

The recording rules that feed the SLO dashboards live in
[`observability/prometheus/slo-rules.yaml`](../../observability/prometheus/slo-rules.yaml).
Dashboard JSONs are in [`observability/grafana/`](../../observability/grafana/).

## System status — `/api/v1/system`

A JSON-shaped status summary for CI/CD probes and operator scripts
that do not want to scrape `/metrics`.

### `GET /api/v1/system/status`

- **Auth:** Bearer or API key.
- **200:** `SystemStatusResponse { engine_version, uptime_seconds,
  server_time, components: [{ name, healthy, detail? }], counts: {
  users, portfolios, backtests, webhooks_active, api_keys_active } }`.

`engine_version` comes from `importlib.metadata.version`. If the
engine is running from a checkout rather than an installed wheel, the
version may be `0.0.0+unknown` — this is a hint, not a bug.

## Client error reporting — `/api/v1/client/errors`

A sink for the frontend's React ErrorBoundary (or any client) to
report unhandled errors. The rate-limit override pins this route to
30 req/min/IP so a buggy render loop cannot DoS the log pipeline.

### `POST /api/v1/client/errors`

- **Body:** `ClientErrorReport { ... }` — see
  [`engine/api/routes/client_errors.py`](../../engine/api/routes/client_errors.py)
  for the exact schema. Includes stack trace, URL, user-agent, and a
  correlation id (which should match the most recent `x-request-id`
  the client received).
- **201:** `ClientErrorAck { id, status: "logged" }`. The id is the
  row's UUID; include it in support tickets.

These rows are **not** exported by `/api/v1/privacy/export` by default
— they are owned by the operator for debugging, not by the user as
personal data. Operators who treat them as personal data should extend
the export walker.

## WebSocket — `/api/v1/ws`

The engine exposes a single WebSocket endpoint for streaming
server-side events to authenticated clients. See
[`engine/api/routes/websocket.py`](../../engine/api/routes/websocket.py)
and [`engine/api/websocket/manager.py`](../../engine/api/websocket/manager.py).

### Protocol

1. Connect with `Authorization: Bearer <jwt>` in the query string or
   the first message (the manager accepts both — clients that cannot
   set headers, like browsers using the native WebSocket constructor,
   use the query string).
2. The server emits `{"event_type": "...", "payload": ...}` frames.
3. The client sends `{"action": "subscribe", "event_types": [...]}` /
   `{"action": "unsubscribe", ...}` to filter the stream.
4. Ping/pong is at the WebSocket protocol layer; no application-level
   heartbeat.

The set of subscribable event types matches what the `EventBus`
publishes (see [`engine/events/bus.py`](../../engine/events/bus.py)).

## Structured logging — code-side

All routes log via `structlog`. The setup is in
[`engine/observability/logging.py`](../../engine/observability/logging.py).
Operators control the format with `NEXUS_LOG_FORMAT` (`console` for
dev, `json` for production) and the sink with `NEXUS_LOG_SINK`
(`stdout`, `file`, `otlp`).

> **Reserved kwarg:** `event` is reserved by structlog — pass
> `event_type=` instead. Calling `logger.info("foo", event=bar)` will
> silently overwrite the message. The convention is enforced by review
> and a `ruff` custom check is on the wishlist.

## Distributed tracing

OpenTelemetry SDK is wired up in
[`engine/observability/tracing.py`](../../engine/observability/tracing.py).
The OTLP exporter endpoint is `NEXUS_OTLP_ENDPOINT`; empty disables
export. Sentry SDK is wired up alongside it (`NEXUS_SENTRY_DSN`).

The correlation id middleware
([`engine/observability/middleware.py:CorrelationIdMiddleware`](../../engine/observability/middleware.py))
stamps every request with an `x-request-id`, propagates any inbound
traceparent, and binds both to the structlog context so every log line
inside the request handler is searchable by id.
