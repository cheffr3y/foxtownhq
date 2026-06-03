#!/usr/bin/env bash
# Run this before any destructive migration (commissary removal, schema changes).
# Requires DATABASE_URL in the environment, or pass it as the first argument.
# Output: a timestamped .sql.gz file in the current directory.
#
# Usage:
#   ./scripts/backup_db.sh                   # uses $DATABASE_URL
#   ./scripts/backup_db.sh postgres://...    # explicit URL

set -euo pipefail

DB_URL="${1:-${DATABASE_URL:-}}"

if [[ -z "$DB_URL" ]]; then
  echo "ERROR: No database URL. Set DATABASE_URL or pass it as an argument." >&2
  exit 1
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT="foxtownhq_backup_${TIMESTAMP}.sql.gz"

echo "Backing up to ${OUTPUT} ..."
pg_dump --no-owner --no-acl "$DB_URL" | gzip > "$OUTPUT"
echo "Done: ${OUTPUT} ($(du -sh "$OUTPUT" | cut -f1))"
