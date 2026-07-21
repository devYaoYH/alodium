#!/usr/bin/env bash
# Idempotent helper: ensure the four difficulty:* labels exist in the
# coordination repo. Operator-run once (or whenever the tier table changes).
#
#   ./scripts/ensure-tier-labels.sh
#
# Safe to re-run: label creation is a no-op if the label already exists
# (Forgejo ignores duplicate label creation by name).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

A() { /usr/bin/curl -sk --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \
      -H "Authorization: token $AGENT_FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"; }
GAPI="https://git.${NODE_DOMAIN}/api/v1/repos/${COORDINATION_REPO}"

# The four tiers, ordered. Color is the same blue-green as in-progress;
# the label name is the primary key — Forgejo deduplicates by name.
LABELS=(
  "difficulty:trivial:#00b894:Trivial — quick edit, no structural change"
  "difficulty:easy:#00cec9:Easy — single file, well-understood change"
  "difficulty:moderate:#74b9ff:Moderate — multi-file, needs design attention"
  "difficulty:hard:#a29bfe:Hard — cross-cutting, risky, or complex"
)

for entry in "${LABELS[@]}"; do
  IFS=: read -r name color desc <<<"$entry"
  # Check if already exists (name compare, case-insensitive by Forgejo)
  EXISTING=$(A "$GAPI/labels?limit=100" | python3 -c "
import json,sys
labels = json.load(sys.stdin)
for l in labels:
    if l['name'].lower() == '$name'.lower():
        print(l['name'])
        break
")
  if [[ -n "$EXISTING" ]]; then
    echo "label '$name' already exists (as '$EXISTING'); skipping"
  else
    A -X POST "$GAPI/labels" \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"name":"'$name'","color":"'$color'","description":"'$desc'"}))')" \
      >/dev/null
    echo "created label '$name'"
  fi
done

echo "ensure-tier-labels: done"