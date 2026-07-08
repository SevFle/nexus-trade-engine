# ADR-0008: Pluggable MetricsBackend Protocol

- **Status**: Accepted
- **Date**: 2026-06-08
- **Deciders**: Lead maintainer + observability reviewer
- **Tags**: observability, architecture, metrics

## Context and Problem Statement

Engine code needs to emit operational metrics — counters, gauges,
histograms — from many call sites: the event bus, the webhook
dispatcher, the WebSocket connection manager, the auth layer. The
question was *which* metrics system to couple the engine to.

Nexus is single-tenant self-hosted. Different operators standardise on
different observability stacks: Prometheus (pull), StatsD (push),
OpenTelemetry Collector (OTLP), or nothing at all for a solo
deployment that just wants logs. Hard-coding `prometheus_client` into
every call site would (a) force a runtime dependency on every operator
and (b) make unit-testing metric emissions require a Prometheus
registry fixture. Hard-coding OTel would do the same for a different
crowd.

## Decision Drivers

- **No mandatory monitoring dependency.** A solo operator running a
  single pod with `docker compose` must not need to stand up Prometheus
  to import the engine.
- **Testability.** Unit tests need to assert that a specific counter
  was incremented, without spinning up a scrape endpoint.
- **Swappability at deploy time.** The operator picks the exporter;
  the engine code stays identical.
- **Tag-order independence.** Call sites should not have to thread a
  canonical tag-order through their code; `{a:1,b:2}` and `{b:2,a:1}`
  must aggregate together.

## Considered Options

1. **Hard-code `prometheus_client`** at every call site.
2. **Hard-code OpenTelemetry Metrics API** (`opentelemetry.metrics`).
3. **Define a `MetricsBackend` Protocol; ship a `NullBackend` default
   and a `RecordingBackend` test double.** Operators (or the lifespan)
   wire the real exporter via `set_metrics()`.
4. **Build metrics on top of structlog** — emit metric events as log
   lines and aggregate downstream.

## Decision Outcome

Chosen option: **Option 3 — pluggable `MetricsBackend` Protocol**, a
process-wide singleton resolved lazily by `get_metrics()`.

### How it works

Source: [`engine/observability/metrics.py`](../../engine/observability/metrics.py).

```python
@runtime_checkable
class MetricsBackend(Protocol):
    def counter(self, name, value=1.0, tags=None) -> None: ...
    def gauge(self, name, value, tags=None) -> None: ...
    def histogram(self, name, value, tags=None) -> None: ...
    def timer(self, name, tags=None) -> ContextManager[None]: ...
```

- `_BACKEND` is a module-level singleton defaulting to `NullBackend`
  (every call validates the name, then no-ops). Importing the module
  costs nothing.
- `set_metrics(backend)` swaps the singleton under a lock. The app
  lifespan calls `set_metrics(PrometheusBackend())`
  ([`app.py:156`](../../engine/app.py#L156)); tests call
  `set_metrics(RecordingBackend())` in a fixture.
- Call sites resolve lazily: `get_metrics().counter("foo", tags=…)`.
  Lazy resolution matters because the `EventBus` is constructed before
  the lifespan swaps the backend — it reads `get_metrics()` at call
  time, not at construction time
  ([`bus.py:112`](../../engine/events/bus.py#L112)).

### Consequences

- **Positive** — zero monitoring dependency unless the operator opts in.
  `pip install nexus-trade-engine` does not pull `prometheus_client`.
- **Positive** — tests assert on emissions directly:
  ```python
  backend = RecordingBackend()
  set_metrics(backend)
  …
  assert ("webhook.delivered", (("status","ok"),)) in backend.counters
  ```
- **Positive** — swapping to OTel or StatsD later is one class + one
  `set_metrics()` call. No call-site churn.
- **Negative** — the `Protocol` methods are fire-and-forget, so a typo
  in a metric name is invisible until someone reads `/metrics`. A
  metric catalog (the intended fix) does not exist yet — see
  [`known-limitations.md`](../known-limitations.md) "SLO metric
  coverage is incomplete".

## The PrometheusBackend

Source: [`engine/observability/prometheus.py`](../../engine/observability/prometheus.py).

`PrometheusBackend` subclasses `RecordingBackend` and adds a `render()`
that emits Prometheus text-exposition format. The
[`GET /metrics`](../api-reference.md) handler calls `render()` and
returns it as `text/plain; version=0.0.4`.

Design choices in the renderer:

- **No `prometheus_client` dependency.** The renderer is hand-rolled
  (~170 lines). It emits `*_count` + `*_sum` for histograms (no bucketed
  quantiles). Operators who need real histogram buckets should swap in a
  backend that pre-buckets at observation time — the door is open, the
  dependency is not taken here.
- **Dot → underscore.** Engine metric names are dot-separated
  (`webhook.delivered`) to match the codebase style. Prometheus
  requires `[a-zA-Z_:][a-zA-Z0-9_:]*`, so dots become underscores at
  render time.
- **Single-process.** The recorder is in-memory per process. Multi-pod
  deployments need a shared aggregation layer (e.g. `prometheus_client`
  multi-process mode or an OTel collector) — explicitly deferred.

## Pros and Cons of the Options

### Option 1 — Hard-code `prometheus_client`

- **Pros:** Full histogram buckets, battle-tested, less code.
- **Cons:** Mandatory runtime dependency; tests need a registry fixture;
  couples every call site to the Prometheus API shape.

### Option 2 — Hard-code OpenTelemetry Metrics

- **Pros:** Vendor-neutral standard; aligns with the OTel traces already
  wired in the lifespan.
- **Cons:** Still a mandatory dependency; the OTel Metrics API is more
  verbose (Meter → Instrument → binding) for simple counter increments;
  `opentelemetry-api` without a configured exporter silently no-ops,
  which masks misconfiguration.

### Option 3 — Pluggable Protocol (chosen)

- **Pros:** No mandatory dep; testable; swappable; the lifespan already
  wires Prometheus so operators who want it get it with zero config.
- **Cons:** The Protocol is our own contract, so it can drift from what
  exporters support (e.g. exemplars, native histograms). Mitigated by
  keeping the surface tiny (4 methods).

### Option 4 — Metrics as log events

- **Pros:** Zero new abstraction; one pipeline for logs + metrics.
- **Cons:** Aggregation must happen downstream (LogQL, Splunk SPL);
  cardinality explosions in tag values are harder to control; latency
  is higher than an in-process counter.

## Links

- Original issue: gh#34
- Source: [`engine/observability/metrics.py`](../../engine/observability/metrics.py),
  [`engine/observability/prometheus.py`](../../engine/observability/prometheus.py)
- Wiring: [`engine/app.py:156`](../../engine/app.py) (`set_metrics(PrometheusBackend())`)
- Related: [`docs/operations/slos.md`](../operations/slos.md),
  [`docs/observability/logging.md`](../observability/logging.md)
- Known gap: [`docs/known-limitations.md`](../known-limitations.md)
  "SLO metric coverage is incomplete"
