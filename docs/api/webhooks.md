# Webhooks API

Mounted at `/api/v1/webhooks`. Implementation:
`engine/api/routes/webhooks.py`. Dispatcher:
`engine/events/webhook_dispatcher.py`.

Webhooks let operators fan engine events out to external HTTP
endpoints (Discord, Slack, custom integrations). Each webhook has a
URL, an event-type subscription list, a signing secret (HMAC-SHA256),
and a payload template.

## POST /api/v1/webhooks

Create a webhook config. The `signing_secret` is generated server-side
and returned **exactly once** in the create response.

**Auth** — requires `trade` scope (or JWT session).

**Request body** `WebhookCreateRequest`:
```json
{
  "url": "https://hooks.zapier.com/...",
  "event_types": ["backtest.completed", "portfolio.updated"],
  "custom_headers": { "X-Tenant": "alpha" },
  "template": "generic",
  "max_retries": 3,
  "portfolio_id": "uuid-or-null"
}
```

| Field             | Type     | Default    | Notes                                  |
|-------------------|----------|------------|----------------------------------------|
| `url`             | HttpUrl  | required   | Validated by Pydantic                  |
| `event_types`     | string[] | `[]`       | See event list below                   |
| `custom_headers`  | object   | `{}`       | Sent with every delivery               |
| `template`        | string   | `"generic"`| One of {generic, discord, slack, telegram} |
| `max_retries`     | int      | 3          | 1–10                                   |
| `portfolio_id`    | UUID     | null       | Scope to one portfolio                 |

**Response** `WebhookResponse` (201):
```json
{
  "id": "uuid",
  "url": "https://...",
  "event_types": ["backtest.completed"],
  "template": "generic",
  "max_retries": 3,
  "is_active": true,
  "portfolio_id": null,
  "signing_secret": "token-urlsafe(32)"
}
```

`signing_secret` is the only field that's write-once. Subsequent
reads (GET, PUT) return `null`.

## GET /api/v1/webhooks

List the caller's webhooks, newest-first.

**Response** — `list[WebhookResponse]` (without `signing_secret`).

## PUT /api/v1/webhooks/{webhook_id}

Update fields. All keys are optional; only supplied keys are changed.

**Request body** `WebhookUpdateRequest` — same shape as create but
every field optional, plus `is_active: bool` for soft-pause.

**Response** — `WebhookResponse`.

## DELETE /api/v1/webhooks/{webhook_id}

Hard-delete. Cascades to delivery history.

**Response** — `204 No Content`.

## POST /api/v1/webhooks/{webhook_id}/test

Fire a `test.event` payload to the configured URL using the real
dispatcher.

**Response** `DeliveryResponse`:
```json
{
  "id": "uuid",
  "event_type": "test.event",
  "status": "delivered | failed | pending",
  "response_status": 200,
  "response_ms": 142,
  "attempts": 1,
  "error": null,
  "created_at": "2026-06-05T12:00:00Z",
  "delivered_at": "2026-06-05T12:00:00Z"
}
```

The dispatcher retries on 5xx and network errors; gives up on 4xx.

## GET /api/v1/webhooks/{webhook_id}/deliveries

Delivery history for a webhook.

**Query params** — `limit` (default 50, max 200).

**Response** — `list[DeliveryResponse]`, newest-first.

## Event types

Defined in `engine/events/bus.py:EventType`. Subscribable strings:

| Category  | Events                                                              |
|-----------|---------------------------------------------------------------------|
| Market    | `market.data.update`, `market.open`, `market.close`                 |
| Signal    | `signal.emitted`, `signal.batch`                                    |
| Order     | `order.created`, `order.validated`, `order.submitted`, `order.filled`, `order.rejected`, `order.failed` |
| Portfolio | `portfolio.updated`, `position.opened`, `position.closed`           |
| Strategy  | `strategy.loaded`, `strategy.unloaded`, `strategy.error`            |
| Risk      | `risk.warning`, `risk.circuit_breaker`                              |
| System    | `engine.started`, `engine.stopped`, `backtest.started`, `backtest.completed` |

## Signing

Each delivery POST carries:

- `X-Nexus-Signature: sha256=<hex-hmac-sha256-of-body>`
- `X-Nexus-Event: <event_type>`
- `X-Nexus-Delivery: <delivery_uuid>`

The signature is computed over the exact body bytes. Verify on the
receiver:

```python
import hmac, hashlib
expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, received_sig.removeprefix("sha256=")):
    raise Unauthorized
```

## Templates

| Template   | Shape                                                   |
|------------|---------------------------------------------------------|
| `generic`  | `{"event": "...", "data": {...}, "timestamp": "..."}`   |
| `discord`  | Discord webhook payload with embedded fields            |
| `slack`    | Slack incoming webhook with blocks                      |
| `telegram` | Bot sendMessage payload                                 |

Renderer: `engine/events/webhook_dispatcher.py:render_template`.

## Retries and back-off

The dispatcher retries `max_retries` times on 5xx or network errors
with exponential back-off. There is no public knob for the
back-off curve; operators who need different timing should file an
issue.
