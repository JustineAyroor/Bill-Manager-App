#!/usr/bin/env bash
# Lightweight local SQLite backup, run daily via a systemd timer and also
# once before every deploy (see deploy.sh) right before running migrations.
#
# This is deliberately NOT the only backup: it protects against a bad
# migration or a bad edit that needs a fast, single-file rollback. Real
# off-VM disaster recovery is handled separately by a GCE daily boot-disk
# snapshot schedule (see docs/decisions/.../10-deployment-implementation.md),
# which needs no credentials on the VM at all.
#
# No `sqlite3` CLI is installed on this minimal Ubuntu image, so the actual
# hot-backup copy is done via Python's stdlib sqlite3 module (same technique
# used one-off during the systemd migration).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${REPO_DIR}/tmobile.db"
BACKUP_DIR="${HOME}/backups"
RETENTION_DAYS=14

mkdir -p "${BACKUP_DIR}"

if [ ! -f "${DB_PATH}" ]; then
  echo "backup_db.sh: no DB at ${DB_PATH}, nothing to back up" >&2
  exit 0
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DEST="${BACKUP_DIR}/tmobile-${TIMESTAMP}.db"

python3 - "$DB_PATH" "$DEST" << 'PYEOF'
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
dst.close()
src.close()
PYEOF

gzip -f "${DEST}"
echo "backup_db.sh: wrote ${DEST}.gz"

# Prune local backups older than RETENTION_DAYS - the disk snapshot schedule
# is the long-term/off-VM copy, so these are just a short rollback window.
find "${BACKUP_DIR}" -name 'tmobile-*.db.gz' -mtime +"${RETENTION_DAYS}" -delete
