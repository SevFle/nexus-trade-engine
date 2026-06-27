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
| 011   | `api_keys` — long-lived scoped credentials for SDK / headless access (gh#94). |
| 012   | `dsr_requests` — GDPR / CCPA data-subject-request audit log (gh#157). |
| 013   | `users.processing_restricted` — GDPR Art. 18 right-to-restriction flag (gh#157). Backfills the column the model + `engine/privacy/deletion.py` already referenced. |

Run `alembic history` for the source of truth. The next free revision
number is `014`.

## Critical tables

These are the rows you must protect during a restore. See the backup
runbook at [`docs/operations/backup-and-recovery.md`](../operations/backup-and-recovery.md).

- **`users`** — primary identity. Password hashes are bcrypt; MFA
  TOTP secrets are Fernet-encrypted with the engine's
  `MFA_ENCRYPTION_KEY` (see [auth-mfa runbook](../operations/runbooks/auth-mfa.md)).
  Carries the `processing_restricted` flag (rev 013, GDPR Art. 18) —
  see `data-model.md` for the semantics.
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
- **`api_keys`** — long-lived bearer credentials for headless clients.
  `key_hash` is bcrypt; the plaintext secret is shown once at create
  time. Revocation is soft (`revoked_at`).
- **`dsr_requests`** — append-only GDPR / CCPA audit row. The
  `sla_due_at` column is the statutory clock. The application default
  is **30 days** (see `SLA_DEFAULT_DAYS` in
  [`engine/privacy/dsr.py`](../../engine/privacy/dsr.py)), chosen as
  the strictest widely-applicable deadline we ship out of the box:

  - **GDPR** (EU/UK) requires a response within **one calendar month**
    of receipt (Art. 12(3)). "One month" is interpreted literally —
    a request received on 15 January is due on 15 February — so a
    flat 30-day timer is the safer side of the line and must be
    revisited on a per-request basis when a 31-day month is in play.
  - **CCPA / CPRA** (California) allows up to **45 days**, with a
    single 45-day extension permitted on written notice. Our 30-day
    default is well inside the CCPA ceiling; operators who need the
    full 45 days (or the 45-day extension) should pass `sla_days=45`
    to [`record_request`](../../engine/privacy/dsr.py).

  Operators are expected to keep this table intact for regulators.

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
  [`engine/deps.py`](../../engine/deps.py).
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
