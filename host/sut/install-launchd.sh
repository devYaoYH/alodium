#!/usr/bin/env bash
# Install the host-owned SUT dispatcher after `sutctl.sh init` succeeds.
# host/sut/setup.sh runs this as its last step.
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ "$(uname -s)" == "Darwin" ]] || { echo "sut dispatcher: use cron/systemd on Linux" >&2; exit 2; }
./host/sut/sutctl.sh doctor >/dev/null
ROOT="$PWD"
mkdir -p "$ROOT/.task-sut" "$HOME/Library/LaunchAgents"
DST="$HOME/Library/LaunchAgents/node.sutwatch.plist"
# launchd gives jobs a minimal PATH; bake in the directories that hold the
# colima and docker this shell resolves (sutctl.sh also appends the usual ones).
BIN_DIR="$(dirname "$(command -v colima)")"
DOCKER_BIN_DIR="$(dirname "$(command -v docker)")"
sed -e "s#/REPLACE/with/abs/path/to/alodium#$ROOT#g" \
    -e "s#/REPLACE/with/command/dir#$BIN_DIR:$DOCKER_BIN_DIR#g" \
    host/sut/node.sutwatch.plist > "$DST"
launchctl unload "$DST" 2>/dev/null || true
launchctl load "$DST"
echo "SUT dispatcher installed: every 2 minutes it queues PRs the operator labeled '${SUT_LABEL:-requires-sut}'."
