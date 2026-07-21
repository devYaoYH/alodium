#!/usr/bin/env bash
# Idempotent helper: ensure the four difficulty:* labels exist in the
# coordination repo. Operator-run once (or whenever the tier table changes).
#
#   ./scripts/ensure-tier-labels.sh            # create missing labels
#   ./scripts/ensure-tier-labels.sh --verify   # dry-run: validate label definitions (no API calls)
#
# Safe to re-run: the script checks for existence via the API before
# creating, so duplicate creation is avoided (Forgejo does NOT
# deduplicate by name — see task-dispatcher.sh's triplication warning).
#
# The --verify mode is used by verify-config.sh to catch quoting/encoding
# issues in label definitions at PR time, before they can cause runtime
# failures (e.g. the shell-interpolation bug that hit issue #26 comment #649).
set -euo pipefail
cd "$(dirname "$0")/.."

VERIFY=0
if [[ "${1:-}" == "--verify" ]]; then
  VERIFY=1
fi

if [[ "$VERIFY" -eq 0 ]]; then
  set -a; source .env; set +a
  A() { /usr/bin/curl -sk --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \
        -H "Authorization: token $AGENT_FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"; }
  GAPI="https://git.${NODE_DOMAIN}/api/v1/repos/${COORDINATION_REPO}"
fi

# The four tiers, ordered. Color is the same blue-green as in-progress.
# Safe to re-run: the explicit pre-check below avoids the triplication
# problem (Forgejo does NOT deduplicate by name).
LABELS=(
  "difficulty:trivial:#00b894:Trivial — quick edit, no structural change"
  "difficulty:easy:#00cec9:Easy — single file, well-understood change"
  "difficulty:moderate:#74b9ff:Moderate — multi-file, needs design attention"
  "difficulty:hard:#a29bfe:Hard — cross-cutting, risky, or complex"
)

# Shared helper: validate a single label definition by constructing its
# JSON payload via python (same code path as the real creation). Exits 1
# on failure so the caller can catch quoting/encoding issues.
validate_label_json() {
  local name="$1" color="$2" desc="$3"
  python3 -c '
import json,sys
name, color, desc = sys.argv[1], sys.argv[2], sys.argv[3]
payload = json.dumps({"name": name, "color": color, "description": desc})
# Round-trip: parse and re-dump to catch any encoding issues
json.loads(payload)
print(payload)
' "$name" "$color" "$desc" >/dev/null 2>&1
}

FAIL=0
for entry in "${LABELS[@]}"; do
  IFS=: read -r name color desc <<<"$entry"

  # Validate label definition in all modes (verify + normal). Catches
  # quoting/encoding issues at PR time (verify-config.sh) AND at runtime.
  if ! validate_label_json "$name" "$color" "$desc"; then
    echo "FAIL: label '$name' — JSON construction failed (quoting/encoding error)"
    FAIL=1
    continue
  fi
  [[ "$VERIFY" -eq 1 ]] && echo "OK: label '$name' definition valid" && continue

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
      -d "$(python3 -c '
import json,sys
name, color, desc = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({"name": name, "color": color, "description": desc}))
' "$name" "$color" "$desc")" \
      >/dev/null
    echo "created label '$name'"
  fi
done

if [[ "$VERIFY" -eq 1 ]]; then
  if [[ "$FAIL" -eq 0 ]]; then
    echo "ensure-tier-labels: all label definitions valid"
  else
    echo "ensure-tier-labels: FAIL — fix label definitions above"
  fi
  exit "$FAIL"
fi

echo "ensure-tier-labels: done"