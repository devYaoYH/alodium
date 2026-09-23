#!/usr/bin/env bash
# Thin wrapper. One detached issue-work run lives in scripts/dispatch_run.py.
#
#   scripts/dispatch-run.sh <issue-number>
#
# Not called by humans: the dispatcher spawns scripts/dispatch_run.py directly
# (no bash needed to detach it). This path stays for anything that learned it
# — a drill, a manual re-run from the host, host/dispatch/README.md.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/dispatch_run.py "$@"
