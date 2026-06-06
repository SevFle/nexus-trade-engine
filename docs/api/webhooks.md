# Webhooks API

Base path: `/api/v1/webhooks`. Source:
[`engine/api/routes/webhooks.py`](../../engine/api/routes/webhooks.py),
[`engine/events/webhook_dispatcher.py`](../../engine/events/webhook_dispatcher.py).

Outbound webhook subscriptions. Each config tells the engine: "for
these `event_types`, POST this body to that URL, signed with this
secret, retrying up to N times on 5xx." Deliveries are persisted to
`webhook_deliveries` for audit.

## Templates

Built-in body templates:

| Template   | When to use                                                       |
|------------|-------------------------------------------------------------------|
| `generic`  | Default. Sends the raw event payload as JSON.                     |
| `discord`  | Discord webhook format (embeds).                                  |
| `slack`    | Slack incoming webhook format.                                    |
| `telegram` | Telegram bot message format.                                      |

Adding a template = extending `render_template` in
`engine/events/webhook_dispatcher.py` and adding it to the
`_VALID_TEMPLATES` set in `routes/webhooks.py`.

## Endpoints

### `POST /api/v1/webhooks`

Create a new webhook config. The `signing_secret` is generated
server-side and returned **once**, in the response body.

**Auth**: Bearer JWT *or* API key with `trade`+ scope
(`require_api_scope("trade")`).

**Request body**:

```json
{
  "url": "https://example.com/hooks/nexus",
  "event_types": ["backtest.completed", "backtest.failed"],
  "custom_headers": { "X-Tenant": "acme" },
  "template": "generic",
  "max_retries": 5,
  "portfolio_id": "uuid"
}
```

| Field            | Type              | Default     | Notes                                  |
|------------------|-------------------|-------------|----------------------------------------|
| `url`            | string (URL)      | required    | HttpUrl-validated.                     |
| `event_types`    | array of strings  | `[]`        | Empty = subscribe to all events.       |
| `custom_headers` | object            | `{}`        | Added to every outbound POST.          |
| `template`       | string            | `"generic"` | One of `{generic, discord, slack, telegram}`. |
| `max_retries`    | int               | `3`         | 1–10.                                  |
| `portfolio_id`   | UUID              | null        | Scope the webhook to one portfolio.    |

**Response**: `201 Created`:

```json
{
  "id": "uuid",
  "url": "https://example.com/hooks/nexus",
  "event_types": ["backtest.completed", "backtest.failed"],
  "template": "generic",
  "max_retries": 5,
  "is_active": true,
  "portfolio_id": "uuid-or-null",
  "signing_secret": "generated-secret-shown-once"
}
```

`400` if `template` is not in the allow-list.

### `GET /api/v1/webhooks`

List the caller's webhook configs. Always excludes `signing_secret`.

**Auth**: Bearer JWT or API key.

**Response**: `200 OK` — array of `WebhookResponse` (same shape as
create, but `signing_secret` is `null`).

### `PUT /api/v1/webhooks/{webhook_id}`

Patch a webhook. Any subset of fields may be supplied; omitted fields
keep their current value. `signing_secret` cannot be rotated via this
endpoint — delete and recreate the webhook to rotate.

**Auth**: Bearer JWT or API key.

**Request body** (any subset):

```json
{
  "url": "...",
  "event_types": [...],
  "custom_headers": {...},
  "template": "...",
  "max_retries": 5,
  "is_active": false
}
```

**Response**: `200 OK` → `WebhookResponse`.

### `DELETE /api/v1/webhooks/{webhook_id}`

Hard-delete a webhook config. Cascades to its deliveries.

**Response**: `204 No Content`. `404` if not owned by the caller.

### `POST /api/v1/webhooks/{webhook_id}/test`

Send a synthetic `test.event` payload. Useful for verifying the
endpoint during onboarding.

**Auth**: Bearer JWT or API key.

**Response**: `200 OK`:

```json
{
  "id": "delivery-uuid",
  "event_type": "test.event",
  "status": "delivered",
  "response_status": 200,
  "response_ms": 142,
  "attempts": 1,
  "error": null,
  "created_at": "2026-06-06T12:00:00Z",
  "delivered_at": "2026-06-06T12:00:00Z"
}
```

### `GET /api/v1/webhooks/{webhook_id}/deliveries`

List recent deliveries for one webhook. Ordered by `created_at desc`.

**Auth**: Bearer JWT or API key.

**Query params**:

| Name   | Type | Default | Notes                |
|--------|------|---------|----------------------|
| `limit`| int  | 50      | Capped at 200.       |

**Response**: `200 OK` — array of `DeliveryResponse`.

## Signing

Every outbound POST carries:

```
X-Nexus-Signature: t=<unix-ts>,v1=<hmac-sha256-hex>
```

Recipients verify by:

1. Splitting the header on `,` to extract `t` and `v1`.
2. Computing `HMAC-SHA256(signing_secret, "{t}.{raw_body}")`.
3. Comparing in constant time to `v1`.
4. Rejecting if `t` is more than ~5 minutes stale (defence against
   replay).

The dispatcher's exact algorithm is in
[`engine/events/webhook_dispatcher.py`](../../engine/events/webhook_dispatcher.py).

## Retry semantics

The dispatcher retries on 5xx responses and network errors with
exponential back-off, up to `max_retries` attempts. **4xx responses
are not retried** — they are treated as "the operator's server
deliberately refused the delivery."

A delivery's lifecycle is:

```
pending → delivered   (success — 2xx)
pending → failed      (max retries exhausted)
pending → dead        (unrecoverable: bad URL, DNS failure, etc.)
```
