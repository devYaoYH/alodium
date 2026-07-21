#!/usr/bin/env bash
# One dispatched issue-work run, DETACHED from the dispatcher pass so tenants
# run CONCURRENTLY: task-dispatcher.sh claims the issue (adds `in-progress`),
# spawns this in the background, and moves on. This process owns the run's whole
# lifecycle — the ephemeral tenant, the completion comment, label-on-failure,
# and the audit trail. Not called by humans directly.
#
#   scripts/dispatch-run.sh <issue-number>
#
# Per-issue exclusivity is guaranteed by the caller: the `in-progress` label is
# added BEFORE this spawns, and the dispatcher's issue scan skips labeled
# issues — so at most one of these runs per issue at a time. Spend is bounded by
# LiteLLM (each run mints its own budget-capped key; a global budget, if set,
# bounds the aggregate across all concurrent runs).
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

NUM="${1:?usage: dispatch-run.sh <issue-number>}"
[[ "$NUM" =~ ^[0-9]+$ ]] || { echo "dispatch-run: issue must be an integer"; exit 2; }

OPERATOR_LOGIN="${OPERATOR_LOGIN:-${FORGEJO_ADMIN_USER:-operator}}"

A() { /usr/bin/curl -sk --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \
      -H "Authorization: token $AGENT_FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"; }
GAPI="https://git.${NODE_DOMAIN}/api/v1/repos/${COORDINATION_REPO}"
say() { A -X POST "$GAPI/issues/$1/comments" \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"body":sys.argv[1]}))' "$2")" >/dev/null; }
audit() {  # audit <action> <detail> — one JSON line to the shared audit log
  printf '{"ts":"%s","issue":%s,"action":"%s","run":"%s","detail":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$NUM" "$1" "${RUN:-}" "${2//\"/\'}" >> .task-dispatch/dispatch-audit.log
}
INPROG_ID=$(A "$GAPI/labels?limit=100" \
  | python3 -c 'import json,sys; ids=[l["id"] for l in json.load(sys.stdin) if l["name"]=="in-progress"]; print(ids[0] if ids else "")')

RUN="issue-$NUM"   # coarse tag for the audit line; run-task mints its own key alias
FSTAMP=".task-dispatch/issue-$NUM"

# --- difficulty tier resolution -------------------------------------------------
# Resolve the issue's difficulty label (if any) to a model + budget.
# Gates (each independently sufficient to fall back):
#   1. Label must be applied by the OPERATOR (agent self-labeling is ignored).
#   2. Unknown tier key -> fall back to default tier.
#   3. Target model not live in LiteLLM -> fall back to deepseek-flash + loud comment.
#   4. (Compile-time) verify-config.sh checks tier models exist in litellm.yaml.

DIFF_MODEL=""
DIFF_BUDGET=""
DIFF_SOURCE="brief"   # what resolved the profile: brief|default-tier|difficulty-label|fallback

# Read dispatch-tiers.yaml (Python to parse YAML without yq)
TIER_JSON=$(python3 -c "
import json, sys
try:
    import yaml
    with open('config/dispatch-tiers.yaml') as f:
        d = yaml.safe_load(f)
    print(json.dumps(d))
except Exception as e:
    print(json.dumps({'error': str(e)}))
" 2>/dev/null || echo '{"error":"yaml parse failed"}')

# Check if the operator applied a difficulty:* label
DIFF_LABEL=$(A "$GAPI/issues/$NUM/timeline?limit=100" | python3 -c "
import json,sys
op = sys.argv[1]
try:
    events = json.load(sys.stdin)
except:
    sys.exit(0)
# Walk events in reverse (most recent first) to find the latest operator-add
for e in reversed(events):
    if e.get('type') == 'label' and not e.get('removed', False):
        label_name = (e.get('label') or {}).get('name', '')
        if label_name.startswith('difficulty:'):
            actor = (e.get('user') or {}).get('login', '')
            if actor == op:
                print(label_name)
                sys.exit(0)
print('')
" "$OPERATOR_LOGIN")

if [[ -n "$DIFF_LABEL" ]]; then
  # Gate 1: operator-applied label found. Extract tier key (after 'difficulty:')
  TIER="${DIFF_LABEL#difficulty:}"
  echo "[dispatch-run] #$NUM: operator-applied label '$DIFF_LABEL' -> tier '$TIER'"

  # Gate 2: unknown tier key -> fall back to default
  TIER_RESOLVE=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
if 'error' in data:
    print('error:' + data['error'])
    sys.exit(0)
tiers = data.get('tiers', {})
default_tier = data.get('default', 'moderate')
tier_key = sys.argv[2]
if tier_key not in tiers:
    print('unknown:' + tier_key + '->default:' + default_tier)
    sys.exit(0)
t = tiers[tier_key]
print(t['model'] + ':' + str(t['budget_usd']))
" "$TIER_JSON" "$TIER")

  case "$TIER_RESOLVE" in
    error:*)
      echo "[dispatch-run] #$NUM: ERROR parsing dispatch-tiers.yaml: ${TIER_RESOLVE#error:}"
      # Fall through to brief default
      ;;
    unknown:*)
      echo "[dispatch-run] #$NUM: unknown tier '$TIER' (${TIER_RESOLVE#unknown:}); falling back to brief default"
      say "$NUM" "Difficulty label \`$DIFF_LABEL\` not recognized — tier \`$TIER\` is not in the dispatch table. Falling back to the default tier. Valid tiers: \`trivial\`, \`easy\`, \`moderate\`, \`hard\`."
      ;;
    *:*)
      DIFF_MODEL="${TIER_RESOLVE%%:*}"
      DIFF_BUDGET="${TIER_RESOLVE#*:}"
      DIFF_SOURCE="difficulty-label"
      echo "[dispatch-run] #$NUM: resolved tier '$TIER' -> model=$DIFF_MODEL budget=\"$DIFF_BUDGET\""

      # Gate 3: check if model is live in LiteLLM before launch
      LLM_CHECK=$(/usr/bin/curl -sk --resolve "llm.${NODE_DOMAIN}:443:127.0.0.1" \
        -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-}" \
        "https://llm.${NODE_DOMAIN}/v1/models" 2>/dev/null \
        | python3 -c "
import json,sys
try:
    data = json.load(sys.stdin)
    models = data.get('data', [])
    target = sys.argv[1]
    for m in models:
        mid = m.get('id', '')
        # Check both exact match and 'openrouter/' prefix (LiteLLM rewrites)
        if mid == target or mid.endswith('/' + target):
            print('live')
            sys.exit(0)
    print('not_found')
except Exception as e:
    print('error:' + str(e))
" "$DIFF_MODEL" 2>/dev/null || echo "error:curl_failed")

      if [[ "$LLM_CHECK" != "live" ]]; then
        echo "[dispatch-run] #$NUM: model '$DIFF_MODEL' not live in LiteLLM ($LLM_CHECK); falling back to deepseek-flash"
        say "$NUM" "⚠️ Tier \`$TIER\` resolved to model \`$DIFF_MODEL\` but that model is not currently served by LiteLLM. Falling back to \`deepseek-flash\`. Check \`config/litellm.yaml\` if this persists."
        DIFF_MODEL="deepseek-flash"
        DIFF_BUDGET="0.50"
        DIFF_SOURCE="fallback"
      fi
      ;;
  esac
fi

# If no difficulty label resolved, use the default tier from dispatch-tiers.yaml
if [[ -z "$DIFF_MODEL" ]]; then
  DEFAULT_TIER=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
if 'error' in data:
    sys.exit(0)
default = data.get('default', 'moderate')
tiers = data.get('tiers', {})
t = tiers.get(default, {})
if t:
    print(t.get('model', '') + ':' + str(t.get('budget_usd', 0.50)))
else:
    sys.exit(0)
" "$TIER_JSON" 2>/dev/null || echo "")
  if [[ -n "$DEFAULT_TIER" ]]; then
    DIFF_MODEL="${DEFAULT_TIER%%:*}"
    DIFF_BUDGET="${DEFAULT_TIER#*:}"
    DIFF_SOURCE="default-tier"
    echo "[dispatch-run] #$NUM: no difficulty label, using default tier -> model=$DIFF_MODEL budget=\"$DIFF_BUDGET\""
  fi
fi

# Build the extra args for run-task.sh
DISPTCH_ARGS=()
[[ -n "$DIFF_MODEL" ]] && DISPTCH_ARGS+=(--model "$DIFF_MODEL")
[[ -n "$DIFF_BUDGET" ]] && DISPTCH_ARGS+=(--budget "$DIFF_BUDGET")

if OUT=$(./scripts/run-task.sh tasks/issue-work.md --issue "$NUM" ${DISPTCH_ARGS[@]+"${DISPTCH_ARGS[@]}"} 2>&1); then RC=0; else RC=$?; fi

if [[ "$RC" -eq 0 ]]; then
  say "$NUM" "Run finished — see the agent's comment above for the PR. **To request changes:** leave your feedback as a comment here, then **remove the \`in-progress\` label**; that re-launches a tenant which reads your feedback and revises the same PR. (A comment alone does nothing — removing the label is the 'go again' signal.) Leave \`in-progress\` on and the issue rests until you merge or clear it."
  audit completed "rc=0"
else
  # Failed: release the claim so the issue isn't stranded + cooldown stamp.
  touch "$FSTAMP"
  [[ -n "$INPROG_ID" ]] && A -X DELETE "$GAPI/issues/$NUM/labels/$INPROG_ID" >/dev/null || true
  CLEAN=$(printf '%s' "$OUT" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | tr -d '\r' | grep -viE 'Processing|Reasoning|Ctrl\+C' | grep -v '^[[:space:]]*$' | tail -12)
  say "$NUM" "Dispatch FAILED (exit $RC) — released \`in-progress\` for retry (1h cooldown). Tail:

\`\`\`
$CLEAN
\`\`\`
Fix the cause, then re-assign or wait for the cooldown."
  audit failed "rc=$RC"
fi
