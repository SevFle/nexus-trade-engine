# Database

Nexus Trade Engine stores all durable state in a single Postgres
database (TimescaleDB extension enabled for time-series tables).
The schema is owned by the Alembic migration chain in
[`engine/db/migrations/versions/`](../../engine/db/migrations/versions/).

## Migration policy

- **One revision per logical change.** The chain is numbered
  sequentially: `001_initial_schema.py`, `002_additional_tables.py`,
  `003_bt_result_nullable_pid.py`, …, `010_webhooks.py`. Pick the next
  number when adding a migration.
- Every migration must define both `upgrade()` and `downgrade()`. If a
  step is genuinely irreversible (data loss), make `downgrade()` an
  explicit `op.execute("...")` that destroys what was created — but
  *do* write it down.
- Migrations run via `make migrate` locally and on the operator's
  schedule in production. Long-running migrations should be split into
  reversible steps so they can be rolled out without locking writes.
- Models live in [`engine/db/models.py`](../../engine/db/models.py).
  Keep them and the migration that creates them in the same PR.

## Current chain

| Rev   | Adds / changes                                                 |
|-------|----------------------------------------------------------------|
| 001   | Initial schema: users, strategies, backtest_results, accounts. |
| 002   | Auxiliary tables (portfolios, journals, positions, fills).     |
| 003   | Make `backtest_results.portfolio_id` nullable.                 |
| 004   | Legal documents (Terms, Privacy, Disclaimer, …).               |
| 005   | Auth/RBAC tables (roles, role_assignments).                    |
| 006   | Make `legal_acceptance` rows immutable (no update/delete).     |
| 007   | `scoring_snapshots` for cross-strategy composite scoring.      |
| 008   | `backtest_results.composite_score` + `score_breakdown` (gh#8). |
| 009   | `users.{mfa_enabled, mfa_secret_encrypted, mfa_backup_codes}`. |
| 010   | `webhook_configs` + `webhook_deliveries` (gh#80).              |

Run `alembic history` for the source of truth.

## Critical tables

These are the rows you must protect during a restore. See the backup
runbook at [`docs/operations/backup-and-recovery.md`](../operations/backup-and-recovery.md).

- **`users`** — primary identity. Password hashes are bcrypt; MFA
  TOTP secrets are Fernet-encrypted with the engine's
  `MFA_ENCRYPTION_KEY` (see [auth-mfa runbook](../operations/runbooks/auth-mfa.md)).
- **`backtest_results`** — every run a user has ever submitted. The
  `score_breakdown` JSONB column is the per-dimension score map from
  the strategy evaluator.
- **`portfolios`, `accounts`, `positions`, `fills`** — operational
  trading state. When live trading lands (#109 / #111) these tables
  will see write traffic on every fill.
- **`webhook_configs`, `webhook_deliveries`** — the outbound webhook
  registry and a delivery audit trail. The `signing_secret` column is
  returned to the operator only on create; reads return null. **Do not
  log delivery payloads** — they may contain user data.

## TimescaleDB usage

We use the TimescaleDB extension for time-series tables that grow
unboundedly (market data, OHLCV bars, account equity history). When
adding such a table:

1. Define it as a regular Postgres table in the migration.
2. Convert it to a hypertable in the same migration:
   ```python
   op.execute(
       "SELECT create_hypertable('ohlcv', 'ts', "
       "if_not_exists => TRUE, chunk_time_interval => INTERVAL '1 day');"
   )
   ```
3. Add a retention policy if the data has a sane retention window.
4. Note the dependency in this doc.

Operators can run on vanilla Postgres if they accept the storage cost.

## Async access pattern

All DB access goes through SQLAlchemy 2's async API:

```python
from engine.db.session import session_factory

async with session_factory() as session:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
```

- **No sync sessions in route handlers.** They block the event loop.
- **One session per request.** Don't pass a session across async
  boundaries; use the dependency in
  [`engine/api/deps.py`](../../engine/api/deps.py).
- **Don't `commit` inside utility functions.** Commit at the route
  handler boundary so the request's atomicity is obvious.
- **Use `select` + `where`, not `query`.** SQLAlchemy 2's legacy API
  is still importable but we don't use it.

## Conventions

- Primary keys are UUIDs except for legacy bigserial tables. New
  tables should use UUIDs.
- All tables have `created_at` and `updated_at` (default `now()`,
  `updated_at` set by SQLAlchemy event listener).
- JSON-shaped columns use `JSONB`, never `JSON`. Index with `GIN` if
  you query by key.
- Foreign keys default to `ON DELETE CASCADE` for owned data and
  `ON DELETE RESTRICT` for shared / audit rows.

## Testing

Unit tests run against SQLite when possible to keep CI fast. Tests
that exercise Postgres-specific features (JSONB queries, TimescaleDB
hypertables, immutable triggers) should be marked
`@pytest.mark.integration` and run in a Postgres-backed CI job.

When adding a migration, also add a test that:
1. Asserts the table / column exists at the new head.
2. Round-trips a representative row.
3. Exercises whatever invariant the migration is enforcing
   (e.g. uniqueness, immutability).
