#!/usr/bin/env bash
set -euo pipefail
# Smoke: openapi served and shape looks right
URL="${1:-http://radicale-toolshim:8100/openapi.json}"
json=$(curl -fsS "$URL")
echo "$json" | grep -q '"title": *"radicale-tools"' && echo "ok: openapi served" || { echo "fail: openapi missing" >&2; exit 1; }
