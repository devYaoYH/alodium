#!/usr/bin/env bash
# Materialize node.deploywatch.plist with this checkout's real paths and load
# it. Idempotent: rewrites + reloads each run (paths may have moved). darwin
# only — same shape as host/dispatch/install-launchd.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
mkdir -p "$ROOT/.task-dispatch"

DST="$HOME/Library/LaunchAgents/node.deploywatch.plist"
mkdir -p "$HOME/Library/LaunchAgents"

# launchd gets a minimal PATH; deploy.sh needs `docker`. Detect its dir.
DOCKER_BIN="$(command -v docker || true)"
DOCKER_DIR="$(dirname "${DOCKER_BIN:-/usr/local/bin/docker}")"

sed -e "s#/REPLACE/with/abs/path/to/alodium/scripts/deploy-watch.sh#$ROOT/scripts/deploy-watch.sh#g" \
    -e "s#/REPLACE/with/abs/path/to/alodium/.task-dispatch/deploy-watch.log#$ROOT/.task-dispatch/deploy-watch.log#g" \
    -e "s#/REPLACE/with/abs/path/to/alodium#$ROOT#g" \
    -e "s#/REPLACE/with/docker/dir#$DOCKER_DIR#g" \
    host/deploy-watch/node.deploywatch.plist > "$DST"

launchctl unload "$DST" 2>/dev/null || true
launchctl load "$DST"
echo "   launchd job node.deploywatch loaded (polls forgejo/main every 2 min)"
