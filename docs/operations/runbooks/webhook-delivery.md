# Runbook — Webhook delivery

**Alerts**: `WebhookDeliverySlowBurn`, `WebhookDeliveryBudgetExhaustion`

**SLO**: 99% of webhook deliveries reach `delivered` (vs. `failed`) at
their terminal state, over 28 days
([slos.md](../slos.md#critical-user-journeys)).

## What this means

Outbound webhooks fired by the engine are landing in `failed` state
more often than the SLO budget allows. The dispatcher in
[`engine/events/webhook_dispatcher.py`](../../../engine/events/webhook_dispatcher.py)
retries on 5xx and network errors, but gives up on 4xx — so this alert
fires when *terminal* failures rise, not just when individual attempts
fail. Ticket severity.

## First 60 seconds

1. Open the **Nexus / Webhook pipeline** dashboard. The "Terminal
   deliveries per second by outcome" panel should show whether
   `failed` has overtaken `delivered`.
2. Look at "Failed (last 1h)" — single-digit failures over 1h with
   100k deliveries is normal noise; double-digits or above is the
   alert speaking.

## Triage

- **Is one webhook config dominating the failures?** Pull the last 50
  rows of `webhook_deliveries` ordered by `created_at desc` filtered
  on `status='failed'` and group by `webhook_id`. A single registry of
  bad endpoints will dominate.
- **Are 4xx terminations or 5xx exhaustion the cause?** Inspect the
  `error` column on the failed delivery rows. The dispatcher's
  `_RETRYABLE_STATUS` set is `{408, 425, 429, 500, 502, 503, 504}`; any
  other 4xx terminates immediately with `error` containing
  "non-retryable". Sustained 5xx → "max retries exceeded".
- **Did anything change in `webhook_dispatcher.py` recently?** Check
  recent merges; the structlog `event` kwarg collision was a class of
  bug — pass `event_type=` not `event=`.

## Common causes

- **Operator mis-configured a webhook endpoint** — wrong URL, expired
  signing secret on the receiver side, etc. The endpoint returns 400
  / 401 / 404 and the dispatcher gives up. Reach out to the
  operator / contact the user; do not change retry behaviour.
- **Receiver outage** — a Slack / Discord / Telegram outage will spike
  failed-because-5xx terminations. The engine retries 3 times by
  default (`max_retries=3` on `WebhookConfig`); if the outage lasts
  longer the dispatcher correctly marks them failed.
- **DNS or egress issue** — `httpx.ConnectError` errors in the logs.
  Check egress firewall / proxy configuration.
- **Custom-headers regression** — operator added a custom header that
  the receiver rejects. Inspect `webhook_configs.custom_headers`.
- **Signing-secret rotation gone wrong** — receiver rejects with 401
  because they verified an HMAC against the old secret. Coordinate the
  secret rotation with the receiver next time.

## Escalation

Webhook failures rarely require pagers; if a single high-traffic
webhook starts terminating most deliveries, ping the operator who owns
that endpoint. Don't quietly disable a customer's webhook — open a
ticket.

## Post-incident

- If a class of failure was missed by the retry classifier, propose a
  change to `_RETRYABLE_STATUS` in a follow-up PR with tests.
- If a customer's webhook needs to be disabled to stop the bleeding,
  prefer setting `is_active=false` over deleting the row — preserves
  the audit trail.
