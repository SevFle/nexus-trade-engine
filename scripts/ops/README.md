# Operations scripts

Operator-facing helper scripts. These are **examples** — review them, fork
them, and adapt to your storage backend, secrets model, and scheduler
before depending on them.

| Script | Purpose | Trigger |
|--------|---------|---------|
| `pg_logical_backup.sh` | Daily `pg_dump` (custom format) → object storage. Prunes by `RETENTION_DAYS`. | Cron, e.g. `0 2 * * *` |
| `pg_basebackup.sh`     | Periodic physical base backup (`pg_basebackup` tar+gzip) → object storage. | Cron, e.g. `0 3 * * *` |
| `pg_pitr_restore.sh`   | Restore a base backup into a fresh `$PGDATA`, then replay WAL to a recovery target and promote. | Manual, during recovery / DR drill |

See [`docs/operations/backup-and-recovery.md`](../../docs/operations/backup-and-recovery.md)
for the runbook these scripts implement, and
[`docs/operations/dr-drill-checklist.md`](../../docs/operations/dr-drill-checklist.md)
for the quarterly drill that exercises them.

## Prerequisites

- `aws` CLI (or set `AWS_CMD` to the equivalent for your object store).
- `pg_dump`, `pg_basebackup`, `pg_ctl` matching the running Postgres major version.
- An IAM role / credential with read+write on the backup bucket and read on
  the WAL prefix.

## Production guidance

For a production deployment we recommend swapping these scripts for
[pgbackrest](https://pgbackrest.org/) or [wal-g](https://github.com/wal-g/wal-g):
both consolidate base backups + WAL archiving, support incremental
backups, ship encryption, and have first-class restore tooling.
