#!/usr/bin/env bash
# Take a physical base backup with pg_basebackup and upload it to object
# storage. The base backup is what PITR replays WAL forward from.
#
# Required env:
#   PGHOST, PGUSER, PGPASSWORD
#   BACKUP_DEST   e.g. s3://your-bucket/base/   (must end with /)
#
# Optional env:
#   AWS_CMD     aws CLI binary (default: aws)
#   COMPRESS    gzip|none      (default: gzip)
#
# Reference: docs/operations/backup-and-recovery.md

set -euo pipefail

: "${PGHOST:?PGHOST is required}"
: "${PGUSER:?PGUSER is required}"
: "${BACKUP_DEST:?BACKUP_DEST is required (e.g. s3://your-bucket/base/)}"

AWS_CMD="${AWS_CMD:-aws}"
COMPRESS="${COMPRESS:-gzip}"

case "$BACKUP_DEST" in
  */) ;;
  *) BACKUP_DEST="${BACKUP_DEST}/" ;;
esac

stamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
prefix="${stamp}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

work="${tmp}/${prefix}"
mkdir -p "$work"

echo "[pg_basebackup] streaming base backup -> ${work}"
gzip_arg=""
if [ "$COMPRESS" = "gzip" ]; then
  gzip_arg="--gzip"
fi

PGPASSWORD="${PGPASSWORD:-}" pg_basebackup \
  --host="$PGHOST" \
  --username="$PGUSER" \
  --pgdata="$work" \
  --format=tar \
  ${gzip_arg} \
  --progress \
  --wal-method=fetch \
  --checkpoint=fast \
  --verbose

echo "[pg_basebackup] uploading -> ${BACKUP_DEST}${prefix}/"
"$AWS_CMD" s3 cp --no-progress --recursive "$work" "${BACKUP_DEST}${prefix}/"

echo "[pg_basebackup] done"
