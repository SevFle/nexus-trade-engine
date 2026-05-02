# DR Drill Checklist

Run this drill **at least once per quarter**. The goal is not to "pass" — it
is to find the gap before a real incident does. Record every drill (date,
operator, observations, RTO actually achieved) in your operations log.

## Pre-Drill (T-1 day)

- [ ] Pick a date with at least one other engineer available as a co-pilot.
- [ ] Notify stakeholders that a drill will run. State that **no production
      writes will be affected** — the drill restores into a sandbox.
- [ ] Confirm the latest base backup is < 36 hours old.
- [ ] Confirm WAL archive is current (no `archive_failures_total`).
- [ ] Confirm the Fernet key version in the secrets vault matches what
      production is using.

## Drill (T+0)

### 1. Restore from logical dump (15 min target)

- [ ] Pull the most recent logical dump from the bucket.
- [ ] Restore into a sandbox Postgres at the same major version.
- [ ] Run `alembic current` and confirm the schema head matches production.
- [ ] Run a representative read query (e.g. `SELECT count(*) FROM users`).

### 2. Point-in-time recovery (45 min target)

- [ ] Pick a recovery target ~30 minutes in the past (real-clock-aligned).
- [ ] Run `scripts/ops/pg_pitr_restore.sh` against the sandbox.
- [ ] Confirm Postgres reaches `recovery_target` and is promoted.
- [ ] Verify the recovered DB does **not** contain rows committed after
      the recovery target. Pick a row inserted between the target and
      "now" and confirm its absence.

### 3. Application bring-up

- [ ] Repoint a sandbox engine instance at the recovered DB.
- [ ] Hit `/api/v1/health` — expect `200`.
- [ ] Log in with an MFA-enabled test user. Confirm the code is accepted
      (verifies the Fernet key recovery path).
- [ ] Hit a representative read (e.g. `GET /api/v1/strategies`) and a
      representative write (e.g. `POST /api/v1/webhooks`) and confirm the
      write lands.

### 4. Tear down

- [ ] Drop the sandbox database and instance.
- [ ] Remove the temporary IAM credentials used for the drill.

## Post-Drill (T+1 day)

- [ ] Record actual RTO measured during the drill.
- [ ] Open issues for every gap found (missing alerts, slow steps, broken
      docs, undocumented manual steps). Tag them `priority-high` if they
      would have extended RTO during a real incident.
- [ ] Update `docs/operations/backup-and-recovery.md` with anything you
      learned.
- [ ] Confirm no production resources were left running.

## What to Practice Quarterly

Rotate the scenario each quarter so the team hits every failure mode
within a year:

| Quarter | Scenario                                                     |
|---------|--------------------------------------------------------------|
| Q1      | Logical dump restore to fresh DB.                            |
| Q2      | PITR to a timestamp.                                         |
| Q3      | PITR to a transaction id (read from logs).                   |
| Q4      | Full DR — primary destroyed, restore in a different region.  |

If you only ever rehearse Q1, you have not actually tested PITR.

## Failure Modes to Force

When you have time, deliberately introduce these and verify the runbook
still works:

- The Fernet key version differs between the running engine and what is
  in the vault.
- The latest base backup is corrupt — fall back to the prior one.
- The WAL bucket is missing a segment — confirm the alert fires before
  you have to recover.
- Alembic head differs between dump and code — confirm the operator
  knows whether to upgrade or downgrade.
