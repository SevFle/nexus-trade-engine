# Common issues — debug runbook

This runbook covers problems that don't map cleanly to one SLO. For
SLO-driven alerts (API availability, latency, MFA, webhook delivery,
task pipeline, backtest submit), use the matching runbook in this
directory.

Each entry: **Symptom → First 60 seconds → Triage → Likely fix**.

---

## Symptom: every protected route returns `403`

### First 60 seconds

```bash
# 1. Are legal docs accepted?
curl -H "Authorization: Bearer $TOKEN" \
  https://api.example.com/api/v1/legal/acceptances/me | jq
```

If `acceptances` is empty or missing one of the
`requires_acceptance=true` docs, that's the cause — most non-GET
routes (`/backtest/*`, `/portfolio/*`, `/market-data/*`,
`/scoring/*`, `/strategies/*`, `/marketplace/*`, `/reference/*`)
gate on `require_legal_acceptance` (see
[`engine/legal/dependencies.py`](../../../engine/legal/dependencies.py)).

### Triage

- Check `legal_documents` for documents where `requires_acceptance=true`
  and the user has no matching `(document_slug, document_version)`
  row in `legal_acceptances`.
- A new release shipped a new `current_version` for a document
  (manual or via `sync_legal_documents`) — acceptances for the old
  version don't satisfy the new one.

### Likely fix

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"acceptances":[{"slug":"terms-of-service","version":"2.0.0"},...]}' \
  https://api.example.com/api/v1/legal/accept
```

For operator-wide rollouts, frontend should pop the consent flow on
the next session.

---

## Symptom: API key returns `403` for a write call

### First 60 seconds

```bash
curl -H "X-API-Key: $KEY" https://api.example.com/api/v1/auth/me   # works → key valid
curl -X POST -H "X-API-Key: $KEY" ... /api/v1/portfolio/           # 403?
```

### Triage

Check the key's `scopes` via `GET /api/v1/auth/api-keys`. The scope
hierarchy is `admin > trade > read` (see
[`api/auth/dependency.py:160`](../../../engine/api/auth/dependency.py#L160)).
A `read`-scoped key cannot POST. Routes that mutate state declare
`Depends(require_api_scope("trade"))` (e.g. portfolio create, webhook
create).

### Likely fix

Issue a new key with `scopes: ["trade"]` (or `["admin"]` if needed).
Scopes cannot be edited on an existing key — revoke and reissue.

---

## Symptom: refresh-token refresh returns `401 "Token reuse detected"`

### First 60 seconds

This is **not** a bug — it's the replay-detection guard firing
([`routes/auth.py:208-219`](../../../engine/api/routes/auth.py#L208)).
A refresh token was used twice. The engine revoked every active
session for that user as a defense.

### Triage

- A client (mobile app, multiple tabs, parallel CI jobs) cached the
  refresh token and reused it after rotation. This is the most common
  cause.
- A backup-restore of client state replayed an old token.
- Actual token theft — rare but possible.

### Likely fix

The user must log in again from scratch. Fix the client:
`POST /refresh` returns a *new* `refresh_token` — the *old* one is
dead the moment it leaves the server. Persist the rotation
transactionally.

---

## Symptom: MFA enroll → `503 Service Unavailable`

### First 60 seconds

`NEXUS_MFA_ENCRYPTION_KEY` is empty in this environment. The MFA
service refuses to enroll (see
[`mfa_service.py`](../../../engine/api/auth/mfa_service.py)) because
there's no key to encrypt the TOTP secret with.

### Triage

```bash
docker compose exec app printenv NEXUS_MFA_ENCRYPTION_KEY
```

Empty / unset → cause confirmed.

### Likely fix

Generate a Fernet key and set it:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# export NEXUS_MFA_ENCRYPTION_KEY=<that string>
# restart app
```

Existing users with `mfa_enabled=true` are unaffected (their stored
secrets were encrypted with the previous key — keep `*_PREVIOUS`
keys during rotation).

---

## Symptom: `GET /market-data/{symbol}/bars` returns `503`

### First 60 seconds

```bash
curl https://api.example.com/health/providers | jq
```

If any registered provider is `down`, the registry will fail through
to the next one and return `503` if none can serve.

### Triage

- Yahoo default provider hit a rate limit (most common cause — Yahoo
  is unauthenticated and throttles aggressively).
- `polygon` / `alpaca` / etc. configured but credentials expired.
- Provider config YAML has a typo and `configure_from_file` failed
  silently at startup (the lifespan logs
  `data_provider.bootstrap.failed` but does not abort).

### Likely fix

- Check `structlog` for `data_provider.error.*` events around the
  request timestamp.
- If Yahoo-only, try a less chatty interval. If persistent, register
  a paid provider in `config/data_providers.yaml` and set
  `NEXUS_DATA_PROVIDERS_CONFIG`.
- Restart the app after fixing the YAML — bootstrap only runs at
  startup.

---

## Symptom: webhook deliveries stuck at `pending`

### First 60 seconds

```sql
SELECT status, count(*) FROM webhook_deliveries
WHERE created_at > now() - interval '15 minutes'
GROUP BY status;
```

### Triage

- The dispatcher runs in the **web process** today (no separate
  worker task). If the web process is busy with backtests (see
  [known-limitations.md](../../known-limitations.md) — backtests also
  run in-process), deliveries can lag.
- Target URL is timing out and the dispatcher is in a retry backoff
  window.
- A new template was added in code but not in `_VALID_TEMPLATES` in
  [`routes/webhooks.py`](../../../engine/api/routes/webhooks.py#L30).

### Likely fix

```bash
# Force a test delivery to the suspect endpoint
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://api.example.com/api/v1/webhooks/$ID/test
```

If that also stalls, the dispatcher itself is wedged — restart the
web process. Inspect target URL response codes in
`webhook_deliveries.response_status`; 4xx are not retried, 5xx are.

---

## Symptom: process RSS climbs steadily

### First 60 seconds

```bash
curl https://api.example.com/api/v1/system/status | jq '.counts'
```

### Triage

- The in-memory `_backtest_results` dict
  ([`routes/backtest.py:22`](../../../engine/api/routes/backtest.py#L22))
  holds full equity curves for 1 hour. A burst of backtests against
  long histories can grow this to GBs.
- `MarketStateBuilder` retains Polars frames for the duration of a
  backtest; if the web process is running multiple backtests
  concurrently, peak RSS = N × frame size.

### Likely fix

- Restart the process to clear `_backtest_results`. (Safe today — the
  dict is not the canonical store; the `backtest_results` table is.
  See [known-limitations.md](../../known-limitations.md).)
- Long-term: move backtests to the TaskIQ worker.

---

## Symptom: every request logs `auth.token_replay_detected`

### First 60 seconds

A client is in a hot loop, replaying the same refresh token. Every
refresh revokes every other session for the user, the next request
fails auth, the client retries — another replay — and so on.

### Triage

- Look at the user_id in the log. Confirm with the user / device.
- Common: a script saved a refresh token to disk and is being run by
  cron across multiple hosts.

### Likely fix

Invalidate the user's sessions server-side
(`UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = ?`),
then have the user re-authenticate once. Fix the client to honor the
rotated refresh token.

---

## Escalation

For anything not covered here:

1. Search `structlog` output for `*.failed` / `*.error` events
   around the incident start time.
2. Check Sentry (`NEXUS_SENTRY_DSN`) for new issues.
3. Cross-reference with the SLO-specific runbooks in this directory.
4. If the issue is a bug, open a GitHub issue tagged `bug`; if it's
   a runbook gap, tag `docs` and update this file in the same PR as
   the fix.
