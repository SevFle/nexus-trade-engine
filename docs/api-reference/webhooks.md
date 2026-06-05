# Webhooks

Outbound HTTP subscriptions to engine events. Source:
[`engine/api/routes/webhooks.py`](../../engine/api/routes/webhooks.py).

The dispatcher in
[`engine/events/webhook_dispatcher.py`](../../engine/events/webhook_dispatcher.py)
is the single subscriber on `EventBus` today; it fans out to every
active `WebhookConfig` that subscribed to the relevant event
type. Retries are exponential with jitter, capped at
`max_retries` per config (default 3). 5xx responses trigger retry;
4xx responses mark the delivery `failed` immediately.

## Endpoints

### `POST /api/v1/webhooks`

Create a new subscription.

**Auth:** JWT or API key with **`trade`** scope. (Read-only keys
are rejected with `403`.)

**Request body** — `WebhookCreateRequest`:

```json
{
  "url": "https://example.com/hooks/nexus",
  "event_types": ["backtest.completed", "order.filled"],
  "custom_headers": { "X-Tenant": "acme" },
  "template": "generic",
  "max_retries": 3,
  "portfolio_id": null
}
```

`template` must be one of `generic`, `discord`, `slack`,
`telegram`. `portfolio_id` is optional — when set, the webhook
fires only for events on that portfolio.

**Response** `201 Created` — `WebhookResponse`:

```json
{
  "id": "<uuid>",
  "url": "https://example.com/hooks/nexus",
  "event_types": ["backtest.completed", "order.filled"],
  "template": "generic",
  "max_retries": 3,
  "is_active": true,
  "portfolio_id": null,
  "signing_secret": "<32-byte url-safe secret>"
}
```

The `signing_secret` is shown **only** on this response. Subsequent
reads return `null` for it. Store it now; if lost, delete and
recreate the webhook.

### `GET /api/v1/webhooks`

List the caller's webhooks (newest first).

### `PUT /api/v1/webhooks/{webhook_id}`

Patch any subset of fields. Same validation rules as create.

**Request body** — `WebhookUpdateRequest` (all fields optional):

```json
{ "event_types": ["backtest.completed"], "is_active": false }
```

### `DELETE /api/v1/webhooks/{webhook_id}`

Hard-delete the config. Existing `WebhookDelivery` rows are
cascade-deleted with it. `204 No Content`.

### `POST /api/v1/webhooks/{webhook_id}/test`

Fire a synthetic `test.event` payload through the dispatcher with
the webhook's normal signing + retry policy.

**Response** `200 OK` — `DeliveryResponse`:

```json
{
  "id": "<uuid>",
  "event_type": "test.event",
  "status": "delivered",
  "response_status": 200,
  "response_ms": 142,
  "attempts": 1,
  "error": null,
  "created_at": "2026-06-05T12:00:00Z",
  "delivered_at": "2026-06-05T12:00:00Z"
}
```

### `GET /api/v1/webhooks/{webhook_id}/deliveries`

Recent delivery audit for a webhook. Newest first.

**Query:** `limit` (default 50, capped 200).

**Response** `200 OK` — `DeliveryResponse[]`.

## Verifying deliveries

The dispatcher sends an `X-Nexus-Signature` header on every
delivery. Format: `sha256=<hex-hmac>` over the raw body, keyed
with the `signing_secret` shown on create. Verification (Python):

```python
import hmac, hashlib

expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
hmac.compare_digest("sha256=" + expected,
                    request.headers["X-Nexus-Signature"])
```

Reject mismatches with `401`; the dispatcher treats `401` as a
terminal failure (no retry).

## Templates

| Template   | Body shape                                          |
|------------|-----------------------------------------------------|
| `generic`  | `{ "event_type": "...", "payload": {...}, "ts": ... }` |
| `discord`  | Discord webhook execute payload (`{ "embeds": [...] }`) |
| `slack`    | Slack incoming webhook payload (`{ "blocks": [...] }`) |
| `telegram` | Telegram Bot API `sendMessage` payload                |

Template-specific transformations live in
[`engine/events/webhook_dispatcher.py`](../../engine/events/webhook_dispatcher.py).
Adding a new template requires updating `_VALID_TEMPLATES` in
`webhooks.py` *and* the rendering branch in the dispatcher.
