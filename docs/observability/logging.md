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
