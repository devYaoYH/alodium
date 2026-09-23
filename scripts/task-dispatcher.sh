#!/usr/bin/env bash
# Thin wrapper. The dispatcher lives in scripts/task_dispatcher.py, with its
# decisions in scripts/node_dispatch/ and what it shares with deploy-watch in
# scripts/node_host/.
#
#   ./scripts/task-dispatcher.sh            # one pass (the scheduler provides the loop)
#   ./scripts/task-dispatcher.sh --dry-run  # validate + report, run nothing
#
# This file stays because it is the path everything already knows:
# host/dispatch/node.dispatch.plist runs it on every doorbell and every 10
# minutes, and up.sh, the docs and the request-task skill name it. Moving the
# entrypoint would have meant editing a live launchd job in order to land a
# refactor, which is the wrong order.
#
# It carries no logic on purpose. What keeps dispatch safe to automate — only
# TRACKED briefs marked `dispatch: auto` run, only the OPERATOR's assignment
# authorizes an issue run, the issue body never reaches a tenant — is now pure
# Python with offline tests (scripts/node_dispatch/test_*.py) and a replay of
# scenarios recorded from the bash, instead of shell that could only be checked
# by filing issues at the live node.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/task_dispatcher.py "$@"
