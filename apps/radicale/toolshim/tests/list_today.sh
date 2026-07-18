#!/usr/bin/env bash
set -euo pipefail
# List today's events; accept empty list — shape must match (no jq dependency)
URL="${1:-http://radicale-toolshim:8100/events/today}"
json=$(curl -fsS "$URL")
# Expect an "events": [ ... ] array in the JSON
if echo "$json" | grep -E '"events"[[:space:]]*:' | grep -q '\['; then
  echo "ok: events array present"
else
  echo "fail: events array missing" >&2
  echo "$json" | sed 's/^/  /'
  exit 1
fi
