#!/usr/bin/env bash
# The bridge between "an agent wants a task run" and "no agent may touch
# docker": agents (the assistant, mostly) file a coordination issue titled
# `run: <brief>` with the `task-request` label; THIS host-side script — cron,
# not an agent — polls those requests, validates them against the tracked
# briefs in tasks/, and executes scripts/run-task.sh. The agent proposes,
# deterministic code executes, and the issue thread records the outcome.
#
#   ./scripts/task-dispatcher.sh            # one pass (cron provides the loop)
#   ./scripts/task-dispatcher.sh --dry-run  # validate + report, run nothing
#   */10 7-23 * * *  cd /path/to/node && ./scripts/task-dispatcher.sh >> /var/log/node-dispatch.log 2>&1
#
# What keeps this safe to automate:
#   - Only briefs that EXIST in tasks/ on the merged checkout can run — an
#     agent cannot author a new prompt into execution; new briefs arrive via
#     agent-dev PR + operator merge, like any capability change.
#   - Only briefs whose frontmatter says `dispatch: auto` are eligible; the
#     flag itself ships through review.
#   - The issue body is NEVER passed to the tenant — no parameter smuggling;
#     the brief is the whole prompt, budgets included.
#   - Per-brief cooldown (1h) bounds what a request-spamming (or injected)
#     agent can burn: each brief's own budget, at most hourly.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
DRY="${1:-}"

A() { /usr/bin/curl -sk --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \
      -H "Authorization: token $AGENT_FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"; }
GAPI="https://git.${NODE_DOMAIN}/api/v1/repos/${COORDINATION_REPO}"
front() { awk -v k="$2" 'NR>1 && /^---$/{exit} $1==k":"{sub(/^[^:]*: */,""); sub(/[[:space:]]*#.*$/,""); sub(/[[:space:]]+$/,""); print}' "$1"; }
say()   { A -X POST "$GAPI/issues/$1/comments" \
          -d "$(python3 -c 'import json,sys; print(json.dumps({"body":sys.argv[1]}))' "$2")" >/dev/null; }
close() { A -X PATCH "$GAPI/issues/$1" -d '{"state":"closed"}' >/dev/null; }

mkdir -p .task-dispatch    # per-brief cooldown stamps (gitignored host state)

OPERATOR_LOGIN="${OPERATOR_LOGIN:-${FORGEJO_ADMIN_USER:-operator}}"   # the ONLY actor whose assignment authorizes a launch (host env may override)
AGENT_LOGIN="${AGENT_GIT_USER:-agent-dev}"         # the assignee we dispatch for

A "$GAPI/issues?state=open&labels=task-request&type=issues" \
| python3 -c 'import json,sys; [print(i["number"], i["title"]) for i in json.load(sys.stdin)]' \
| while read -r NUM TITLE; do
  NAME=$(sed 's/^run:[[:space:]]*//i' <<<"$TITLE" | tr -cd 'a-z0-9_-')
  echo "[dispatch] #$NUM -> brief '$NAME'"

  if [[ -z "$NAME" || ! -f "tasks/$NAME.md" ]]; then
    say "$NUM" "Rejected: no tracked brief \`tasks/$NAME.md\`. New capabilities are a \`handoff\` to agent-dev (brief ships as a PR), not a request."
    close "$NUM"; continue
  fi
  if [[ "$(front "tasks/$NAME.md" dispatch)" != "auto" ]]; then
    say "$NUM" "Rejected: \`tasks/$NAME.md\` is not marked \`dispatch: auto\`. Flipping that flag is a reviewed PR — ask agent-dev via \`handoff\`."
    close "$NUM"; continue
  fi
  STAMP=".task-dispatch/$NAME"
  if [[ -f "$STAMP" ]] && [[ $(( $(date +%s) - $(stat -f %m "$STAMP" 2>/dev/null || stat -c %Y "$STAMP") )) -lt 3600 ]]; then
    echo "[dispatch] #$NUM deferred — '$NAME' ran within the hour"; continue
  fi

  if [[ "$DRY" == "--dry-run" ]]; then
    say "$NUM" "Dry-run: request is valid; \`$NAME\` would run now (budget \$$(front "tasks/$NAME.md" budget_usd), model $(front "tasks/$NAME.md" model)). Leaving open."
    continue
  fi

  touch "$STAMP"
  OUT=$(./scripts/run-task.sh "tasks/$NAME.md" 2>&1 | tail -15 || true)
  say "$NUM" "Ran \`$NAME\` as an ephemeral tenant. Tail of the run:

\`\`\`
$OUT
\`\`\`
The deliverable, if any, is its own issue (label \`digest\`/\`handoff\`)."
  close "$NUM"
done

# --- assigned-issue mode ----------------------------------------------------
# Operator assigns a coordination issue to agent-dev (in the web UI, with a
# passkey) => this pass launches an ephemeral tenant to work it. The trigger
# (an Actions "doorbell" workflow, or just this cron pass) is only a HINT:
# every fact below is re-derived from the Forgejo API and the merged checkout,
# so a forged nudge or a compromised runner cannot cause an unauthorized run.
#
# The gate, in order — each is independently sufficient to refuse:
#   1. agent-dev is actually an assignee (not just a stale mention).
#   2. NOT already claimed: no `in-progress` label (the cross-instance lock).
#   3. The assignment was performed BY THE OPERATOR — read from the issue
#      timeline's assign-event actor. Assistant/agent tokens hold write on
#      coordination and CAN self-assign (verified), so "assigned" alone is not
#      authorization; "assigned by the operator" is. This is the anti-
#      escalation core.
#   4. The issue number ONLY is handed to the tenant; the body never is. The
#      agent fetches the body itself, as data, under its own scoped token.

# in-progress label id (the durable claim). Must pre-exist — creating it at
# runtime is how coordination's other labels got triplicated. Look up, don't create.
INPROG_ID=$(A "$GAPI/labels" \
  | python3 -c 'import json,sys; ids=[l["id"] for l in json.load(sys.stdin) if l["name"]=="in-progress"]; print(ids[0] if ids else "")')

assign_by_operator() {   # $1=issue number → prints operator login iff operator made the latest agent-dev assignment
  A "$GAPI/issues/$1/timeline?limit=100" | python3 -c '
import json,sys
op,agent=sys.argv[1],sys.argv[2]
ev=[e for e in json.load(sys.stdin)
    if e.get("type")=="assignees" and not e.get("removed")
    and (e.get("assignee") or {}).get("login")==agent]
ev.sort(key=lambda e: e.get("created_at",""))
print((ev[-1].get("user") or {}).get("login","") if ev else "")' "$OPERATOR_LOGIN" "$AGENT_LOGIN"
}

if [[ -z "$INPROG_ID" ]]; then
  echo "[dispatch] assigned-issue mode DISABLED: no 'in-progress' label in $COORDINATION_REPO (operator must create it once)"
else
  A "$GAPI/issues?state=open&type=issues&limit=50" | python3 -c '
import json,sys
agent=sys.argv[1]
for i in json.load(sys.stdin):
    assignees={(a or {}).get("login") for a in (i.get("assignees") or [])}
    labels={(l or {}).get("name") for l in (i.get("labels") or [])}
    if agent in assignees and "in-progress" not in labels:
        print(i["number"])' "$AGENT_LOGIN" \
  | while read -r NUM; do
    echo "[assign] #$NUM assigned to $AGENT_LOGIN, unclaimed — checking authorization"

    ACTOR=$(assign_by_operator "$NUM")
    if [[ "$ACTOR" != "$OPERATOR_LOGIN" ]]; then
      echo "[assign] #$NUM REFUSED: latest assignment actor='${ACTOR:-none}' != operator='$OPERATOR_LOGIN'"
      [[ "$DRY" == "--dry-run" ]] || say "$NUM" "Not dispatched: this issue was assigned to \`$AGENT_LOGIN\` by \`${ACTOR:-someone other than the operator}\`, not the operator. Auto-dispatch only honors operator assignments — an agent cannot task another agent by self-assigning. If this is real work, the operator should (re)assign it."
      continue
    fi

    # Retry cooldown: a prior launch that FAILED released the claim and stamped
    # here. Don't relaunch within the hour — bounds hot-looping on a persistent
    # failure (bad PATH, key-mint down) while still allowing eventual retry.
    FSTAMP=".task-dispatch/issue-$NUM"
    if [[ -f "$FSTAMP" ]] && [[ $(( $(date +%s) - $(stat -f %m "$FSTAMP" 2>/dev/null || stat -c %Y "$FSTAMP") )) -lt 3600 ]]; then
      echo "[assign] #$NUM deferred — a launch failed within the hour; cooling down"; continue
    fi

    if [[ "$DRY" == "--dry-run" ]]; then
      echo "[assign] #$NUM WOULD LAUNCH issue-work (operator-authorized, unclaimed)"
      continue
    fi

    # Claim FIRST (add in-progress label), then launch — the label is the
    # visible lock so a second pass/instance skips it at the filter above.
    A -X POST "$GAPI/issues/$NUM/labels" -d "{\"labels\":[$INPROG_ID]}" >/dev/null
    echo "[assign] #$NUM claimed (in-progress); launching issue-work"
    say "$NUM" "Dispatched to an ephemeral \`$AGENT_LOGIN\` tenant (operator-authorized). Claimed with \`in-progress\`. Deliverable will be a node-config PR + a comment here; if I'm blocked I'll say so."

    if OUT=$(./scripts/run-task.sh tasks/issue-work.md --issue "$NUM" 2>&1); then
      RC=0; else RC=$?; fi
    if [[ "$RC" -eq 0 ]]; then
      # The agent posts its own comment with the PR link + what it did, so
      # don't echo the (ANSI-noisy) run tail — just the operator contract.
      say "$NUM" "Run finished — see the agent's comment above for the PR. **To request changes:** leave your feedback as a comment here, then **remove the \`in-progress\` label**; that re-launches a tenant which reads your feedback and revises the same PR. (A comment alone does nothing — removing the label is the 'go again' signal.) Leave \`in-progress\` on and the issue rests until you merge or clear it."
    else
      # Launch/agent FAILED — release the claim so the issue isn't stranded,
      # stamp the cooldown, and show a CLEANED tail (failure = you need it).
      touch "$FSTAMP"
      A -X DELETE "$GAPI/issues/$NUM/labels/$INPROG_ID" >/dev/null || true
      CLEAN=$(printf '%s' "$OUT" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | tr -d '\r' | grep -viE 'Processing|Reasoning|Ctrl\+C' | grep -v '^[[:space:]]*$' | tail -12)
      say "$NUM" "Dispatch FAILED (exit $RC) — released \`in-progress\` for retry (1h cooldown). Tail:

\`\`\`
$CLEAN
\`\`\`
Fix the cause, then re-assign or wait for the cooldown."
    fi
  done
fi

# Consume doorbell markers: they are wake signals only (this pass already
# re-derived everything authoritatively), so clearing them just stops relaunch
# churn. Optional — only if the operator mapped the spool to a host path.
if [[ -n "${DISPATCH_SPOOL:-}" && -d "$DISPATCH_SPOOL" ]]; then
  rm -f "$DISPATCH_SPOOL"/*.nudge 2>/dev/null || true
fi

echo "[dispatch] pass complete: $(date '+%Y-%m-%dT%H:%M:%S%z')"
