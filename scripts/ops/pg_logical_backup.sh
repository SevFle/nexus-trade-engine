#!/usr/bin/env bash
# Take a logical pg_dump of the engine DB and upload it to object storage.
#
# Required env:
#   PGHOST, PGUSER, PGPASSWORD, PGDATABASE
#   BACKUP_DEST   e.g. s3://your-bucket/logical/   (must end with /)
#
# Optional env:
#   RETENTION_DAYS    purge older dumps after N days (default: 30)
#   DUMP_JOBS         parallel jobs (default: 4)
#   AWS_CMD           aws CLI binary (default: aws)
#
# Reference: docs/operations/backup-and-recovery.md

set -euo pipefail

: "${PGHOST:?PGHOST is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${BACKUP_DEST:?BACKUP_DEST is required (e.g. s3://your-bucket/logical/)}"

RETENTION_DAYS="${RETENTION_DAYS:-30}"
DUMP_JOBS="${DUMP_JOBS:-4}"
AWS_CMD="${AWS_CMD:-aws}"

case "$BACKUP_DEST" in
  */) ;;
  *) BACKUP_DEST="${BACKUP_DEST}/" ;;
esac

stamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
key="${stamp}-${PGDATABASE}.dump"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

dumpfile="${tmp}/${key}"

echo "[pg_logical_backup] dumping ${PGDATABASE} -> ${dumpfile}"
PGPASSWORD="${PGPASSWORD:-}" pg_dump \
  --host="$PGHOST" \
  --username="$PGUSER" \
  --dbname="$PGDATABASE" \
  --format=custom \
  --jobs="$DUMP_JOBS" \
  --no-owner \
  --no-privileges \
  --file="$dumpfile" \
  --verbose

bytes=$(wc -c < "$dumpfile" | tr -d ' ')
echo "[pg_logical_backup] dump complete: ${bytes} bytes"

echo "[pg_logical_backup] uploading -> ${BACKUP_DEST}${key}"
"$AWS_CMD" s3 cp --no-progress "$dumpfile" "${BACKUP_DEST}${key}"

# Best-effort retention pass. Fails closed (warns but exits 0).
echo "[pg_logical_backup] pruning objects older than ${RETENTION_DAYS} days"
cutoff=$(date -u -v-"${RETENTION_DAYS}"d +%s 2>/dev/null \
  || date -u -d "${RETENTION_DAYS} days ago" +%s)

"$AWS_CMD" s3 ls "$BACKUP_DEST" | while read -r line; do
  obj_date=$(echo "$line" | awk '{print $1, $2}')
  obj_name=$(echo "$line" | awk '{print $4}')
  [ -z "$obj_name" ] && continue
  obj_ts=$(date -u -j -f "%Y-%m-%d %H:%M:%S" "$obj_date" +%s 2>/dev/null \
    || date -u -d "$obj_date" +%s 2>/dev/null \
    || true)
  if [ -n "$obj_ts" ] && [ "$obj_ts" -lt "$cutoff" ]; then
    echo "[pg_logical_backup]   deleting expired ${obj_name}"
    "$AWS_CMD" s3 rm "${BACKUP_DEST}${obj_name}" || \
      echo "[pg_logical_backup]   WARN failed to delete ${obj_name}"
  fi
done

echo "[pg_logical_backup] done"
