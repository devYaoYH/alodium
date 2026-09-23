#!/usr/bin/env bash
# Thin wrapper. The deploy lives in scripts/deploy.py and scripts/node_deploy/.
#
#   ./scripts/deploy.sh
#
# This file stays because it is the path everything already knows: the launchd
# job host/deploy-watch/node.deploywatch.plist runs scripts/deploy-watch.sh,
# which runs THIS, every two minutes; the README, docs and the propose-change
# skill all name it. Moving the entrypoint would have meant editing a live
# launchd job in order to land a refactor, which is the wrong order.
#
# It carries no logic on purpose: everything load-bearing in a deploy is a
# decision — what changed since the last deploy, which images that implies
# rebuilding, which containers need restarting, which profiles must be visible,
# whether the result is ok / warning / failed — and those now live in pure
# Python with offline tests (scripts/node_deploy/test_*.py), instead of in
# shell that could only be checked by merging something and watching.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/deploy.py "$@"
