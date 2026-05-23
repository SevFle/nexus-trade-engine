# Runbooks

This document covers the most likely production issues and how to diagnose
them. For granular per-subsystem runbooks, see [operations/runbooks/](operations/runbooks/).

---

## RB-01: API Returning 503 / "Upstream Provider Unavailable"

**Symptoms:** Market data endpoints return 503. Health endpoint shows
`providers` status as `degraded` or `down`.

**Diagnosis:**

```bash
# Check provider health
curl http://localhost:8000/health/providers

# Check engine logs for provider errors
docker compose logs app | grep "provider_transient\|provider_error"
```

**Resolution:**

1. If Yahoo Finance is the default provider, it may be rate-limited.
   Yahoo enforces ~2,000 requests/hour per IP. Check logs for HTTP 429.
2. If using a paid provider (Polygon, Alpaca), verify API key is valid:
   ```bash
   curl -H "Authorization: Bearer $POLYGON_API_KEY" \
     "https://api.polygon.io/v2/aggs/ticker/AAPL/prev"
   ```
3. If all providers are down, the engine still serves cached results
   (if available) and all other endpoints work normally.
4. To add a fallback provider, update `config/data_providers.yaml` and
   restart the app container.

**Prevention:** Configure at least two providers per asset class with
different priorities. The registry will failover automatically.

---

## RB-02: Backtest Stuck in "running" State

**Symptoms:** `GET /api/v1/backtest/results/{id}` returns `status: "running"`
for more than a few minutes.

**Diagnosis:**

```bash
# Check worker logs
docker compose logs worker | grep "backtest\|taskiq"

# Check if worker is processing tasks
docker compose logs worker | grep "received_task\|task_finished"
```

**Resolution:**

1. **Worker is not running** — restart it:
   ```bash
   docker compose restart worker
   ```
2. **Worker is processing but slow** — the strategy may have a computationally
   expensive `evaluate()` loop. Check the strategy's `max_cpu_seconds` in
   its manifest. The sandbox will time out and return empty signals.
3. **Valkey connection lost** — worker can't receive tasks:
   ```bash
   docker compose logs valkey
   docker compose restart worker
   ```
4. **Stale result** — if the worker crashed mid-task, the result stays in
   `running` forever. The result store has an automatic expiry (TTL in
   Valkey). If needed, manually clear:
   ```bash
   docker compose exec app python -c "
   import asyncio
   from engine.tasks.result_store import get_result_store
   async def clear():
       store = await get_result_store()
       await store.evict_expired()
   asyncio.run(clear())
   "
   ```

---

## RB-03: Circuit Breaker Triggered

**Symptoms:** All order submissions return `RISK_REJECTED` with reason
"Circuit breaker active". Log entries show `risk.circuit_breaker_triggered`.

**Diagnosis:**

```bash
# Check logs for trigger event
docker compose logs app | grep "circuit_breaker"
```

**Resolution:**

1. Verify the drawdown is real — check the portfolio value:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/api/v1/portfolio/{portfolio_id}
   ```
2. If the drawdown is legitimate, the circuit breaker is doing its job.
   Review the strategy's recent trades and adjust position sizing before
   resetting.
3. If the trigger was false (e.g., stale price data caused incorrect NAV),
   reset the circuit breaker:
   ```python
   # Inside a running app context
   from engine.core.risk_engine import risk_engine
   risk_engine.reset_circuit_breaker()
   ```
4. Adjust the threshold if it's too tight:
   ```
   NEXUS_CIRCUIT_BREAKER_DRAWDOWN_PCT=0.15  # Increase from 10% to 15%
   ```

**Prevention:** Monitor the `risk.circuit_breaker` metric in Grafana. Set
an alert for any state change.

---

## RB-04: Kill Switch Engaged

**Symptoms:** Live trading orders are blocked. Logs show `kill_switch.engaged`.

**Diagnosis:**

```bash
docker compose logs app | grep "kill_switch"
```

**Resolution:**

1. Identify why the switch engaged — check the `reason` and `actor` fields
   in the log entry.
2. Verify the issue that caused the engagement is resolved.
3. Disengage with the confirmation token (documented in this runbook):
   ```python
   # The confirmation token prevents accidental disengagement
   from engine.core.live.kill_switch import get_kill_switch
   ks = get_kill_switch()
   ks.disengage(confirmation="I_UNDERSTAND_THE_RISK", actor="operator-name")
   ```
4. Verify state:
   ```python
   ks.snapshot()  # Should show state=disengaged
   ```

**IMPORTANT:** The kill switch does NOT survive process restarts. After a
crash, verify state manually before resuming trading.

---

## RB-05: Database Connection Pool Exhaustion

**Symptoms:** API requests hang and eventually return 500. Logs show
`TimeoutError` from asyncpg or "queue pool limit reached".

**Diagnosis:**

```bash
# Check active connections
docker compose exec db psql -U nexus -c "SELECT count(*) FROM pg_stat_activity;"

# Check pool configuration
grep "pool" .env
```

**Resolution:**

1. Increase pool size if load is genuinely higher than expected:
   ```
   NEXUS_DATABASE_POOL_SIZE=10
   NEXUS_DATABASE_MAX_OVERFLOW=20
   ```
2. Check for long-running queries:
   ```sql
   SELECT query, state, duration
   FROM pg_stat_activity
   WHERE state = 'active' AND now() - query_start > interval '5 seconds';
   ```
3. Check for leaked transactions — routes that begin a transaction but
   never commit/rollback. The async session factory uses `expire_on_commit=False`,
   but a missing `await db.commit()` will hold a connection open.
4. Restart the app container to reset the pool:
   ```bash
   docker compose restart app
   ```

---

## RB-06: Webhook Deliveries Failing

**Symptoms:** Webhook delivery history shows `status: "failed"` repeatedly.

**Diagnosis:**

```bash
# Check recent deliveries
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/webhooks/{webhook_id}/deliveries?limit=10"
```

**Resolution:**

1. Check the `response_status` and `error` fields in the delivery records.
2. **4xx errors** — the webhook URL is returning client errors. The endpoint
   may be rejecting the payload format. Use the test endpoint to debug:
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/api/v1/webhooks/{webhook_id}/test
   ```
3. **Timeout** — the remote endpoint is slow. Increase `max_retries` or
   ask the endpoint operator to increase their timeout.
4. **DNS / network** — the engine cannot reach the webhook URL:
   ```bash
   docker compose exec app python -c "
   import httpx
   r = httpx.get('https://example.com/health')
   print(r.status_code)
   "
   ```
5. **Signing mismatch** — verify the endpoint is validating the HMAC
   signature using the `signing_secret` from the webhook creation response.

---

## RB-07: High Memory Usage in Worker

**Symptoms:** Worker process is consuming excessive memory, possibly OOM-killed.

**Diagnosis:**

```bash
# Check worker memory
docker stats nexus-trade-engine-worker-1

# Check for large result objects in logs
docker compose logs worker | grep "MemoryError\|large"
```

**Resolution:**

1. Backtests with very long date ranges can produce large equity curves.
   The `equity_curve` list is stored in memory before being written to
   Valkey/D B.
2. Reduce the backtest date range or increase the worker's memory limit:
   ```yaml
   worker:
     deploy:
       resources:
         limits:
           memory: 4G
   ```
3. If the strategy is leaking memory ( accumulating state across bars),
   the sandbox's CPU timeout will eventually kill it, but memory is not
   reclaimed until the process restarts.

---

## RB-08: JWT Token Issues

**Symptoms:** All authenticated requests return 401.

**Diagnosis:**

```bash
# Try to get a fresh token
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"secret"}'
```

**Resolution:**

1. **Token expired** — access tokens expire after 60 minutes (configurable).
   Use the refresh token to get a new pair:
   ```bash
   curl -X POST http://localhost:8000/api/v1/auth/refresh \
     -H "Content-Type: application/json" \
     -d '{"refresh_token":"..."}'
   ```
2. **Secret key rotated** — if `NEXUS_SECRET_KEY` was changed, all existing
   tokens are invalid. Set `NEXUS_SECRET_KEY_PREVIOUS` to the old key during
   the transition window.
3. **Token replay detected** — if a refresh token is used twice, the system
   revokes ALL refresh tokens for that user as a security measure. The user
   must log in again.
4. **MFA required** — if the user has MFA enabled, login returns
   `mfa_required: true` instead of tokens. Complete the MFA flow via
   `/api/v1/auth/mfa/verify`.

---

## See Also

- [operations/runbooks/](operations/runbooks/) — per-subsystem runbooks
- [operations/slos.md](operations/slos.md) — SLO targets and alerting thresholds
- [operations/backup-and-recovery.md](operations/backup-and-recovery.md) — backup procedures
- [operations/dr-drill-checklist.md](operations/dr-drill-checklist.md) — disaster recovery
