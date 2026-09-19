#!/usr/bin/env bash
# One command to install, or repair, the isolated SUT gate on a macOS node.
# Idempotent: re-run it after SUT changes merge, or whenever doctor complains.
#
#   ./host/sut/setup.sh
#
# Afterwards, request a test by adding the `requires-sut` label to a
# node-config PR (as the operator). Progress and results arrive as PR comments.
set -euo pipefail
cd "$(dirname "$0")/../.."
PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
step() { echo "== $*"; }
[[ "$(uname -s)" == "Darwin" ]] || { echo "setup.sh provisions Colima on macOS; see host/sut/README.md for Linux" >&2; exit 2; }

step "1/5 prerequisites"
command -v colima >/dev/null || { echo "   Colima is not installed. Run: brew install colima" >&2; exit 2; }
command -v docker >/dev/null || { echo "   No docker CLI found (Docker Desktop, or: brew install docker)" >&2; exit 2; }
grep -q '^SUT_FORGEJO_TOKEN=..*' .env 2>/dev/null || {
  echo "   .env has no SUT_FORGEJO_TOKEN. Mint it with: ENABLE_SUT=1 ./scripts/bootstrap-forgejo.sh" >&2; exit 2; }
echo "   colima, docker and the SUT token are present"

step "2/5 worker profile"
if [[ -f .task-sut/config.env ]]; then
  echo "   already configured ($(grep '^SUT_PROFILE=' .task-sut/config.env))"
  # Installs from before the stack grew have 2 CPUs, on which redash's workers
  # miss their boot timeout. Single-use workers pick this up on the next run.
  if grep -qx 'SUT_CPUS=[123]' .task-sut/config.env; then
    sed -i '' 's/^SUT_CPUS=.*/SUT_CPUS=4/' .task-sut/config.env
    echo "   raised SUT_CPUS to 4"
  fi
else
  ./host/sut/sutctl.sh init
fi

step "3/5 request label"
./host/sut/sutctl.sh label
echo "   '${SUT_LABEL:-requires-sut}' exists on node-config"

step "4/5 leftovers from the old watcher"
# The pre-queue watcher kept its lock at .task-sut/watch.lock. A run killed
# mid-way left it behind, and every later pass then exited quietly.
if [[ -d .task-sut/watch.lock ]]; then rm -rf .task-sut/watch.lock; echo "   removed stale watch.lock"; else echo "   none"; fi

step "5/5 scheduled dispatcher"
./host/sut/install-launchd.sh
./host/sut/sutctl.sh doctor
