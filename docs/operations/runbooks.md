# Operations runbooks

Diagnosis playbooks for the failures you will actually see in
production. Each entry is shaped the way an on-call engineer thinks:
**Symptom → Probable cause → Diagnostic commands → Remediation →
Post-incident follow-up**.

Detailed per-issue runbooks (with metric thresholds and exact alert
names) live alongside this file under `runbooks/`. This file is the
engineer's index; the individual runbooks are the deep dives.

## Index

| Runbook | Symptom | Sev |
|---------|---------|-----|
| [Backtest submissions stuck](#1-backtest-submissions-stuck) | `POST /api/v1/backtest/run` returns 202 but results never appear | High |
| [Refresh token replay storm](#2-refresh-token-replay-storm) | Spike in `auth.token_replay_detected` log events | High |
| [Webhook deliveries failing](#3-webhook-deliveries-failing) | `webhook.deliveries.terminal_total{outcome="failed"}` rising | Med |
| [Data provider 503s](#4-data-provider-503s) | `GET /market-data/*/bars` returning 503 | High |
| [DB connection exhaustion](#5-db-connection-exhaustion) | New requests failing with `OperationalError: queuepool` | Critical |
| [Valkey OOM](#6-valkey-oom) | Rate limiter returning 429 to everyone | High |
| [MFA verify failures](#7-mfa-verify-failures) | `auth.mfa_verify_failed` rising | Med |
| [Backtest runner OOM](#8-backtest-runner-oom) | Backtest process killed mid-run | Med |
| [Legal-acceptance gate misfire](#9-legal-acceptance-gate-misfire) | All users suddenly 403 on backtest routes | Med |

The existing per-issue runbooks at `runbooks/*.md` cover additional
scenarios; this file is the consolidated overview.

---

## 1. Backtest submissions stuck

**Symptom.** `POST /api/v1/backtest/run` returns `202 Accepted`, but
`GET /api/v1/backtest/results/{id}` returns `running` indefinitely
(or `failed` with no error context).

**Likely causes.**

1. TaskIQ worker not running.
2. Worker running but cannot reach Valkey.
3. Worker running but cannot reach the database.
4. Worker reached the per-eval sandbox memory cap.

**Diagnose.**

```bash
# 1. Is the worker container up?
docker compose ps worker

# 2. Worker logs (last 100 lines)
docker compose logs --tail=100 worker

# 3. Valkey queue depth
docker compose exec valkey valkey-cli LLEN taskiq:broker:default

# 4. Active worker tasks
docker compose exec valkey valkey-cli KEYS "taskiq:*"
```

If the queue is non-empty and the worker is idle, the worker lost its
broker connection — restart it.

If `worker` logs show `MemoryError` or `RLIMIT_AS exceeded`, the
sandbox memory cap (`manifest.resources.max_memory`) is too low for
the strategy's universe. Raise it in the manifest, or reduce the
universe size.

**Remediate.**

```bash
# Restart the worker (idempotent; in-flight jobs are reclaimed)
docker compose restart worker

# If the queue is corrupt (rare), flush it
docker compose exec valkey valkey-cli DEL taskiq:broker:default
```

**Follow-up.** If `LLEN` is consistently >0 under normal load, the
worker concurrency is too low for the workload — raise
`NEXUS_WORKER_CONCURRENCY`. If `LLEN` grows unboundedly, the strategy
is wedging — look at the sandbox timeout enforcement.

---

## 2. Refresh token replay storm

**Symptom.** Spike in `auth.token_replay_detected` log lines;
affected users complain of being logged out everywhere.

**Likely cause.**

A client is using a refresh token more than once. Possibilities:

1. Buggy client (most common) — refresh logic is racing on parallel
   requests.
2. Token leak — the user's refresh token was stolen and the attacker
   is using it concurrently.
3. Engine restart during a refresh race — pre-SEV-507, the race window
   was wider.

**Diagnose.**

```bash
# Count replay events by user in the last hour
docker compose exec app \
  grep "token_replay_detected" logs/engine.log | \
  jq -r '.user_id' | sort | uniq -c | sort -rn | head

# Check user agents for the offending user
docker compose exec db psql -U nexus -d nexus -c \
  "SELECT user_agent, ip_address, COUNT(*) FROM refresh_tokens
   WHERE user_id = '<uuid>' GROUP BY 1, 2;"
```

**Remediate.**

- Single user, single IP/UA: notify the user; their client has a bug.
  The engine's response (revoke every session) is correct.
- Single user, multiple IPs/UAs: treat as a token leak. Force password
  reset (`UPDATE users SET is_active=false WHERE id='<uuid>'`), reset
  MFA (`mfa_secret_encrypted=NULL, mfa_enabled=false`), and revoke
  every API key.
- Multiple users simultaneously: suspect a deployment regression.
  Check `git log engine/api/routes/auth.py` and the most recent
  release notes.

**Follow-up.** Open an issue tracking the root cause. If the cause is
a buggy SDK, document the workaround in
[`../api/reference.md#auth`](../api/reference.md).

---

## 3. Webhook deliveries failing

**Symptom.**
`nexus.webhook.deliveries_terminal_total{outcome="failed"}` is
rising; `webhook_deliveries` table has many rows with
`status='failed'` and `response_status` in the 400–500 range.

**Likely cause.**

1. Recipient endpoint is down or returning 4xx.
2. Recipient endpoint changed URL and the operator didn't update.
3. Template payload schema changed in a recent engine release.
4. Network egress from the engine host is broken.

**Diagnose.**

```bash
# Recent failures by webhook
docker compose exec db psql -U nexus -d nexus -c \
  "SELECT webhook_id, COUNT(*), MAX(error)
   FROM webhook_deliveries
   WHERE status = 'failed' AND created_at > NOW() - INTERVAL '1 hour'
   GROUP BY webhook_id ORDER BY 2 DESC;"

# Test a specific webhook from the engine host
curl -X POST https://<recipient> -H "X-Nexus-Signature: ..." -d '{}'

# Send a synthetic test event through the engine
curl -X POST https://<host>/api/v1/webhooks/<id>/test \
  -H "Authorization: Bearer <admin token>"
```

**Remediate.**

- Recipient side fix (most common). Ask the operator to update the
  URL or fix their endpoint.
- 4xx is treated as terminal by the dispatcher (no retry). If the
  recipient wants retries on 4xx (rare), they need to return 5xx
  instead.
- Disable a misconfigured webhook:
  `UPDATE webhook_configs SET is_active=false WHERE id='<uuid>';`
- Template mismatch: check the changelog for `engine/events/webhook_dispatcher.py`
  and update the recipient.

**Follow-up.** Add the recipient to the runbook if they fail
repeatedly. Consider a per-webhook "disable after N consecutive
failures" circuit breaker.

---

## 4. Data provider 503s

**Symptom.** `GET /api/v1/market-data/{symbol}/bars` returns 503;
`GET /health/providers` shows `down` for one or more providers.

**Likely cause.**

1. Provider's API key expired or rate-limited.
2. Provider is genuinely down.
3. The registry has every candidate adapter failing for the asset
   class — `NoProviderAvailableError`.

**Diagnose.**

```bash
# Provider health (returns per-provider status + latency)
curl https://<host>/health/providers | jq .

# Logs
docker compose exec app grep "market_data.provider_transient" logs/engine.log | tail -50

# Check the YAML config for expired keys
docker compose exec app cat /path/to/data-providers.yaml
```

**Remediate.**

- Provider down: nothing to do except wait. The engine continues
  serving routes that don't need market data.
- Expired key: update the YAML, restart the engine. The registry
  reloads on startup.
- All providers down: temporarily register a fallback (e.g. Yahoo)
  via `engine.data.providers.configure_registry`.
- Permanently dead provider: deregister it from the YAML and remove
  the entry.

**Follow-up.** Multi-provider redundancy is the right answer. Make
sure each asset class has at least two adapters configured.

---

## 5. DB connection exhaustion

**Symptom.** New requests fail with
`OperationalError: QueuePool limit ... overflow ... reached`; CPU on
the DB host is normal.

**Likely cause.**

1. Long-running queries holding connections.
2. Connection leak in a route (missing `await session.close()` or a
   `try/finally`).
3. Pool size too small for actual concurrency.

**Diagnose.**

```bash
# Engine: how many connections is SQLAlchemy holding?
docker compose exec app python -c "
from engine.db.session import sync_engine
print(sync_engine.pool.status())
"

# Postgres: active connections by application
docker compose exec db psql -U nexus -d nexus -c \
  "SELECT application_name, state, COUNT(*)
   FROM pg_stat_activity GROUP BY 1, 2 ORDER BY 3 DESC;"

# Long-running queries
docker compose exec db psql -U nexus -d nexus -c \
  "SELECT pid, now() - xact_start AS dur, query
   FROM pg_stat_activity WHERE state != 'idle'
   ORDER BY dur DESC LIMIT 10;"
```

**Remediate.**

- Restart the engine to clear the pool: `docker compose restart app`.
- Identify and kill the long-running queries:
  `SELECT pg_terminate_backend(<pid>);`
- If the workload genuinely needs more connections, raise
  `NEXUS_DATABASE_POOL_SIZE` and `NEXUS_DATABASE_MAX_OVERFLOW`.
- If a route is leaking, look for `await db.execute(...)` without a
  surrounding `async with session_factory()` — the request-scoped
  session in `engine/deps.py:get_db` handles this for you; do not
  open new sessions inline.

**Follow-up.** Open a ticket with the offending query / route. Add a
sentinel metric for `sqlalchemy.pool.checkedout` if it isn't already
exposed.

---

## 6. Valkey OOM

**Symptom.** Every request returns 429; rate-limit counters are
absent; TaskIQ broker is silent.

**Likely cause.**

1. Valkey's `maxmemory` is hit.
2. A misbehaving client is hammering a route and the rate-limit keys
   are accumulating.
3. TaskIQ result backend has accumulated un-reaped results.

**Diagnose.**

```bash
docker compose exec valkey valkey-cli INFO memory | grep used_memory_human
docker compose exec valkey valkey-cli INFO keyspace
docker compose exec valkey valkey-cli --bigkeys
```

**Remediate.**

- Free memory: `docker compose exec valkey valkey-cli FLUSHDB` (rate
  limit resets for everyone — use only as a last resort).
- Raise `maxmemory` in `valkey.conf` and restart.
- Switch policy to `volatile-lru` if it isn't already (LRU on TTL'd
  keys preserves the broker queue).
- If TaskIQ results are the culprit, ensure the worker is consuming
  them; `taskiq:broker:*` keys should be small.

**Follow-up.** Add `evicted_keys` and `used_memory` to the operator's
Valkey dashboard.

---

## 7. MFA verify failures

**Symptom.** `auth.mfa_verify_failed` log count rising; users
complain they cannot log in even with the right code.

**Likely cause.**

1. Server clock skew (TOTP is ±30 s by default).
2. Fernet key rotated without re-encrypting secrets.
3. Backup code exhaustion — user used all 10 and is now locked out.
4. User's authenticator app lost the secret (phone reset).

**Diagnose.**

```bash
# Clock skew
docker compose exec app date -u
docker compose exec db date -u

# Per-user failure rate
docker compose exec app grep "mfa_verify_failed" logs/engine.log | \
  jq -r '.user_id' | sort | uniq -c | sort -rn | head
```

**Remediate.**

- **Clock skew > 1 s.** Install NTP / chrony on the engine host.
- **Fernet key mismatch.** Roll back to the previous key. Recovery
  requires admin intervention — there is no automated path.
- **Backup code exhaustion.** Admin-only: clear `mfa_enabled`,
  `mfa_secret_encrypted`, `mfa_backup_codes` for the user, and ask
  them to re-enroll.
- **Lost secret.** Same path as backup code exhaustion.

**Follow-up.** Document the Fernet key rotation procedure in
`SECURITY.md` if it isn't already there.

---

## 8. Backtest runner OOM

**Symptom.** Backtest process killed mid-run; `backtest_results` row
shows `status=failed` with `error_type=MemoryError` (or no error —
process killed before it could log).

**Likely cause.**

1. Strategy loads full OHLCV history into memory.
2. Strategy maintains unbounded rolling state.
3. Sandbox memory cap is reasonable but the strategy's per-eval
   working set grew.

**Diagnose.**

```bash
# Host memory during a backtest
docker stats <engine container>

# Process-level inside the container (distroless lacks ps; use /proc)
docker compose exec app cat /proc/1/status | grep -i vm

# Backtest result with error
docker compose exec db psql -U nexus -d nexus -c \
  "SELECT id, error, error_type FROM backtest_results
   WHERE error_type IS NOT NULL
   ORDER BY created_at DESC LIMIT 20;"
```

**Remediate.**

- Raise `manifest.resources.max_memory` for the offending strategy.
- Increase host memory.
- Stream the OHLCV via Polars lazy frames instead of materialising.
- Reduce the strategy's universe.

**Follow-up.** Open a ticket with the strategy author to fix the
memory growth. A strategy that needs unbounded memory is a strategy
that will eventually OOM in production.

---

## 9. Legal acceptance gate misfire

**Symptom.** Every authenticated request to `/api/v1/backtest`,
`/api/v1/scoring`, `/api/v1/portfolio`, `/api/v1/market-data`, or
`/api/v1/strategies` returns 403 with `"Legal acceptance required"`.

**Likely cause.**

1. A new legal document was added with `requires_acceptance=true` and
   no users have accepted it yet.
2. An existing document's `version` was bumped; previous acceptances
   no longer satisfy the current version check.
3. The legal sync script overwrote the document and dropped the
   `current_version`.

**Diagnose.**

```bash
# Current document versions in the DB
docker compose exec db psql -U nexus -d nexus -c \
  "SELECT slug, current_version, requires_acceptance, effective_date
   FROM legal_documents ORDER BY slug;"

# Who has accepted the new version?
docker compose exec db psql -U nexus -d nexus -c \
  "SELECT user_id, accepted_at FROM legal_acceptances
   WHERE document_slug = '<slug>' AND document_version = '<ver>';"

# Source markdown (compare to DB)
ls -la legal/
```

**Remediate.**

- If a version bump was unintentional, revert the front matter in
  `legal/*.md`, restart the engine (sync runs on startup), and
  acceptances will revalidate.
- If the bump was intentional but no one has accepted, push the UI to
  prompt users on next login (the legal routes return enough info for
  the UI to do this).
- Emergency unblock: temporarily set `requires_acceptance=false` for
  the offending row. **Document this in the on-call log** — it
  bypasses a compliance gate.

**Follow-up.** Add an integration test that bumps a document version
and asserts at least one user has accepted within a reasonable
window.

---

## Other useful operational snippets

### Count active sessions per user

```sql
SELECT user_id, COUNT(*) AS active_sessions
FROM refresh_tokens
WHERE revoked_at IS NULL AND expires_at > NOW()
GROUP BY user_id ORDER BY 2 DESC;
```

### Top webhook consumers (last 24 h)

```sql
SELECT w.url, COUNT(d.id) AS deliveries,
       SUM(CASE WHEN d.status='delivered' THEN 1 ELSE 0 END) AS ok
FROM webhook_deliveries d
JOIN webhook_configs w ON w.id = d.webhook_id
WHERE d.created_at > NOW() - INTERVAL '24 hours'
GROUP BY w.url ORDER BY 2 DESC LIMIT 20;
```

### Recent failed logins

```bash
docker compose exec app grep "auth.login_failed" logs/engine.log | \
  jq -r '.user_id // .email' | sort | uniq -c | sort -rn | head -20
```

### Estimate OHLCV hypertable compression savings

```sql
SELECT chunk_name, pg_size_pretty(before_compression_bytes) AS before,
       pg_size_pretty(after_compression_bytes)  AS after
FROM chunk_compression_stats('ohlcv_bars');
```

## Related runbooks

Per-issue runbooks in [`runbooks/`](runbooks/):

- [`api-availability.md`](runbooks/api-availability.md) — page-grade
  API availability burn-rate.
- [`api-latency.md`](runbooks/api-latency.md) — page-grade latency
  burn-rate.
- [`auth-mfa.md`](runbooks/auth-mfa.md) — auth + MFA verify failures.
- [`backtest-submit.md`](runbooks/backtest-submit.md) — backtest
  submit failures.
- [`task-pipeline.md`](runbooks/task-pipeline.md) — TaskIQ queue
  health.
- [`webhook-delivery.md`](runbooks/webhook-delivery.md) — webhook
  delivery failures.

Service-level objectives and burn-rate thresholds live in
[`slos.md`](slos.md).
