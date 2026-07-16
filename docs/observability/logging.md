# Structured logging & correlation IDs

This page is the contract for log records emitted by every Nexus service.
It covers the wire format, the correlation chain, the level policy, the
redaction list, sampling, and sink selection.

> **Code:** `engine/observability/{logging,context,processors,redact,middleware,taskiq_middleware,http_client}.py`
> **Issue:** [#145](https://github.com/SevFle/nexus-trade-engine/issues/145)

## Wire format

Every record is one JSON object per line, UTF-8, written to stdout (default)
or a file. Required fields:

| Field            | Origin                                       | Notes                              |
| ---------------- | -------------------------------------------- | ---------------------------------- |
| `timestamp`      | `structlog.processors.TimeStamper`           | ISO 8601, UTC                      |
| `level`          | `structlog.stdlib.add_log_level`             | `debug`/`info`/`warning`/`error`/`critical` |
| `logger`         | `structlog.stdlib.add_logger_name`           | Dotted module path                 |
| `event`          | First positional arg of `log.<level>(...)`   | Stable event name, snake_case      |
| `service`        | `add_service_metadata`                       | `settings.app_name`                |
| `env`            | `add_service_metadata`                       | `settings.app_env`                 |
| `version`        | `add_service_metadata`                       | `settings.app_version`             |

Context fields, attached when bound:

| Field            | Bound by                                     |
| ---------------- | -------------------------------------------- |
| `correlation_id` | HTTP middleware / taskiq middleware / `ensure_correlation_id` |
| `request_id`     | HTTP middleware (per request)                |
| `span_id`        | HTTP middleware / taskiq middleware (per task) |
| `user_id`, `role` | `ctx.bind_user_context(...)`                |
| `portfolio_id`, `strategy_id`, `broker`, `order_id`, `tool` | `ctx.bind_domain_context(...)` |

## Correlation chain

```
HTTP client                           HTTP server                              Worker / outbound
─────────────                         ──────────────────────                   ─────────────────────────
X-Correlation-Id: c1   ─────────►    CorrelationIdMiddleware       ─────►     CorrelationMiddleware
                                     binds ctx.correlation_id=c1               (taskiq) reads label,
                                                                               binds ctx.correlation_id=c1
                                     log records get correlation_id            log records share c1

                                     correlated_async_client       ─────►     downstream service
                                     forwards X-Correlation-Id: c1
```

Where it gets bound:

- **HTTP entry** — `engine/observability/middleware.py::CorrelationIdMiddleware` reads `X-Correlation-Id` or generates a UUID4 per request, then binds `correlation_id`, `request_id`, `span_id`. Cleared after the response.
- **taskiq workers** — `engine/observability/taskiq_middleware.py::CorrelationMiddleware` copies the bound id into `message.labels` on `pre_send`, restores it from labels on `pre_execute`, clears on `post_execute`.
- **Outbound HTTP** — use `correlated_async_client(...)` from `engine/observability/http_client.py`. It registers a request hook that injects `X-Correlation-Id` from `ctx.get_correlation_id()` if the header is unset.

## Client IP resolution & audit

When the API runs behind a load balancer / reverse proxy,
`request.client.host` is the *proxy's* address, not the end user's.
[`resolve_client_ip`](../../engine/api/ip_utils.py) recovers the real
client from `X-Forwarded-For` — but only when the immediate peer is a
*trusted* proxy, so a client can't spoof its address by setting the
header. It is the single helper used wherever a record's `ip_address`
must be the genuine origin.

- **Config**: `NEXUS_TRUSTED_PROXIES` (CSV of hosts / CIDRs, default
  `""` = trust nobody → always reports the raw peer). Parsed once and
  memoized via `parse_proxy_networks`; IPv4-mapped IPv6
  (`::ffff:1.2.3.4`) is collapsed before matching so a dual-stack
  listener doesn't defeat an IPv4 trust entry.
- **Walk**: when the peer is trusted, the XFF chain is walked
  right-to-left and the first hop that is *not* a trusted proxy is
  reported (the spoof-resistant reading — each proxy appends the
  previous hop, so the rightmost untrusted entry is the origin).
- **DoS bound** (gh#1491): the walk inspects at most `MAX_XFF_HOPS`
  (16) entries, so a pathologically long, attacker-controlled header
  can't force unbounded parsing. Malformed proxy entries emit a
  structured `warning`, never a silent drop.

The resolved IP is an **audit field, not a log default**. Today the
consumer is **legal acceptance**: the `/api/v1/legal/accept` handlers
resolve the IP and store it on the immutable `LegalAcceptance` row
([`routes/legal.py`](../../engine/api/routes/legal.py) →
[`LegalAcceptance.ip_address`](../../engine/legal/models.py)), giving
consent records a defensible provenance. Auth events still record the
*raw peer* (`request.client.host`) — they are security signals, not
consent proofs, so trusting XFF there would weaken rather than
strengthen them. Don't add a new `ip_address` consumer without going
through `resolve_client_ip`.

## Level policy

| Level      | Use for                                                  |
| ---------- | -------------------------------------------------------- |
| `debug`    | Verbose traces, intermediate values. Off in prod.        |
| `info`     | Lifecycle: session start/stop, order submitted, strategy activated. |
| `warning`  | Recoverable: retry triggered, provider fallback, degraded mode. |
| `error`    | User-facing failure: exception bubbled to API, failed order. |
| `critical` | On-call wake-up: kill-switch fired, auth totally failing, data corruption detected. |

## Redaction

The `redact_processor` strips secrets before any record leaves the process.
Banned keys (case-insensitive, dashes normalized to underscores):

```
password, passwd, token, secret, api_key, authorization, credit_card,
card_number, ssn, access_token, refresh_token, client_secret,
private_key, session_token, cookie, set_cookie
```

Banned **value** patterns:

- `Bearer <anything>` — bearer tokens
- 3-segment dot-separated base64-ish strings ≥ 8 chars per segment — JWTs
- 13–19 digit blocks (with optional spaces / dashes) — card numbers
- Prefixed secrets: `sk*`, `xoxb*`, `xoxp*`, `ghp*`, `ghs*`, `AKIA*`

CI gate: `tests/observability/test_log_redaction_e2e.py` drives structlog
through the full processor chain and asserts no banned values reach the
wire. Add new patterns by editing `engine/observability/redact.py` and
extending the test fixtures.

## Error tracking (Sentry)

Sentry is the crash/error pipeline. It is initialised in the app
lifespan ([`engine/app.py`](../../engine/app.py) →
`_init_observability` → [`init_sentry()`](../../engine/observability/sentry.py))
right after logging/tracing, and torn down last in `_shutdown` via
`close_sentry()`. When `NEXUS_SENTRY_DSN` is empty (the default)
`init_sentry()` is a **graceful no-op**, so dev/test runs without a
backend and the process always starts.

> **Code:** `engine/observability/sentry.py` · reuses
> `engine/observability/redact.py` · **Issue:** [#1336](https://github.com/SevFle/nexus-trade-engine/issues/1336)
> **Tests:** `tests/observability/test_sentry.py` (PII scrubbing #1338)

### The PII guarantee is the same as logs

The whole point of routing Sentry through the redaction module is that
**an error event and a log line leaving the process carry the same
privacy guarantee**. `init_sentry()` registers a `before_send` hook
([`_before_send`](../../engine/observability/sentry.py)) that scrubs
every outbound event using the *exact same* patterns documented in the
[Redaction](#redaction) table above. There is no second redaction
vocabulary to keep in sync — extending `engine/observability/redact.py`
updates both pipelines.

Belt-and-suspenders: Sentry is also initialised with
`send_default_pii=False`, so the SDK attaches no user/server PII
server-side in the first place. The `before_send` hook covers the rest
(request payloads, breadcrumbs, context tags the integrations add).

### What `_before_send` scrubs

| Event field | Treatment |
|---|---|
| `contexts` | Recursive [`_scrub_dict`](../../engine/observability/redact.py) walk — catches any context key matching a banned name. |
| `breadcrumbs` | Scrubbed whether Sentry ships it as a `dict` or a `list`. |
| `request` (HTTP interface) | Handled by `_scrub_request` (see below). |

The Sentry HTTP integrations attach a `request` object holding `url`,
`method`, `query_string`, `headers`, and `data` (the body).
`_scrub_request` only touches the secret-bearing fields and passes
`url`/`method`/`env` through unchanged:

| `request` field | Treatment |
|---|---|
| `query_string` | Parsed **parameter by parameter** by `_redact_query_string`. A param whose decoded key is banned → value replaced wholesale with `REDACTED`; any other param's value still goes through `_scrub_string` (catches Bearer tokens, PANs, `sk*`/`ghp*`/…). |
| `headers` | Flat `{name: value}` dict scrubbed with `_scrub_dict` (covers `authorization`, `cookie`, `x-api-key`, …). |
| `data` (str) | Treated as **form-encoded** (`a=1&b=2`) and split param-by-param by `_redact_query_string`. |
| `data` (bytes) | Decoded, scrubbed the same way, **re-encoded** so the field stays `bytes`. |
| `data` (dict / list) | Recursive `_scrub_value` walk. |

> **Why `query_string` / form bodies get their own parser.**
> `_scrub_string`'s inline `key=value` rule uses a greedy `\S+` for the
> value, which lets a *single* sensitive param absorb its non-sensitive
> siblings across the `&` (e.g. `token=secret&page=1` would lose
> `page=1`). `_redact_query_string` splits on `&` / the first `=` so each
> parameter is considered in isolation — sensitive params are redacted,
> the rest are preserved verbatim. The same parser is reused for the
> `data` body so a form post like `password=secret&keep=ok` becomes
> `password=***REDACTED***&keep=ok`.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `NEXUS_SENTRY_DSN` | `""` | Empty → `init_sentry()` is a no-op (dev/test). |
| `NEXUS_SENTRY_TRACES_SAMPLE_RATE` | `0.0` | Performance-trace sampling. `0.0` disables; raise to capture traces. |
| (`release`) | `NEXUS_APP_VERSION` | Auto-set from app version — do **not** pass manually. |
| (`environment`) | `NEXUS_APP_ENV` | Auto-set from env. |

`close_sentry()` runs last in shutdown: it `flush(timeout=2)` so
buffered events drain before the process exits (a timeout emits a
`sentry.flush_timeout` warning rather than hanging shutdown), then
closes the client. It is safe to call when Sentry was never
initialised.

## Sampling

| Level                        | Rate setting                  | Default |
| ---------------------------- | ----------------------------- | ------- |
| `warning`/`error`/`critical` | always 100%                   | —       |
| `info`                       | `settings.log_sampling_info`  | `1.0`   |
| `debug`                      | `settings.log_sampling_debug` | `0.01`  |

Set via env: `NEXUS_LOG_SAMPLING_INFO=0.1` etc. Implemented in
`processors.sampling_filter` using `structlog.DropEvent`.

## Sinks

`NEXUS_LOG_SINK` selects the backend:

- `stdout` (default) — `logging.StreamHandler(sys.stdout)`
- `file` — `WatchedFileHandler` at `NEXUS_LOG_FILE_PATH` (default `logs/engine.log`)
- `otlp` — currently falls back to stdout until the OTel logs SDK is wired in.

## How to find logs

1. Identify the `correlation_id` in the user-visible error or HTTP response (`X-Correlation-Id` header).
2. Query the log sink for that id — every related record across HTTP, worker, and downstream calls carries it.
3. Cross-reference with `request_id` to scope to the specific HTTP request.

## Adding context to your code

```python
import structlog
from engine.observability import context as ctx

logger = structlog.get_logger(__name__)

async def place_order(portfolio_id: str, strategy_id: str, ...):
    ctx.bind_domain_context(portfolio_id=portfolio_id, strategy_id=strategy_id)
    logger.info("order.submitted", broker="alpaca", quantity=10)
```

The fields bound via `bind_domain_context` attach to every subsequent log
record in this asyncio task — no need to repeat them on each call.
