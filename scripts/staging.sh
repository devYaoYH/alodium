#!/usr/bin/env bash
# Staging lifecycle: the same stack, a second project, throwaway volumes.
#
#   ./scripts/staging.sh up      # bring up the staging twin (all app profiles)
#   ./scripts/staging.sh ps      # what's running
#   ./scripts/staging.sh down    # tear down AND destroy staging volumes
#
# Used by promote.sh; also the target of the quarterly restore drill
# (restore snapshots into staging, verify, record the date in the manifest).
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=sovereign-staging
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.staging.yml
         --profile apps --profile feeds)

case "${1:-}" in
  up)   "${COMPOSE[@]}" up -d --quiet-pull ;;
  ps)   "${COMPOSE[@]}" ps --format 'table {{.Name}}\t{{.Status}}' ;;
  down) "${COMPOSE[@]}" down -v ;;   # -v: staging state is throwaway BY DESIGN
  *)    echo "usage: staging.sh up|ps|down"; exit 1 ;;
esac
