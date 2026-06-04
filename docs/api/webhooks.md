# Webhooks

> **Base path:** `/api/v1/webhooks`
>
> **Source:** [`engine/api/routes/webhooks.py`](../../engine/api/routes/webhooks.py),
> [`engine/events/webhook_dispatcher.py`](../../engine/events/webhook_dispatcher.py),
> [`engine/events/bus.py`](../../engine/events/bus.py)

## What webhooks are

The engine emits domain events whenever something interesting happens
(backtest completed, strategy activated, scoring snapshot written, …).
Subscribers in the engine's `EventBus` listen to those events. The
**webhook dispatcher** is the subscriber that fans the event out to
every `WebhookConfig` row that subscribed to that event type.

Webhooks are per-user (with optional `portfolio_id` pinning) and
support four payload templates:

| Template | Shape |
|----------|-------|
| `generic` | Raw event JSON, signed with HMAC-SHA256. |
| `discord` | Discord webhook shape (embeds, username, avatar_url). |
| `slack` | Slack incoming-webhook shape (text, blocks). |
| `telegram` | Bot-message shape (`chat_id`, `text`). |

The template set is the `_VALID_TEMPLATES` constant in
[`engine/api/routes/webhooks.py`](../../engine/api/routes/webhooks.py).

## Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `` | Bearer or API key with `trade` scope | **201** `WebhookResponse`. The `signing_secret` field is populated **only in this response** — save it; it is never returned again. |
| `GET` | `` | Bearer | Lists caller's webhooks. `signing_secret` is null. |
| `PUT` | `/{webhook_id}` | Bearer | Partial update; only the supplied fields are changed. |
| `DELETE` | `/{webhook_id}` | Bearer | **204**. Hard-delete (deliveries stay, since the FK is `ON DELETE CASCADE` on `webhook_configs` — see limitation below). |
| `POST` | `/{webhook_id}/test` | Bearer | Sends a synthetic `test.event` payload. Returns the resulting `DeliveryResponse`. Useful for verifying templates. |
| `GET` | `/{webhook_id}/deliveries` | Bearer | Paginated delivery history (`limit` capped at 200). |

## Request / response shapes

### `WebhookCreateRequest`

```json
{
  "url": "https://hooks.example.com/nexus",
  "event_types": ["backtest.completed", "scoring.snapshot_written"],
  "custom_headers": { "X-Tenant": "alpha" },
  "template": "generic",
  "max_retries": 3,
  "portfolio_id": "00000000-0000-0000-0000-000000000000"
}
```

- `url`: HTTPS URL (validated by Pydantic `HttpUrl`).
- `event_types`: empty list = "subscribe to everything". The full
  vocabulary lives in `engine/events/bus.py` (search for `publish(`).
- `max_retries`: 1..10. The dispatcher retries on 5xx + network
  errors; 4xx is terminal.
- `portfolio_id`: optional — if set, only events scoped to that
  portfolio fire.

### `WebhookResponse`

```json
{
  "id": "00000000-0000-0000-0000-000000000000",
  "url": "https://hooks.example.com/nexus",
  "event_types": ["backtest.completed"],
  "template": "generic",
  "max_retries": 3,
  "is_active": true,
  "portfolio_id": null,
  "signing_secret": "uH1J...only-on-create"
}
```

### `DeliveryResponse`

```json
{
  "id": "...",
  "event_type": "backtest.completed",
  "status": "delivered",
  "response_status": 200,
  "response_ms": 142,
  "attempts": 1,
  "error": null,
  "created_at": "2026-06-04T12:34:56Z",
  "delivered_at": "2026-06-04T12:34:56Z"
}
```

`status` is `pending | delivered | failed | retrying`. Terminal states
are `delivered` and `failed`; the SLO counts only terminal deliveries
(see [../operations/slos.md](../operations/slos.md)).

## Verifying signatures (generic template)

The dispatcher signs every body with HMAC-SHA256 using the
`signing_secret`. Verify on the receiver:

```python
import hmac, hashlib

sig = request.headers["X-Nexus-Signature"]
mac = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
hmac.compare_digest(sig, mac)
```

Discord / Slack / Telegram templates do **not** include the signature
header because those transports do not allow custom headers on
incoming webhooks. Use a per-route secret in the URL path for those.

## Operational notes

- **Delivery rows are immutable.** They exist for audit; never UPDATE
  them. The dispatcher only ever inserts.
- **Hard delete is intentional.** `webhook_configs` cascades to
  `webhook_deliveries`. If you need a soft-delete (e.g. for compliance
  retention) keep `is_active=false` and never call DELETE. This is a
  known limitation listed in [../limitations.md](../limitations.md).
- **Latency budget:** the dispatcher targets sub-second p99 dispatch.
  Slow receivers (multi-second timeouts) consume worker capacity fast
  — monitor the per-webhook latency panel in the
  [runbook](../operations/runbooks/webhook-delivery.md).
- **No outbound queue today.** Deliveries happen in-process. If a
  worker is restarted mid-flight, in-progress deliveries are retried
  from the `pending` rows on the next tick. Long term we will move
  dispatch to TaskIQ; tracked in [../limitations.md](../limitations.md).
