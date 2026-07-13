#!/usr/bin/env bash
# Encrypted off-site backup of all node volumes via restic.
# This box is your identity; a backup you haven't restored is a rumor.
#
# Setup:
#   cp scripts/backup.env.example scripts/backup.env   # fill in credentials
#   ./scripts/backup.sh init                            # once
#   ./scripts/backup.sh                                 # then cron it daily:
#   0 3 * * *  cd /path/to/sovereign-node && ./scripts/backup.sh >> /var/log/node-backup.log 2>&1

set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="scripts/backup.env"
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE (see backup.env.example)"; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

VOLUMES_ROOT="$(docker volume inspect sovereign-node_forgejo_data --format '{{ .Mountpoint }}' | xargs dirname | xargs dirname)"
TARGETS=(
  "$VOLUMES_ROOT/sovereign-node_forgejo_data"
  "$VOLUMES_ROOT/sovereign-node_litellm_db"
  "$VOLUMES_ROOT/sovereign-node_radicale_data"
  "$VOLUMES_ROOT/sovereign-node_caddy_data"
  "$VOLUMES_ROOT/sovereign-node_pocketid_data"   # identity: SQLite + passkey public halves
)

if [[ "${1:-}" == "init" ]]; then
  restic init
  exit 0
fi

# Consistent DB snapshot: dump Postgres instead of copying live files.
docker exec litellm-db pg_dump -U litellm litellm > /tmp/litellm.sql

restic backup "${TARGETS[@]}" /tmp/litellm.sql \
  --tag sovereign-node --exclude-caches

restic forget --tag sovereign-node --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune

rm -f /tmp/litellm.sql
echo "backup complete: $(date -Is)"

# Restore drill (quarterly, minimum):
#   restic snapshots
#   restic restore latest --target /tmp/restore-drill
# Then update manifest/node.yaml -> backups.tested_restore with today's date.
