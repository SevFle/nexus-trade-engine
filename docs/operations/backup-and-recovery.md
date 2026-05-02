# Backup, Restore, and Disaster Recovery

This runbook describes how to back up the Postgres instance that backs Nexus
Trade Engine and how to restore it — either from a specific snapshot or to
an arbitrary point in time (PITR). It is written for operators running their
own deployment; managed-Postgres users (RDS, Cloud SQL, Crunchy, Supabase,
Neon, etc.) should use the equivalent vendor-native primitives where they
exist.

## Recovery Objectives

These are the targets we recommend for a production-class deployment:

| Metric | Target | Notes |
|--------|--------|-------|
| RPO (recovery point objective) | ≤ 5 minutes | WAL ship interval. |
| RTO (recovery time objective)  | ≤ 1 hour    | Median full restore. |
| Backup retention               | 30 days     | Plus weekly archives ≥ 1 year. |
| Restore drill cadence          | Quarterly   | See [DR drill checklist](dr-drill-checklist.md). |

If your deployment cannot meet these, treat the gap as a known incident and
document the deviation in your operations log.

## Data Surface to Protect

Nexus Trade Engine stores all durable state in a single Postgres database.
The schema is owned by the Alembic migration chain in
`engine/db/migrations/versions/`. The most critical tables for recovery are:

- `users`, including MFA secrets (encrypted at rest with the engine's
  Fernet key — back up that key separately, see [Secrets](#secrets-and-keys)).
- `backtest_results` (composite scores, breakdowns).
- `webhook_configs`, `webhook_deliveries` — operational audit trail.
- Any `strategies_*`, `orders_*`, and `portfolios_*` tables introduced by
  later issues (#109 / #111).

Object storage (logs, large artifacts) is **out of scope** — back it up via
the bucket's native versioning + replication features.

## Backup Strategy

We rely on three layers, each compensating for the failure modes of the
others:

### 1. Continuous WAL archiving (PITR foundation)

Configure Postgres to ship WAL segments to durable, off-host object storage:

```ini
# postgresql.conf
wal_level = replica
archive_mode = on
archive_command = 'aws s3 cp %p s3://your-bucket/wal/%f --no-progress'
archive_timeout = '5min'      # bounds RPO at five minutes
max_wal_senders = 3
wal_keep_size = 1GB
```

Use the bucket's lifecycle policy to expire WAL older than the retention
window (e.g. 30 days). For non-AWS environments, swap `aws s3 cp` for the
equivalent `gcloud storage cp`, `az storage blob upload`, `mc cp` (MinIO),
or `wal-g`.

### 2. Periodic base backups

Take a full physical base backup on a schedule (default: daily at 02:00
UTC). PITR works by replaying WAL forward from the most recent base backup,
so without a recent base backup the WAL chain is useless.

The shipped helper at `scripts/ops/pg_basebackup.sh` is a thin wrapper
around `pg_basebackup`:

```bash
PGHOST=primary.internal \
PGUSER=replicator \
PGPASSWORD=... \
BACKUP_DEST=s3://your-bucket/base/$(date -u +%F) \
  scripts/ops/pg_basebackup.sh
```

Consider `pgbackrest` or `wal-g` for production: both consolidate base
backups + WAL archiving, support incremental backups, and ship encryption
out of the box.

### 3. Daily logical dumps (low-cost portability)

A nightly `pg_dump` is cheap insurance against logical corruption that PITR
will replay forward (e.g. an operator running `DELETE` without a `WHERE`).
The shipped helper at `scripts/ops/pg_logical_backup.sh` performs a
custom-format dump and uploads it:

```bash
PGHOST=primary.internal \
PGUSER=ntx_backup \
PGDATABASE=nexus \
BACKUP_DEST=s3://your-bucket/logical/ \
RETENTION_DAYS=30 \
  scripts/ops/pg_logical_backup.sh
```

Do **not** rely on logical dumps as your only backup — they are a snapshot,
not a continuous record, and large databases make them painful.

## Encryption and Retention

- Enable server-side encryption on the backup bucket (SSE-S3 / SSE-KMS or
  vendor equivalent).
- Object lock / versioning protects against accidental or malicious
  deletion of past backups. Enable it.
- Default retention: 30 days for daily; 1 year for weekly archives. Tune
  based on your compliance regime.

## Secrets and Keys

The engine encrypts MFA secrets and (in future) broker credentials with a
Fernet key supplied via environment configuration (`MFA_ENCRYPTION_KEY`).
**Back up that key out-of-band** — losing it makes the encrypted columns
unrecoverable even with a valid database backup. A typical pattern:

- Store the key in a managed secrets vault (AWS Secrets Manager, Hashicorp
  Vault, GCP Secret Manager) with versioning enabled.
- Cross-region replicate the secret if your DB backup is cross-region.
- Test rotation quarterly using the [DR drill](dr-drill-checklist.md).

Never commit the key to git, and never include it in logical dumps. Rotate
the key if it has ever been on a developer laptop.

## Restore Procedures

### Full restore from a logical dump

For straightforward "recover yesterday's data" cases:

```bash
# Provision a clean Postgres at the same major version.
createdb -h $TARGET_HOST -U $TARGET_USER nexus

# Stream the dump (custom-format) directly from object storage.
aws s3 cp s3://your-bucket/logical/2026-05-01.dump - | \
  pg_restore -h $TARGET_HOST -U $TARGET_USER -d nexus \
             --no-owner --no-privileges --jobs 4
```

After the restore, run `alembic upgrade head` to confirm the schema is at
the expected revision.

### Point-in-time recovery (PITR)

Use this when you need to roll back to a specific timestamp — e.g. just
before a destructive query, or just before a buggy migration ran.

1. Identify the recovery target. Examples:
   - `2026-05-01 14:33:00 UTC` (timestamp).
   - The transaction id immediately before the offending one (read it
     from logs and use `recovery_target_xid`).
   - A named restore point, if one was created via
     `SELECT pg_create_restore_point('pre-migration');`.
2. Stop writes to the failing primary and put it in maintenance mode. Do
   **not** delete it — you may need it for forensics.
3. Provision a fresh Postgres at the same major version.
4. Run the helper:

   ```bash
   BACKUP_DEST=s3://your-bucket \
   RECOVERY_TARGET_TIME='2026-05-01 14:33:00 UTC' \
   PGDATA=/var/lib/postgresql/data \
     scripts/ops/pg_pitr_restore.sh
   ```

   The script will:
   - Download the most recent base backup that predates the recovery
     target.
   - Restore it into `$PGDATA`.
   - Write `recovery.signal` and the appropriate
     `restore_command` / `recovery_target_*` settings.
   - Start Postgres in recovery mode and watch until promotion.
5. Run a smoke test query against the recovered DB before pointing the
   application at it.
6. Once verified, repoint the application's `DATABASE_URL`, then take a
   fresh base backup so the new lineage is protected.

### Recovering MFA-encrypted columns

After a restore, log in as a user with MFA enabled and run through the
full `/api/v1/auth/login` → `/verify` flow. If the MFA code fails, the
Fernet key from the running engine does not match the key the backup was
encrypted with — restore the key from the secrets vault before continuing.

## Monitoring

At minimum, alert on:

- `archive_failures_total` (Postgres `pg_stat_archiver`) > 0 in the last
  10 minutes.
- No WAL segment archived in the last `archive_timeout * 3` window.
- Last successful base backup older than 36 hours.
- Last successful logical dump older than 36 hours.
- Backup bucket free space / object count regression.

These map cleanly onto the Grafana dashboards in #146 and the SLOs in #147
once they land.

## Related

- [DR drill checklist](dr-drill-checklist.md)
- [`SECURITY.md`](../../SECURITY.md) — disclosure timelines that may force
  out-of-cycle restores.
- [`docs/RELEASING.md`](../RELEASING.md) — release runbook (separate from
  recovery).
