#!/usr/bin/env bash
# Thin wrapper. The backup lives in scripts/backup.py and scripts/node_backup/.
#
#   ./scripts/backup.sh init     # once, after ~/.alodium/backup.env exists
#   ./scripts/backup.sh          # then schedule it (PR 4 owns scheduling)
#
# This file stays so README, docs, launchd jobs and anything else that learned
# the old path keep working. It carries no logic on purpose: everything
# load-bearing in a backup is a decision — include vs skip vs missing, dump vs
# degraded vs never-ran, which retention pool a snapshot belongs to — and those
# now live in pure Python with offline tests (scripts/node_backup/test_*.py),
# instead of in shell that could only be checked against a live daemon.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/backup.py "$@"
