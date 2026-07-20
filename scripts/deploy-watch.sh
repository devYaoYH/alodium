#!/usr/bin/env bash
# deploy-watch.sh — closes the merge→deploy gap: one idempotent pass of "did a
# PR merge on Forgejo? then run the normal deploy." launchd (or cron) provides
# the loop; this script provides the judgement.
#
#   ./scripts/deploy-watch.sh            # one pass (launchd provides the loop)
#   ./scripts/deploy-watch.sh --dry-run  # report what it would do; change nothing
#
# The operator's merge REMAINS the authorization moment (docs/AGENT.md); this
# only removes the manual `./scripts/deploy.sh` keystroke that used to follow
# it. No new trust surface: same host user, same deploy.sh, and it POLLS
# Forgejo rather than accepting inbound triggers — no webhook listener, and no
# Actions runner on node-config (agents push workflow files there in PR
# branches; a runner would execute them).
#
# What keeps it safe to loop:
#   - Deploys ONLY from a clean checkout parked on main. On a branch or with
#     tracked edits = the operator is mid-work; skip silently, the next
#     heartbeat catches up after they finish.
#   - deploy.sh's fast-forward-only merge still applies; a divergence deploys
#     nothing and files a `blocked` coordination issue instead.
#   - A failed deploy files ONE `blocked` issue per remote HEAD (stamp-deduped,
#     like the dispatcher's cooldown) so a broken deploy lands in the
#     operator's notebook once — not every two minutes.
set -euo pipefail
cd "$(dirname "$0")/.."
DRY="${1:-}"
# shellcheck disable=SC1091
set -a; source .env; set +a
mkdir -p .task-dispatch

# Pass lock, same shape as task-dispatcher.sh: mkdir is atomic. The lock spans
# the whole pass INCLUDING a live deploy (image pulls can take minutes), so the
# stale-steal threshold is 2h, not the dispatcher's 30m.
LOCK=.task-dispatch/deploy-watch.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  if [[ -d "$LOCK" ]] && [[ $(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK") )) -gt 7200 ]]; then
    echo "[deploy-watch] stealing stale lock (>2h)"; rmdir "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || { echo "[deploy-watch] lock contended; exiting"; exit 0; }
  else
    exit 0   # another pass (or a live deploy) holds it — normal, stay quiet
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# This checkout doubles as the operator's working tree; yanking it forward
# mid-edit is how work gets lost. Parked elsewhere or dirty = not our turn.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" != "main" ]]; then
  echo "[deploy-watch] checkout parked on '$BRANCH' — skipping"; exit 0
fi
if ! git diff-index --quiet HEAD --; then
  echo "[deploy-watch] tracked edits in working tree — skipping"; exit 0
fi

git fetch -q forgejo main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse forgejo/main)
if [[ "$LOCAL" == "$REMOTE" ]]; then
  if [[ "$DRY" == "--dry-run" ]]; then
    echo "[deploy-watch] up to date at ${LOCAL:0:12} — nothing to do"
  fi
  exit 0   # the common every-2-minutes outcome; no log noise
fi

# One report per remote HEAD: a stamp means this exact tip already failed (or
# diverged) and the operator has an issue about it. Auto-retry resumes when
# main moves again; to retry sooner, fix the cause and either run
# ./scripts/deploy.sh by hand or rm the stamp.
FSTAMP=".task-dispatch/deploy-fail-$REMOTE"
if [[ -e "$FSTAMP" ]]; then
  echo "[deploy-watch] ${REMOTE:0:12} already failed and was reported — waiting for a fix or new commits"
  exit 0
fi

A() { /usr/bin/curl -sk --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \
      -H "Authorization: token $AGENT_FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"; }
GAPI="https://git.${NODE_DOMAIN}/api/v1/repos/${COORDINATION_REPO}"
report_blocked() {  # report_blocked <title> <body> — one `blocked` note in the operator's notebook
  LID=$(A "$GAPI/labels?limit=100" \
    | python3 -c 'import json,sys; ids=[l["id"] for l in json.load(sys.stdin) if l["name"]=="blocked"]; print(ids[0] if ids else "")')
  A -X POST "$GAPI/issues" -d "$(python3 -c 'import json,sys
print(json.dumps({"title":sys.argv[1],"body":sys.argv[2],"labels":[int(sys.argv[3])] if sys.argv[3] else []}))' \
    "$1" "$2" "$LID")" >/dev/null || true
}

if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
  echo "[deploy-watch] local main and forgejo/main have DIVERGED — refusing (operator decision, not a script's)"
  if [[ "$DRY" != "--dry-run" ]]; then
    touch "$FSTAMP"
    report_blocked "Auto-deploy blocked: main diverged at ${REMOTE:0:12}" \
"Local main (\`${LOCAL:0:12}\`) is not an ancestor of forgejo/main (\`${REMOTE:0:12}\`), so the fast-forward-only deploy refuses to run. Reconcile the checkout by hand, then run \`./scripts/deploy.sh\` — auto-deploy resumes on the next merge after that. (Stamp \`.task-dispatch/deploy-fail-${REMOTE:0:12}…\` suppresses repeat reports of this tip.)"
  fi
  exit 1
fi

if [[ "$DRY" == "--dry-run" ]]; then
  echo "[deploy-watch] would deploy ${LOCAL:0:12}..${REMOTE:0:12}:"
  git log --oneline "$LOCAL..$REMOTE" | sed 's/^/    /'
  exit 0
fi

echo "[deploy-watch] merge detected — deploying ${LOCAL:0:12}..${REMOTE:0:12}"
if OUT=$(./scripts/deploy.sh 2>&1); then
  printf '%s\n' "$OUT"
  echo "[deploy-watch] deployed ${REMOTE:0:12}"
  rm -f .task-dispatch/deploy-fail-*   # any older failure report is moot now
else
  RC=$?
  printf '%s\n' "$OUT"
  touch "$FSTAMP"
  TAIL=$(printf '%s\n' "$OUT" | tail -15)
  report_blocked "Auto-deploy FAILED at ${REMOTE:0:12} (exit $RC)" \
"\`scripts/deploy.sh\` failed after the merge of \`${REMOTE:0:12}\`. Tail:

\`\`\`
$TAIL
\`\`\`

Fix the cause, then run \`./scripts/deploy.sh\` by hand (or merge a fix — auto-deploy retries when main moves). Full log: \`.task-dispatch/deploy-watch.log\`."
  exit "$RC"
fi
