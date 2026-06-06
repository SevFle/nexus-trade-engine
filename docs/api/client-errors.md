# Client error reporting

Base path: `/api/v1/client`. Source:
[`engine/api/routes/client_errors.py`](../../engine/api/routes/client_errors.py).

The frontend's top-level `ErrorBoundary` reports unhandled exceptions
here so we can correlate browser-side failures with the audit trail.
The endpoint is **not** auth-gated: an authenticated session is
exactly when error reporting is most likely to fail. Abuse is bounded
by a tight per-route rate limit (30 req/min/IP).

## Endpoint

### `POST /api/v1/client/errors`

Submit a client-side error report.

**Auth**: none.

**Request body**:

```json
{
  "message": "TypeError: cannot read property 'map' of undefined",
  "stack": "at Foo.render (Foo.jsx:42)\nat ...",
  "component_stack": "<Foo>\n<Bar>",
  "url": "https://example.com/portfolios/uuid",
  "user_agent": "Mozilla/5.0 ...",
  "error_id": "uuid-v4"
}
```

| Field             | Type   | Constraints                              |
|-------------------|--------|------------------------------------------|
| `message`         | string | 1–64 KiB.                                |
| `stack`           | string | ≤ 64 KiB.                                |
| `component_stack` | string | ≤ 64 KiB.                                |
| `url`             | string | ≤ 2048 chars. Query + fragment stripped before logging. |
| `user_agent`      | string | ≤ 1024 chars.                            |
| `error_id`        | UUID v4 string | Optional. If supplied, must parse as a UUID; arbitrary opaque strings are rejected so an attacker can't collide with a real server correlation id. |

**Response**: `201 Created`:

```json
{ "error_id": "uuid-v4" }
```

The `error_id` is the one the caller supplied (or one generated
server-side if absent). Operators search the structured-log stream
for this id to find the matching log line.

## Sanitization

Before logging, the endpoint strips:

- ANSI CSI / OSC escape sequences (defence against terminal-escape
  injection when humans tail logs).
- ASCII control characters (CR, LF, NUL, …). Tabs are preserved.
- The query string and fragment of `url` (auth tokens frequently end
  up in query strings; the boundary doesn't know to redact them).

The sanitiser is the only defence-in-depth layer — structlog's JSON
renderer already escapes these — but it makes the endpoint safe even
if the operator switches to a plain-text log sink later.

## Log shape

The endpoint emits a single structured log at `ERROR` level:

```
client.error error_id=<uuid> message=<scrubbed> stack=<scrubbed>
              component_stack=<scrubbed> url=<scheme+host+path>
              user_agent=<scrubbed> client_host=<ip>
```

`event_type` would conflict with structlog's reserved `event` kwarg;
the route emits `client.error` instead.

## Persistence

This slice of the feature does not persist to the database — only
emits the log line. A follow-up PR will sink the structlog stream
into a queryable store. Until then, search via the log aggregator
(Loki, CloudWatch, etc.).
