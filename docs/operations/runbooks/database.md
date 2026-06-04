# Runbook: Database issues

**Linked alerts**: `DatabaseUnavailable`, `MigrationFailed`,
`SlowQueryP95`, `ConnectionPoolExhausted`.

## What this means

The engine depends on PostgreSQL for nearly every operation.
When the database is unreachable, slow, or hitting pool limits,
every API route that touches durable state will degrade.

## First 60 seconds

1. Hit the engine's readiness probe:
   ```bash
   curl -sS https://<host>/ready
   ```
   If `db: error`, the engine has confirmed the DB is down from
   its own perspective.
2. Check the database container / VM:
   ```bash
   docker compose ps db
   docker compose logs db --tail=200
   ```
3. Confirm the connection string the engine is using matches
   what the DB expects:
   ```bash
   docker compose exec app env | grep NEXUS_DATABASE_URL
   ```

If the readiness probe shows `db: ok` but you are still seeing
5xx responses, jump to **Slow queries** below.

## Triage

### DB unreachable

Most common cause: the DB container restarted and the engine's
connection pool is holding dead sockets. `asyncpg` will recover
on its own within seconds; if it doesn't:

```bash
docker compose restart app worker
```

The engine re-establishes its pool on startup.

### Slow queries

```sql
-- Active queries, longest first
SELECT pid, now() - query_start AS duration, state, query
FROM pg_stat_activity
WHERE state != 'idle'
ORDER BY duration DESC NULLS LAST
LIMIT 20;
```

The usual culprits:

- `backtest_results` reads without an index — see if a new
  query pattern is missing a `(portfolio_id, created_at)` index.
- `webhook_deliveries` retention sweep running during peak
  traffic — reschedule the sweep off-hours.
- A JSONB query on `metrics` — GIN index may be missing. See
  [data-model.md](../architecture/data-model.md) for guidance.

### Connection pool exhaustion

`asyncpg` connections are bounded by `NEXUS_DATABASE_POOL_SIZE`
(default 5) + `NEXUS_DATABASE_MAX_OVERFLOW` (default 10). If
you're consistently hitting 15 connections:

1. Check for slow queries holding connections open — see above.
2. Check for runaway background tasks that each hold a session:
   ```bash
   docker compose exec app python -c "
   from engine.db.session import get_engine
   import asyncio
   print(asyncio.run(get_engine().pool.status()))
   "
   ```
3. Raise the pool size only after ruling out a leak. A leak will
   just exhaust the larger pool too.

### Migration failures

If `alembic upgrade head` exits non-zero:

1. Look at the migration that failed — the chain is sequential,
   so the failing migration is the one named in the traceback.
2. Restore from backup if data was partially written.
3. Either fix the migration (and re-run) or revert the deploy.

Migrations are forward-only in production. If you must revert,
write a fix-forward migration that restores compatibility with
the previous code.

## Common causes

| Symptom | Likely cause | Fix |
|---|---|---|
| `db: error` in /ready | DB container down or network partition | `docker compose restart db`, then `app` and `worker`. |
| 500s on every write | DB up but disk full | `df -h` on the DB host; expand volume or run retention cleanup. |
| 500s on /api/v1/auth/login | `users` table query timing out | Check for long-running transaction blocking the row. |
| Migration `ALTER TABLE` hangs | Lock held by long-running query | `pg_terminate_backend(<pid>)` the offender; retry. |
| Disk usage climbing fast | `ohlcv_bars` growth | Convert to hypertable (see known-limitations.md) or run retention sweep. |
| Worker logs "too many connections" | Pool size too small for traffic | Raise `NEXUS_DATABASE_POOL_SIZE` gradually; verify the leak test first. |

## Escalation

- **Database** — on-call DBA / platform engineer.
- **Migration** — engineer who authored the failing migration
  (git blame the file in `engine/db/migrations/versions/`).
- **Slow query in domain code** — engineer who owns the route
  (check `engine/api/routes/<area>.py`).

## Post-incident

Capture in the incident doc:

- The exact query or migration that triggered the issue.
- The `pg_stat_activity` snapshot at the peak.
- The recovery action and its wall-clock duration.
- Whether the runbook needs an update. If yes, file the PR in
  the same incident channel before closing the channel.
