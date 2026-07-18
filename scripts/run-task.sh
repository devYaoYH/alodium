#!/usr/bin/env bash
# Ephemeral tenancy (docs/DESIGN.md, "The agent runtime slot"): one container,
# one task, one key — the blast radius of a mayfly.
#
#   ./scripts/run-task.sh tasks/morning-digest.md
#
# What this does, in the order the contract demands:
#   1. mint a PER-RUN LiteLLM virtual key: task budget + expiry, model-allowlisted
#   2. run one jail container (same image as the resident agent) with the brief
#      as a non-interactive prompt; NO workspace volume — the filesystem dies
#      with the container. State leaves ONLY as artifacts the task files
#      (a coordination issue, a PR) — never as agent memory.
#   3. teardown: report spend, revoke the key (expiry is the backstop if this
#      script dies first), remove nothing because nothing persisted.
#
# Operator-run, ring 0 by nature (holds LITELLM_MASTER_KEY). Agents never run
# this; scheduled runs are cron on the host:
#   0 7 * * *  cd /path/to/node && ./scripts/run-task.sh tasks/morning-digest.md
#
# Brief format (tasks/README.md): markdown with a frontmatter block —
#   task, model, budget_usd, expires, env (names passed through from .env).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

BRIEF="${1:?usage: run-task.sh <tasks/brief.md> [--keep-key]}"
KEEP_KEY="${2:-}"    # --keep-key: drill mode — let expiry, not revocation, kill it
[[ -f "$BRIEF" ]] || { echo "no such brief: $BRIEF"; exit 1; }

front() { awk -v k="$2" 'NR>1 && /^---$/{exit} $1==k":"{sub(/^[^:]*: */,""); print}' "$1"; }
TASK=$(front "$BRIEF" task);         TASK=${TASK:-$(basename "$BRIEF" .md)}
MODEL=$(front "$BRIEF" model);       MODEL=${MODEL:-${AGENT_FAST_MODEL:-claude-haiku}}
BUDGET=$(front "$BRIEF" budget_usd); BUDGET=${BUDGET:-0.50}
EXPIRES=$(front "$BRIEF" expires);   EXPIRES=${EXPIRES:-2h}
RUN="task-$TASK-$(date +%Y%m%d-%H%M%S)"
PROMPT=$(awk 'NR>1 && /^---$/{f=1; next} f' "$BRIEF")

LLM=(/usr/bin/curl -sk --resolve "llm.${NODE_DOMAIN}:443:127.0.0.1" \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json")

echo "[$RUN] minting per-run key: model=$MODEL budget=\$$BUDGET expiry=$EXPIRES"
RUN_KEY=$("${LLM[@]}" "https://llm.${NODE_DOMAIN}/key/generate" \
  -d "{\"key_alias\":\"$RUN\",\"models\":[\"$MODEL\"],\"max_budget\":$BUDGET,\"duration\":\"$EXPIRES\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')

teardown() {
  SPEND=$("${LLM[@]}" "https://llm.${NODE_DOMAIN}/key/info?key=$RUN_KEY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("info",{}).get("spend",0))' 2>/dev/null || echo "?")
  if [[ "$KEEP_KEY" != "--keep-key" ]]; then
    "${LLM[@]}" -X POST "https://llm.${NODE_DOMAIN}/key/delete" -d "{\"keys\":[\"$RUN_KEY\"]}" >/dev/null || true
    echo "[$RUN] key revoked; spend was \$$SPEND (budget \$$BUDGET)"
  else
    echo "[$RUN] key KEPT (drill mode); spend \$$SPEND — expiry $EXPIRES is the kill switch"
  fi
}
trap teardown EXIT

# Env passthrough: ONLY names the brief declares, values from .env — the
# brief is the manifest; declare nothing, receive nothing.
ENV_ARGS=()
for name in $(front "$BRIEF" env | tr -d '[],'); do
  ENV_ARGS+=(-e "$name=${!name:-}")
done

# No agent_workspace mount: the resident tenant's memory is not this tenant's
# inheritance. --rm + no volume = the workspace provably dies at teardown.
docker run --rm --name "$RUN" \
  --network sovereign-node_agents \
  -e ANTHROPIC_BASE_URL=http://litellm:4000 \
  -e ANTHROPIC_AUTH_TOKEN="$RUN_KEY" \
  -e ANTHROPIC_MODEL="$MODEL" \
  -e ANTHROPIC_SMALL_FAST_MODEL="$MODEL" \
  -e AGENT_FORGEJO_TOKEN="${AGENT_FORGEJO_TOKEN:-}" \
  -e NODE_CONFIG_REPO="${NODE_CONFIG_REPO:-}" \
  -e COORDINATION_REPO="${COORDINATION_REPO:-}" \
  "${ENV_ARGS[@]}" \
  sovereign-node/agent:local \
  -p "$PROMPT" --output-format text \
  || echo "[$RUN] task exited nonzero — check whether it filed a 'blocked' issue"

echo "[$RUN] done. Deliverable (if any) is an issue in \$COORDINATION_REPO — the artifact, not this transcript."
