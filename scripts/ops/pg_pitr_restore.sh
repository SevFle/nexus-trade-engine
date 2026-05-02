#!/usr/bin/env bash
# Restore Postgres to a point in time by:
#   1. Downloading the most recent base backup that predates the recovery target
#   2. Materializing it into $PGDATA
#   3. Writing recovery.signal + the right restore_command / recovery_target_*
#   4. Letting Postgres replay WAL until the target, then promote
#
# This is intentionally written for the most common operator stack
# (Postgres 14+, S3-compatible object storage, vanilla pg_basebackup output).
# Production deployments should consider pgbackrest / wal-g instead.
#
# Required env:
#   BACKUP_DEST           e.g. s3://your-bucket
#   PGDATA                target data directory (must NOT contain a live cluster)
#
# Exactly one of these recovery targets:
#   RECOVERY_TARGET_TIME  e.g. '2026-05-01 14:33:00 UTC'
#   RECOVERY_TARGET_XID   e.g. '12345678'
#   RECOVERY_TARGET_NAME  named restore point
#
# Optional env:
#   AWS_CMD                aws CLI binary    (default: aws)
#   PG_CTL                 pg_ctl binary     (default: pg_ctl)
#   POLL_INTERVAL_SEC      promote check     (default: 5)
#   PROMOTE_TIMEOUT_SEC    abort after N sec (default: 1800)
#
# Reference: docs/operations/backup-and-recovery.md

set -euo pipefail

: "${BACKUP_DEST:?BACKUP_DEST is required (e.g. s3://your-bucket)}"
: "${PGDATA:?PGDATA is required}"

AWS_CMD="${AWS_CMD:-aws}"
PG_CTL="${PG_CTL:-pg_ctl}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-5}"
PROMOTE_TIMEOUT_SEC="${PROMOTE_TIMEOUT_SEC:-1800}"

case "$BACKUP_DEST" in
  */) BACKUP_DEST="${BACKUP_DEST%/}" ;;
esac

target_count=0
[ -n "${RECOVERY_TARGET_TIME:-}" ] && target_count=$((target_count + 1))
[ -n "${RECOVERY_TARGET_XID:-}"  ] && target_count=$((target_count + 1))
[ -n "${RECOVERY_TARGET_NAME:-}" ] && target_count=$((target_count + 1))
if [ "$target_count" -ne 1 ]; then
  echo "[pitr] ERROR: set exactly one of RECOVERY_TARGET_{TIME,XID,NAME}" >&2
  exit 2
fi

if [ -e "${PGDATA}/PG_VERSION" ]; then
  echo "[pitr] ERROR: ${PGDATA} already contains a cluster — refuse to overwrite" >&2
  exit 2
fi

mkdir -p "$PGDATA"
chmod 700 "$PGDATA"

# Pick the most recent base backup whose prefix sorts <= the target time.
echo "[pitr] listing base backups under ${BACKUP_DEST}/base/"
listing=$("$AWS_CMD" s3 ls "${BACKUP_DEST}/base/" | awk '{print $2}' | sed 's:/$::' | sort)
if [ -z "$listing" ]; then
  echo "[pitr] ERROR: no base backups found at ${BACKUP_DEST}/base/" >&2
  exit 1
fi

if [ -n "${RECOVERY_TARGET_TIME:-}" ]; then
  target_iso=$(date -u -d "$RECOVERY_TARGET_TIME" +%Y-%m-%dT%H-%M-%SZ 2>/dev/null \
    || date -u -j -f "%Y-%m-%d %H:%M:%S %Z" "$RECOVERY_TARGET_TIME" +%Y-%m-%dT%H-%M-%SZ)
  base=$(printf '%s\n' "$listing" | awk -v t="$target_iso" '$0 <= t' | tail -n1)
else
  base=$(printf '%s\n' "$listing" | tail -n1)
fi

if [ -z "$base" ]; then
  echo "[pitr] ERROR: no base backup predates the recovery target" >&2
  exit 1
fi

echo "[pitr] using base backup: ${base}"
"$AWS_CMD" s3 cp --no-progress --recursive "${BACKUP_DEST}/base/${base}/" "${PGDATA}/"

# Untar any tar.gz archives produced by pg_basebackup --format=tar --gzip
shopt -s nullglob
for archive in "${PGDATA}"/*.tar.gz; do
  echo "[pitr] extracting ${archive}"
  tar -xzf "$archive" -C "${PGDATA}"
  rm -f "$archive"
done
shopt -u nullglob

# Restore command: download a WAL segment from the archive on demand.
restore_cmd="${AWS_CMD} s3 cp ${BACKUP_DEST}/wal/%f %p --no-progress"

{
  echo "# Written by pg_pitr_restore.sh"
  echo "restore_command = '${restore_cmd}'"
  if [ -n "${RECOVERY_TARGET_TIME:-}" ]; then
    echo "recovery_target_time = '${RECOVERY_TARGET_TIME}'"
  elif [ -n "${RECOVERY_TARGET_XID:-}" ]; then
    echo "recovery_target_xid = '${RECOVERY_TARGET_XID}'"
  elif [ -n "${RECOVERY_TARGET_NAME:-}" ]; then
    echo "recovery_target_name = '${RECOVERY_TARGET_NAME}'"
  fi
  echo "recovery_target_action = 'promote'"
} >> "${PGDATA}/postgresql.auto.conf"

touch "${PGDATA}/recovery.signal"

echo "[pitr] starting Postgres in recovery mode"
"$PG_CTL" -D "$PGDATA" -l "${PGDATA}/recovery.log" start

deadline=$(( $(date +%s) + PROMOTE_TIMEOUT_SEC ))
while true; do
  if [ ! -f "${PGDATA}/recovery.signal" ] && [ ! -f "${PGDATA}/standby.signal" ]; then
    echo "[pitr] cluster promoted — recovery target reached"
    break
  fi
  if [ "$(date +%s)" -gt "$deadline" ]; then
    echo "[pitr] ERROR: promote timed out after ${PROMOTE_TIMEOUT_SEC}s — see recovery.log" >&2
    exit 1
  fi
  sleep "$POLL_INTERVAL_SEC"
done

echo "[pitr] done. Take a fresh base backup before serving traffic."
