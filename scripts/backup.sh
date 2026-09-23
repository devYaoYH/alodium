#!/usr/bin/env bash
# Thin wrapper. The backup lives in scripts/backup.py and scripts/node_backup/.
#
#   ./scripts/backup.sh passphrase set   # once: into the platform keyring
#   ./scripts/backup.sh init     # once, after ~/.alodium/backup.env exists
#   ./scripts/backup.sh          # then schedule it (PR 4 owns scheduling)
#
# This file stays so README, docs, launchd jobs and anything else that learned
# the old path keep working. It carries no logic on purpose: everything
# load-bearing in a backup is a decision — include vs skip vs missing, dump vs
# degraded vs never-ran, which retention pool a snapshot belongs to — and those
# now live in pure Python with offline tests (scripts/node_backup/test_*.py),
# instead of in shell that could only be checked against a live daemon.
#
# The one choice made here is the interpreter. The platform keyring (the
# default passphrase source) needs the `keyring` package, and PEP 668 Pythons
# refuse a bare pip install, so it goes in a venv under ~/.alodium — the
# host-side root, outside the replaceable checkout. That venv is used when it
# exists. Otherwise the system python3 runs everything, which is enough when
# backup.env names its own passphrase source.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=python3
VENV_PY="${ALODIUM_HOME:-$HOME/.alodium}/venv/bin/python3"
[[ -x "$VENV_PY" ]] && PY="$VENV_PY"
exec "$PY" scripts/backup.py "$@"
