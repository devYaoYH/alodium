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

# Pass lock: at most one dispatcher PASS at a time so claim-checks never race
# (launchd already coalesces its triggers; this also guards a manual run
# overlapping a launchd one). mkdir is atomic. Detached issue runs do NOT hold
# this — it's released when the pass exits, seconds later. Steal a stale lock
# (a crashed pass) after 30 min. Per-issue exclusivity does not depend on this
# lock — the in-progress label is the durable claim — this just prevents two
# passes from both claiming the same fresh issue in the same instant.
LOCK=.task-dispatch/pass.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  if [[ -d "$LOCK" ]] && [[ $(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK") )) -gt 1800 ]]; then
    echo "[dispatch] stealing stale pass lock (>30m)"; rmdir "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || { echo "[dispatch] lock contended; exiting"; exit 0; }
  else
    echo "[dispatch] another pass holds the lock; exiting"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

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

# Concurrency: agents run in PARALLEL — one detached dispatch-run.sh per issue.
# The only hard invariant is per-ISSUE exclusivity (never two containers on the
# same issue), enforced by claim-before-spawn + the pass lock below. MAX_CONC is
# a HOST-RESOURCE backstop (memory/CPU), NOT the spend control — spend is bounded
# by LiteLLM (per-run key budgets, and a global budget if one is set). Raise it
# for bigger swarms; 0 = unlimited.
MAX_CONC="${DISPATCH_MAX_CONCURRENCY:-4}"
running_tenants() { docker ps --filter "name=task-issue-work" -q 2>/dev/null | grep -c . || true; }
adaudit() {  # adaudit <issue> <action> <detail>
  printf '{"ts":"%s","issue":%s,"action":"%s","run":"","detail":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3//\"/\'}" >> .task-dispatch/dispatch-audit.log
}

if [[ -z "$INPROG_ID" ]]; then
  echo "[dispatch] assigned-issue mode DISABLED: no 'in-progress' label in $COORDINATION_REPO (operator must create it once)"
else
  # Collect eligible issues first (avoid a pipe-subshell so counters/claims are
  # in THIS shell — the claim must be visible before the next iteration).
  ELIGIBLE=$(A "$GAPI/issues?state=open&type=issues&limit=50" | python3 -c '
import json,sys
agent=sys.argv[1]
for i in json.load(sys.stdin):
    assignees={(a or {}).get("login") for a in (i.get("assignees") or [])}
    labels={(l or {}).get("name") for l in (i.get("labels") or [])}
    if agent in assignees and "in-progress" not in labels:
        print(i["number"])' "$AGENT_LOGIN")

  for NUM in $ELIGIBLE; do
    echo "[assign] #$NUM assigned to $AGENT_LOGIN, unclaimed — checking authorization"

    ACTOR=$(assign_by_operator "$NUM")
    if [[ "$ACTOR" != "$OPERATOR_LOGIN" ]]; then
      echo "[assign] #$NUM REFUSED: latest assignment actor='${ACTOR:-none}' != operator='$OPERATOR_LOGIN'"
      adaudit "$NUM" refused "actor=${ACTOR:-none}"
      [[ "$DRY" == "--dry-run" ]] || say "$NUM" "Not dispatched: this issue was assigned to \`$AGENT_LOGIN\` by \`${ACTOR:-someone other than the operator}\`, not the operator. Auto-dispatch only honors operator assignments — an agent cannot task another agent by self-assigning. If this is real work, the operator should (re)assign it."
      continue
    fi

    # Retry cooldown: a prior launch that FAILED released the claim and stamped
    # here. Don't relaunch within the hour (bounds hot-looping on a persistent
    # failure) while still allowing eventual retry.
    FSTAMP=".task-dispatch/issue-$NUM"
    if [[ -f "$FSTAMP" ]] && [[ $(( $(date +%s) - $(stat -f %m "$FSTAMP" 2>/dev/null || stat -c %Y "$FSTAMP") )) -lt 3600 ]]; then
      echo "[assign] #$NUM deferred — a launch failed within the hour; cooling down"
      adaudit "$NUM" deferred "cooldown"; continue
    fi

    # Host-resource backstop (not spend): if at the concurrency ceiling, leave
    # the issue UNCLAIMED so the next pass dispatches it when a slot frees.
    if [[ "$MAX_CONC" != "0" ]] && [[ "$(running_tenants)" -ge "$MAX_CONC" ]]; then
      echo "[assign] #$NUM deferred — $MAX_CONC tenants already running (host cap); next pass"
      adaudit "$NUM" deferred "at-host-cap=$MAX_CONC"; continue
    fi

    if [[ "$DRY" == "--dry-run" ]]; then
      echo "[assign] #$NUM WOULD LAUNCH issue-work (operator-authorized, unclaimed)"; continue
    fi

    # Claim FIRST (add in-progress) — the durable per-issue lock. Then spawn the
    # run DETACHED — its own session, so it survives this pass exiting (launchd
    # kills the job's process group on exit; the plist sets no
    # AbandonProcessGroup) and runs alongside other issues' tenants. macOS has
    # no setsid binary (like no flock — same portability trap), so the detach
    # is python's start_new_session, a real setsid() on both platforms.
    # Per-issue exclusivity holds: the claim is visible to the next pass's
    # scan, and the pass lock serializes claiming. If the SPAWNER ITSELF fails
    # (the run never starts, so dispatch-run's own failure path can't fire),
    # release the claim right here — a claim with no process behind it
    # strands the issue as forever "in-progress".
    A -X POST "$GAPI/issues/$NUM/labels" -d "{\"labels\":[$INPROG_ID]}" >/dev/null
    echo "[assign] #$NUM claimed (in-progress); spawning detached issue-work"
    if python3 -c '
import subprocess, sys
with open(".task-dispatch/dispatch-run.log", "ab") as log:
    subprocess.Popen(["./scripts/dispatch-run.sh", sys.argv[1]],
                     stdout=log, stderr=log, start_new_session=True)
' "$NUM"; then
      say "$NUM" "Dispatched to an ephemeral \`$AGENT_LOGIN\` tenant (operator-authorized). Claimed with \`in-progress\`. Deliverable is a node-config PR + a comment here; if I'm blocked I'll say so."
      adaudit "$NUM" dispatched "actor=$ACTOR"
    else
      A -X DELETE "$GAPI/issues/$NUM/labels/$INPROG_ID" >/dev/null || true
      say "$NUM" "Dispatch FAILED before the tenant started (host-side spawn error — see .task-dispatch/dispatch-run.log). Claim released; re-assign or remove/re-add a label to retry once the host is fixed."
      adaudit "$NUM" spawn-failed "detach spawn returned nonzero"
      continue
    fi
    sleep 3   # let the container register before the next cap check
  done
fi

# Consume doorbell markers: they are wake signals only (this pass already
# re-derived everything authoritatively), so clearing them just stops relaunch
# churn. Optional — only if the operator mapped the spool to a host path.
if [[ -n "${DISPATCH_SPOOL:-}" && -d "$DISPATCH_SPOOL" ]]; then
  rm -f "$DISPATCH_SPOOL"/*.nudge 2>/dev/null || true
fi

echo "[dispatch] pass complete: $(date '+%Y-%m-%dT%H:%M:%S%z')"
