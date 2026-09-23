#!/usr/bin/env bash
# Thin wrapper. The watcher lives in scripts/deploy_watch.py.
#
#   ./scripts/deploy-watch.sh            # one pass (the scheduler provides the loop)
#   ./scripts/deploy-watch.sh --dry-run  # report what it would do; change nothing
#
# This file stays because host/deploy-watch/node.deploywatch.plist runs it
# every two minutes and the docs teach it; moving a live launchd job's target
# to land a refactor is the wrong order. No logic here: the pass — clean-main
# gate, deployed-hash trigger, divergence refusal, one `blocked` report per
# failed tip — is Python with a replay of scenarios recorded from the bash
# (scripts/node_deploy/test_watch.py). It still never pulls: deploy.py does the
# fast-forward, and diffs the pre-deploy HEAD to decide what to rebuild.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/deploy_watch.py "$@"
