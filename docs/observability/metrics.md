# Metrics & the pluggable `MetricsBackend`

This page is the reference for the metrics subsystem — the
counterpart to [`logging.md`](logging.md) for logs. It covers the
`MetricsBackend` contract, the singleton lifecycle, the built-in
instrumentation, the `/metrics` scrape route, and how tests assert on
emitted values.

> **Code:** `engine/observability/{metrics,http_metrics,prometheus}.py` +
> `engine/api/routes/metrics.py`
> **Decision:** [ADR-0008 — Pluggable MetricsBackend Protocol](../adr/0008-pluggable-metrics-backend.md)
> **SLO contract & coverage gap:** [`operations/slos.md`](../operations/slos.md#wiring)

## Why a Protocol, not `prometheus_client` directly

Engine code that wants to emit a metric should not know *which*
exporter will ship it. The `MetricsBackend`
[`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)
([`metrics.py`](../../engine/observability/metrics.py)) is the seam:
call sites depend on the interface, the operator picks the backend at
deploy time, and tests swap in a recording double. The import-time
default is [`NullBackend`](#backends) — a pure no-op — so importing
the module never pulls a monitoring dependency or starts a thread.

This is the same reasoning as [ADR-0008](../adr/0008-pluggable-metrics-backend.md):
the engine must run with no metrics at all (local dev, CI) and with
any operator-chosen sink (Prometheus now, OTel/StatsD later) without a
code change at the call site.

## The `MetricsBackend` contract

Every method is fire-and-forget. None returns a value; none blocks.

| Method | Signature | Aggregation |
|---|---|---|
| `counter` | `counter(name, value=1.0, tags=None)` | Additive; same `(name, tags)` accumulates. |
| `gauge` | `gauge(name, value, tags=None)` | Last-write-wins for a given `(name, tags)`. |
| `histogram` | `histogram(name, value, tags=None)` | Every observation stored (or bucketed by the backend). |
| `timer` | `timer(name, tags=None)` → context manager | Records `monotonic()` elapsed **in ms** as a histogram observation. |

```python
from engine.observability.metrics import get_metrics

metrics = get_metrics()
metrics.counter("kill_switch.engaged", tags={"actor": actor})
metrics.gauge("kill_switch.state", 1.0)
metrics.histogram("oms.submit.latency_ms", elapsed_ms, tags={"outcome": "filled"})
```

> `timer()` is part of the contract and implemented by every backend,
> but **no engine call site uses it today** — existing histograms are
> timed by hand (`time.monotonic()`) so the timing boundary is
> explicit. Prefer `timer()` for new self-contained timing blocks.

## Backends

| Backend | When | Source |
|---|---|---|
| `NullBackend` | Import-time default. Every call is a no-op that still validates the name (so typos blow up in tests). Use when no exporter is configured. | [`metrics.py`](../../engine/observability/metrics.py) |
| `RecordingBackend` | In-memory test double **and** the base class for any backend that needs to aggregate before rendering. Never used in production paths. | [`metrics.py`](../../engine/observability/metrics.py) |
| `PrometheusBackend` | Production. Extends `RecordingBackend` with `.render()` → Prometheus exposition text. Installed once in the app lifespan. | [`prometheus.py`](../../engine/observability/prometheus.py) |

`PrometheusBackend` inherits aggregation from `RecordingBackend` and
adds exactly one method:

```python
class PrometheusBackend(RecordingBackend):
    def render(self) -> str: ...
```

`set_metrics(PrometheusBackend())` runs in
[`engine/app.py`](../../engine/app.py) during the lifespan startup
(gh#34), so the process-wide singleton is a recording backend before
the first request is served. Operators who want a different exporter
(OTel, StatsD) call `set_metrics(...)` again after `create_app()`
returns — the singleton is the only thing they override.

## Process singleton

```python
_BACKEND: MetricsBackend = NullBackend()       # module-global
_BACKEND_LOCK = threading.Lock()               # guards the swap

def get_metrics() -> MetricsBackend: ...        # read the active backend
def set_metrics(backend) -> None: ...           # install a new one (startup / tests)
```

Call sites read via `get_metrics()`; nothing caches the backend across
calls because tests swap it mid-process. The only writer is startup
(`set_metrics(PrometheusBackend())`) and tests.

### The lazy-resolution pattern (testability without touching the global)

Domain classes that emit metrics do **not** call `get_metrics()`
directly on every emit. They store an optional injected backend and
resolve lazily:

```python
class KillSwitch:
    def __init__(self, ..., metrics: MetricsBackend | None = None) -> None:
        self._metrics = metrics

    @property
    def metrics(self) -> MetricsBackend:
        return self._metrics if self._metrics is not None else get_metrics()
```

This pattern appears in [`kill_switch.py`](../../engine/core/live/kill_switch.py),
[`live/loop.py`](../../engine/core/live/loop.py),
[`execution/paper.py`](../../engine/core/execution/paper.py),
[`oms/risk.py`](../../engine/core/oms/risk.py), and
[`brokers/paper.py`](../../engine/core/brokers/paper.py). It exists so
**unit tests inject a `RecordingBackend` without mutating the
process-global singleton** — see [Testing](#testing) below.

## Naming convention

- **Dots, not underscores or colons.** `http.request.count`,
  `kill_switch.engaged`, `mcp.tool.call`. The renderer converts dots →
  underscores for Prometheus (see [Rendering](#prometheus-rendering)).
- **Tags are arbitrary `str → str`.** No reserved keys. Use `outcome`,
  `actor`, `reason`, etc. — short, low-cardinality values.
- **Tag order is normalized.** Callers may pass `{"a": 1, "b": 2}` or
  `{"b": 2, "a": 1}`; `_canonical_tags` sorts by key so equivalent
  tag sets share one aggregation key. Never hand-build a canonical
  order at the call site.
- **The full HTTP path is deliberately not a tag** (see
  [`HttpMetricsMiddleware`](#built-in-instrumentation)) — FastAPI
  paths carry ids that explode time-series cardinality.

## Built-in instrumentation

### `HttpMetricsMiddleware`

A raw-ASGI middleware ([`http_metrics.py`](../../engine/observability/http_metrics.py))
that emits three metrics per HTTP request, routed through the active
backend (so it's zero-cost under `NullBackend`):

| Metric | Type | Tags | Notes |
|---|---|---|---|
| `http.request.count` | counter | `method`, `status_class` | Exactly once per terminated request. |
| `http.request.duration_ms` | histogram | `method`, `status_class` | Wall-time from scope receive to first `http.response.start`. |
| `http.request.in_flight` | gauge | — | Incremented on entry, decremented in a `finally`, so a scrape always sees a fresh value. |

`status_class` collapses the status code into `1xx`–`5xx` (or
`unknown`). There is **no `route` and no exact `status_code` tag**;
that is the intentional limitation called out in the SLO coverage
table ([`operations/slos.md`](../operations/slos.md#metric-name-coverage-read-this-before-trusting-the-slos)).
It is added **last** in the middleware stack
([`engine/app.py`](../../engine/app.py) `add_middleware`), so it wraps
every other layer, and it deliberately includes `/metrics` itself so
scrape latency is observable.

Raw ASGI (not Starlette `BaseHTTPMiddleware`) is deliberate: it keeps
the timing honest for streaming responses and `BackgroundTasks`, and
it writes the status code only on the first `http.response.start`
message (all Starlette ever sends).

### MCP tools

[`engine/mcp/observability.py`](../../engine/mcp/observability.py)
wraps every tool dispatch with:

- `mcp.tool.call` (counter, tags `tool`, `outcome`)
- `mcp.tool.duration_ms` (histogram)
- `mcp.tool.error` (counter, on the failure path)

### Live / paper / risk surfaces

The live trading loop, kill switch, paper execution backend, paper
broker, and OMS risk engine all emit under their own dot-namespaces
(`oms.submit.*`, `kill_switch.*`, `paper_backend.*`). These are the
in-progress write-path metrics; most are not yet reachable from a
public route (see [`known-limitations.md`](../known-limitations.md)).

## The `/metrics` route

`GET /metrics` — source [`routes/metrics.py`](../../engine/api/routes/metrics.py).
Unauthenticated (operators restrict it with a network ACL or
reverse-proxy auth, the standard Prometheus pattern). The rate limiter
exempts `/metrics`.

```http
HTTP/1.1 200 OK
Content-Type: text/plain; version=0.0.4; charset=utf-8

# HELP http_request_count engine counter (gh#34)
# TYPE http_request_count counter
http_request_count{method="GET",status_class="2xx"} 412
...
http_request_duration_ms_count{method="POST",status_class="2xx"} 38
http_request_duration_ms_sum{method="POST",status_class="2xx"} 21904.7
```

If the active backend is **not** a `RecordingBackend` (e.g. the default
`NullBackend`), the handler returns a placeholder body at `200`:

```
# metrics backend does not support exposition
```

Prometheus accepts an empty-ish scrape, so operators can scrape
unconditionally and only see real data once a recording backend is
installed (which `create_app()` does for them).

## Prometheus rendering

[`render_prometheus(backend)`](../../engine/observability/prometheus.py)
converts the `RecordingBackend` snapshot to exposition text. Rules:

- **Name mapping**: any char outside `[a-zA-Z0-9_:]` → `_`; a leading
  digit is prefixed with `_`. So `http.request.count` →
  `http_request_count`. Engine code uses dots; the renderer does the
  translation.
- **Histograms emit `_count` + `_sum` only — no buckets.** The output
  is typed `summary` so Prometheus accepts it. Operators who need
  bucketed quantiles must swap in a backend that pre-buckets at
  observation time (`prometheus_client.Histogram`). This scaffold keeps
  the door open without taking the dependency.
- **Labels**: rendered `{k="v",...}`, with `\`, `\n`, `"` escaped.
  Empty tag set renders as no braces (label-less series).
- **Output is sorted** by metric name then label set, so two snapshots
  with identical observations diff cleanly.
- **`# HELP` / `# TYPE` are placeholders**, not a metric catalog —
  feed-through descriptions are a future enhancement.

### Single-process caveat

`PrometheusBackend` is a single-process recorder. Each uvicorn worker
(and each replica) keeps its own in-memory counters. To collect real
SLI data you must **scrape every process and aggregate in Prometheus**.
See [`operations/slos.md`](../operations/slos.md#wiring) for the
recommended scrape config.

## Testing

Components accept an injected `metrics=` backend, so tests pass a
`RecordingBackend` directly — no `set_metrics()` mutation of the
global, no `app` fixture needed:

```python
from engine.observability.metrics import RecordingBackend

def test_kill_switch_engages(kill_switch_factory):
    recording = RecordingBackend()
    ks = kill_switch_factory(metrics=recording)
    ks.engage(actor="oncall")

    assert recording.counters.get(("kill_switch.engaged", (("actor", "oncall"),))) == 1.0
    assert recording.gauges.get(("kill_switch.state", ())) == 1.0
```

The `counters` / `gauges` / `histograms` dicts are keyed by
`(metric_name, canonical_tag_tuple)`. The canonical tag tuple is the
tags sorted by key (so assert with a sorted tuple, or use the helper
most suites share — summing over `name == …`):

```python
def total(recording: RecordingBackend, name: str) -> float:
    return sum(v for (n, _t), v in recording.counters.items() if n == name)
```

`histograms` maps `(name, tags) → list[float]` of every observation
(in order), so assert on `len(...)` and `sum(...)`.

## Gaps (read before trusting dashboards)

The metrics *plumbing* is complete; the *SLI coverage* is not. The
intended SLI contract names (`nexus_http_requests_total`,
`nexus_auth_attempts_total`, `nexus_task_runs_total`, …) that
[`observability/prometheus/slo-rules.yaml`](../../observability/prometheus/slo-rules.yaml)
is written against are **partially or not emitted** today. Concretely:

- HTTP metrics are emitted as `http_request_*` tagged
  `method`+`status_class` — no `route`, no exact `status_code`.
- `nexus_auth_attempts_total`, `nexus_backtest_submissions_total`,
  `nexus_task_runs_total` have **no call site** — those SLOs cannot
  fire.

The authoritative per-metric table and the "pick one and update both"
policy live in [`operations/slos.md`](../operations/slos.md#metric-name-coverage-read-this-before-trusting-the-slos);
the operational impact is ranked in
[`known-limitations.md`](../known-limitations.md). When you wire a new
counter, emit it under the dot-namespace and update the SLO coverage
table + rules file in the same PR.

## Related

- [ADR-0008](../adr/0008-pluggable-metrics-backend.md) — *why* the
  backend is a Protocol, not `prometheus_client`.
- [`operations/slos.md`](../operations/slos.md) — the SLO contract,
  error budgets, MWMBR alerts, and the metric-coverage table.
- [`logging.md`](logging.md) — the sibling reference for the structlog
  subsystem. Metrics and logs share the correlation chain
  (`correlation_id`, `request_id`).
- [`api-reference.md`](../api-reference.md#health--observability) — the
  `/metrics`, `/health`, `/ready` route contracts.
- [`known-limitations.md`](../known-limitations.md) — incomplete SLI
  coverage, single-process recorder, no live-trading SLO yet.
