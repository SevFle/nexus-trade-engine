# Webhooks API

CRUD plus test and delivery-audit endpoints for outbound webhooks.
Implementation: [`engine/api/routes/webhooks.py`](../../engine/api/routes/webhooks.py).

A `WebhookConfig` is an outbound subscription: when one of the
subscribed `event_types` fires, the
[`WebhookDispatcher`](../../engine/events/webhook_dispatcher.py) POSTs
a templated payload to `url` with HMAC-SHA256 signature, retries up to
`max_retries` times on 5xx, and writes a `WebhookDelivery` audit row
for every attempt.

`POST /webhooks` is the only call that returns the `signing_secret`.
Reads (`GET`) and updates (`PUT`) never echo it. Treat the secret like
a password — if leaked, delete + recreate the webhook.

## Endpoint summary

| Method | Path | Auth / scope | Purpose |
|---|---|---|---|
| `POST`   | `/api/v1/webhooks`                       | JWT or `trade`-scope API key | Create |
| `GET`    | `/api/v1/webhooks`                       | JWT/API key                  | List caller's webhooks |
| `PUT`    | `/api/v1/webhooks/{webhook_id}`          | JWT/API key                  | Update fields |
| `DELETE` | `/api/v1/webhooks/{webhook_id}`          | JWT/API key                  | Delete + revoke |
| `POST`   | `/api/v1/webhooks/{webhook_id}/test`     | JWT/API key                  | Fire a `test.event` synchronously |
| `GET`    | `/api/v1/webhooks/{webhook_id}/deliveries` | JWT/API key                | Delivery audit trail (paginated) |

## Schemas

```python
class WebhookCreateRequest(BaseModel):
    url: HttpUrl
    event_types: list[str] = []                # ["backtest.completed", ...]
    custom_headers: dict[str, str] = {}
    template: str = "generic"                  # generic|discord|slack|telegram
    max_retries: int = 3                       # 1-10
    portfolio_id: UUID | None = None           # scope to one portfolio

class WebhookUpdateRequest(BaseModel):
    url: HttpUrl | None = None
    event_types: list[str] | None = None
    custom_headers: dict[str, str] | None = None
    template: str | None = None
    max_retries: int | None = None
    is_active: bool | None = None

class WebhookResponse(BaseModel):
    id: UUID
    url: str
    event_types: list[str]
    template: str
    max_retries: int
    is_active: bool
    portfolio_id: UUID | None
    signing_secret: str | None = None          # only on POST

class DeliveryResponse(BaseModel):
    id: UUID
    event_type: str
    status: str                                # pending|delivered|failed
    response_status: int | None
    response_ms: int | None
    attempts: int
    error: str | None
    created_at: str
    delivered_at: str | None
```

## Templates

`template` selects the payload shape that the dispatcher renders.
Allowed values today: `generic`, `discord`, `slack`, `telegram`
(defined in `_VALID_TEMPLATES` in the route file and mirrored in
`engine/events/webhook_dispatcher.py:render_template`). Other shapes
land via PR — extend the renderer and the allow-list together.

## Examples

```bash
# Create
curl -X POST http://localhost:8000/api/v1/webhooks \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{
        "url": "https://hooks.slack.com/services/...",
        "event_types": ["backtest.completed", "task.failed"],
        "template": "slack",
        "max_retries": 5
      }'
# => {id, url, ..., signing_secret: "<shown once>"}

# Update
curl -X PUT http://localhost:8000/api/v1/webhooks/<id> \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"is_active": false}'

# Fire a test
curl -X POST http://localhost:8000/api/v1/webhooks/<id>/test \
  -H 'authorization: Bearer <access>'
# => DeliveryResponse{status: delivered|failed, response_status: 200, ...}

# Inspect delivery history (default 50, max 200)
curl 'http://localhost:8000/api/v1/webhooks/<id>/deliveries?limit=100' \
  -H 'authorization: Bearer <access>'
```

## Verification (receiver side)

The dispatcher signs every outbound request with HMAC-SHA256 using the
webhook's `signing_secret`. Receivers verify:

```
signature = hex(HMAC-SHA256(signing_secret, raw_request_body))
```

The signature is sent in the `X-Nexus-Signature` header. The exact
header name + payload shape per template is in
[`engine/events/webhook_dispatcher.py`](../../engine/events/webhook_dispatcher.py).

## Errors

| Status | When |
|---|---|
| `400` | Invalid template name; `max_retries` outside 1-10. |
| `401` | Missing/invalid credential. |
| `403` | API key missing `trade` scope on `POST`; or attempting to access another user's webhook. |
| `404` | Webhook does not exist (or belongs to another user). |

## See also

- [Webhook delivery runbook](../operations/runbooks/webhook-delivery.md)
- [`docs/operations/slos.md`](../operations/slos.md) — webhook delivery SLO
- [Architecture — events](../architecture/overview.md#event-flow)
