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
AGENT_LOGIN="${AGENT_GIT_USER:-agent-dev}"   # the tenant we dispatch for (matches task-dispatcher.sh)

A() { /usr/bin/curl -sk --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \
      -H "Authorization: token $AGENT_FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"; }
GAPI="https://git.${NODE_DOMAIN}/api/v1/repos/${COORDINATION_REPO}"
say() { A -X POST "$GAPI/issues/$1/comments" \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"body":sys.argv[1]}))' "$2")" >/dev/null; }
audit() {  # audit <action> <detail> — one JSON line to the shared audit log
  printf '{"ts":"%s","issue":%s,"action":"%s","run":"%s","detail":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$NUM" "$1" "${RUN:-}" "${2//\"/\'}" >> .task-dispatch/dispatch-audit.log
}
# front <file> <key> -> the value of <key>: in the frontmatter (before the first
# `---` closer), with trailing comments/whitespace stripped. Same one-liner
# task-dispatcher.sh and run-task.sh already use; defined here because this
# script is spawned detached and sources no library.
front() { awk -v k="$2" 'NR>1 && /^---$/{exit} $1==k":"{sub(/^[^:]*: */,""); sub(/[[:space:]]*#.*$/,""); sub(/[[:space:]]+$/,""); print}' "$1"; }
INPROG_ID=$(A "$GAPI/labels?limit=100" \
  | python3 -c 'import json,sys; ids=[l["id"] for l in json.load(sys.stdin) if l["name"]=="in-progress"]; print(ids[0] if ids else "")')

RUN="issue-$NUM"   # coarse tag for the audit line; run-task mints its own key alias
FSTAMP=".task-dispatch/issue-$NUM"

# Operator-gated labels (difficulty:*, trace) take two checks:
#   1 — the label is in the issue's CURRENT labels ($ISSUE_LABELS below): a
#       removed label is simply absent, no add/remove bookkeeping needed.
#   2 — label_added_by_operator: the most recent ADD of it in the timeline was
#       the operator's. Forgejo label events: add → body='1', remove →
#       body=''; there is no 'removed' boolean field.
# Sets LABEL_ACTOR to whoever made that add, for the log line.
label_added_by_operator() {
  LABEL_ACTOR=$(A "$GAPI/issues/$NUM/timeline?limit=100" | python3 -c '
import json, sys
target = sys.argv[1]
try:
    events = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for e in reversed(events):
    if e.get("type") == "label" and e.get("body") == "1" \
            and (e.get("label") or {}).get("name") == target:
        print((e.get("user") or {}).get("login", ""))
        break
' "$1")
  [[ -n "$LABEL_ACTOR" && "$LABEL_ACTOR" == "$OPERATOR_LOGIN" ]]
}

ISSUE_LABELS=$(A "$GAPI/issues/$NUM" | python3 -c '
import json, sys
try:
    print("\n".join(l.get("name", "") for l in json.load(sys.stdin).get("labels", [])))
except ValueError:
    pass
')

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

# The operator's difficulty:* label, if any (both gates: see label_added_by_operator).
DIFF_LABEL=$(printf '%s\n' "$ISSUE_LABELS" | grep -m1 '^difficulty:' || true)
if [[ -n "$DIFF_LABEL" ]] && ! label_added_by_operator "$DIFF_LABEL"; then
  echo "[dispatch-run] #$NUM: label '$DIFF_LABEL' present but not added by operator (actor='${LABEL_ACTOR:-none}'); ignoring"
  DIFF_LABEL=""
fi

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

      # Gate 3: check if model is live in LiteLLM before launch.
      # Distinguish "model definitively absent" (fall back) from
      # "couldn't reach LiteLLM" (abort — don't silently run the wrong model).
      LLM_CHECK=$(/usr/bin/curl -sk --resolve "llm.${NODE_DOMAIN}:443:127.0.0.1" \
        --max-time 10 \
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

      case "$LLM_CHECK" in
        live)
          # All good — proceed with the resolved model
          ;;
        not_found)
          echo "[dispatch-run] #$NUM: model '$DIFF_MODEL' not in LiteLLM model list; falling back to deepseek-flash"
          say "$NUM" "⚠️ Tier \`$TIER\` resolved to model \`$DIFF_MODEL\` but that model is not currently served by LiteLLM. Falling back to \`deepseek-flash\`. Check \`config/litellm.yaml\` if this persists."
          DIFF_MODEL="deepseek-flash"
          DIFF_BUDGET="0.50"
          DIFF_SOURCE="fallback"
          ;;
        *)
          echo "[dispatch-run] #$NUM: could not reach LiteLLM ($LLM_CHECK); aborting — won't silently run the wrong model"
          say "$NUM" "⚠️ Could not reach LiteLLM to verify model availability (\`$LLM_CHECK\`). Aborting this run — the model for tier \`$TIER\` (\`$DIFF_MODEL\`) may be live but cannot be confirmed. Check LiteLLM and retry by assigning the issue again."
          # Set cooldown stamp so the dispatcher doesn't re-dispatch during an outage
          touch "$FSTAMP"
          # Clean up the in-progress label so the issue isn't stranded
          [[ -n "$INPROG_ID" ]] && A -X DELETE "$GAPI/issues/$NUM/labels/$INPROG_ID" >/dev/null || true
          audit abort "LiteLlm unreachable: $LLM_CHECK"
          exit 1
          ;;
      esac
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

# --- jail summary on the kickoff comment ---------------------------------------
# Post the "Dispatched to..." comment with the resolved model + budget +
# source, the harness (from the brief), the image + a 12-char sha256 digest,
# and the skills library as shipped in this checkout — so the issue thread
# records WHICH jail actually landed, not just that one did. Computed AFTER
# difficulty resolution so the model shown is the one that will run. task-
# dispatcher.sh mirrors the same block on the task-request path. Fails soft:
# every field degrades to a placeholder, and a failure here never blocks the
# launch (this script runs `set -uo pipefail` without `-e`, and `say` failing
# just costs the comment, not the run).
HARNESS=$(front tasks/issue-work.md harness); HARNESS=${HARNESS:-forge}
IMAGE="${AGENT_IMAGE:-sovereign-node/agent:local}"
# 12-char image fingerprint: the same ID deploy.sh tags :local with, so an
# operator can grep build logs by it. "?" if the image isn't present locally
# (e.g. running dispatch-run.sh on a host without a built :local, like a drill).
IMG_ID=$(docker inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || true)
IMG_SHORT="${IMG_ID#sha256:}"; IMG_SHORT="${IMG_SHORT:0:12}"
[[ -z "$IMG_SHORT" ]] && IMG_SHORT="?"
# Skills library as shipped in node-config — counted + comma-list of the
# skill directory names (the dir under skills/, not the SKILL.md file). Uses
# python3, not `find -printf`, because the host dispatcher runs on macOS
# where BSD find has no -printf (the original PR #118 hit this).
SKILLS=$(python3 -c '
import glob, os
names = sorted(os.path.basename(os.path.dirname(p))
              for p in glob.glob("skills/*/SKILL.md"))
print(",".join(names) if names else "")' 2>/dev/null || true)
if [[ -n "$SKILLS" ]]; then
  SKILL_COUNT=$(awk -F',' '{print NF}' <<<"$SKILLS")
else
  SKILLS="(empty)"; SKILL_COUNT=0
fi
say "$NUM" "Dispatched to an ephemeral \`$AGENT_LOGIN\` tenant (operator-authorized). Claimed with \`in-progress\`.

**Jail summary**
- **Model:** \`${DIFF_MODEL:-<unresolved>}\` (budget \$${DIFF_BUDGET:-0}, resolved via \`${DIFF_SOURCE:-brief}\`)
- **Harness:** \`$HARNESS\`
- **Image:** \`$IMAGE\` (sha256: \`$IMG_SHORT\`)
- **Skills available:** $SKILLS ($SKILL_COUNT)

Deliverable is a node-config PR + a comment here; if I'm blocked I'll say so."
audit announced "model=${DIFF_MODEL:-?} harness=$HARNESS image=$IMAGE:$IMG_SHORT skills=$SKILL_COUNT"

# --- tracing: the `trace` label ------------------------------------------------
# Operator-applied `trace` -> run-task.sh --trace, then a comment linking the
# rendered timeline (end of this script). Same gate as difficulty: an agent
# adding `trace` to its own issue is ignored — tracing grants no privilege, but
# it keeps the container until teardown and writes to the host's traces/.
TRACE=""
if printf '%s\n' "$ISSUE_LABELS" | grep -qx 'trace'; then
  if label_added_by_operator trace; then
    TRACE=1
    echo "[dispatch-run] #$NUM: operator-applied label 'trace' -> tracing this run"
  else
    echo "[dispatch-run] #$NUM: label 'trace' present but not added by operator (actor='${LABEL_ACTOR:-none}'); ignoring"
  fi
fi

# Build the extra args for run-task.sh
DISPTCH_ARGS=()
[[ -n "$DIFF_MODEL" ]] && DISPTCH_ARGS+=(--model "$DIFF_MODEL")
[[ -n "$DIFF_BUDGET" ]] && DISPTCH_ARGS+=(--budget "$DIFF_BUDGET")
[[ -n "$TRACE" ]] && DISPTCH_ARGS+=(--trace)

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

# --- trace link -----------------------------------------------------------------
# run-task.sh --trace names its saved directory on a "trace saved to
# traces/<run>" line. Render it (--wait rides out LiteLLM's batched spend-log
# writes) and post the link as its own comment, so the completion comment above
# is never held back. The summary names tools and durations only, never command
# text: every tenant can read coordination, and a command line holds whatever
# the model typed. The page itself sits behind the traces.<domain> door.
if [[ -n "$TRACE" ]]; then
  TDIR=$(printf '%s\n' "$OUT" | sed -n 's#.*trace saved to \(traces/[A-Za-z0-9._-]*\).*#\1#p' | tail -1)
  if [[ -n "$TDIR" && -d "$TDIR" ]] && ./scripts/trace-render.py "$TDIR" --wait 300 >/dev/null 2>&1; then
    say "$NUM" "**Run trace:** https://traces.${NODE_DOMAIN}/${TDIR#traces/}/trace.html

$(cat "$TDIR/summary.md")"
    audit traced "${TDIR#traces/}"
  else
    say "$NUM" "Tracing was requested (\`trace\` label) but no trace was rendered for this run (${TDIR:-run-task.sh reported no trace directory}). Render by hand on the host: \`./scripts/trace-render.py traces/<run>\`."
    audit trace_failed "${TDIR:-no trace directory}"
  fi
fi
