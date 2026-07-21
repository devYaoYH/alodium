#!/usr/bin/env bash
# One-shot, idempotent Forgejo bootstrap. Turns a fresh forgejo container into
# the node's config-as-code hub with zero web-installer clicks:
#
#   1. operator admin account            (password -> .env, break-glass only)
#   2. agent-dev user + scoped token     (-> AGENT_FORGEJO_TOKEN in .env)
#   3. operator API token                (-> FORGEJO_TOKEN in .env, for mirror.sh)
#   4. SUT watcher token                (-> SUT_FORGEJO_TOKEN in .env)
#   5. node-config repo, current git history pushed
#   6. coordination repo — the agents' shared notebook (issues + board)
#   7. assistant user + weaker token   (-> ASSISTANT_FORGEJO_TOKEN in .env)
#
# Re-running skips anything that already exists. Humans run this once;
# everything after flows through git and the agents.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

ADMIN="${FORGEJO_ADMIN_USER:-operator}"
EDGE_NET="sovereign-node_edge"
FJ() { docker exec -u 1000 forgejo forgejo "$@"; }
API() { /usr/bin/curl -sk --resolve "git.localhost:443:127.0.0.1" \
        -H "Authorization: token $FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"; }
saveenv() {  # saveenv KEY VALUE — insert or replace in .env
  grep -q "^$1=" .env && sed -i '' "s|^$1=.*|$1=$2|" .env || printf '%s=%s\n' "$1" "$2" >> .env
}

echo "== 1/6 operator admin =="
if FJ admin user list --admin | grep -qw "$ADMIN"; then
  echo "   admin '$ADMIN' exists — skip"
else
  PASS=$(openssl rand -base64 18)
  FJ admin user create --admin --username "$ADMIN" --password "$PASS" \
     --email "${ACME_EMAIL:-operator@$NODE_DOMAIN}" --must-change-password=false
  saveenv FORGEJO_ADMIN_USER "$ADMIN"
  saveenv FORGEJO_ADMIN_PASSWORD "$PASS"
  echo "   created '$ADMIN' (password in .env — break-glass; day-to-day auth goes OIDC later)"
fi

echo "== 2/6 operator API token =="
if [[ -z "${FORGEJO_TOKEN:-}" ]]; then
  FORGEJO_TOKEN=$(FJ admin user generate-access-token --username "$ADMIN" \
      --token-name node-ops --scopes write:repository,write:organization,write:user --raw)
  saveenv FORGEJO_TOKEN "$FORGEJO_TOKEN"
  echo "   minted node-ops token -> .env FORGEJO_TOKEN"
else
  echo "   FORGEJO_TOKEN already set — skip"
fi

echo "== 3/7 agent-dev user + scoped token =="
# Scopes: write:repository (clone/branch/push/PR) + write:issue (the M3
# coordination surface — agents track work and leave notes as issues/boards).
# Widening this line IS the jail-widening moment: it ships as a reviewed diff.
# Pre-M3 installs: delete the old "jail" token in Forgejo (user agent-dev ->
# settings -> applications), unset AGENT_FORGEJO_TOKEN in .env, re-run.
if FJ admin user list | grep -qw agent-dev; then
  echo "   agent-dev exists — skip"
else
  FJ admin user create --username agent-dev --password "$(openssl rand -base64 18)" \
     --email "agent-dev@node.invalid" --must-change-password=false
fi
if [[ -z "${AGENT_FORGEJO_TOKEN:-}" ]]; then
  TOKEN=$(FJ admin user generate-access-token --username agent-dev \
      --token-name jail --scopes write:repository,write:issue --raw)
  saveenv AGENT_FORGEJO_TOKEN "$TOKEN"
  saveenv NODE_CONFIG_REPO "$ADMIN/node-config"
  echo "   minted agent-dev token -> .env AGENT_FORGEJO_TOKEN (write:repository,write:issue)"
else
  echo "   AGENT_FORGEJO_TOKEN already set — skip"
fi

echo "== 4/7 isolated SUT watcher token =="
# The host SUT controller clones PR heads and source snapshots, then reads and
# comments on PRs. It deliberately cannot write code, administer Forgejo, or
# access secrets inside the test VM.
if [[ -z "${SUT_FORGEJO_TOKEN:-}" ]]; then
  SUT_FORGEJO_TOKEN=$(FJ admin user generate-access-token --username "$ADMIN" \
      --token-name sut-watch --scopes read:repository,read:issue,write:issue --raw)
  saveenv SUT_FORGEJO_TOKEN "$SUT_FORGEJO_TOKEN"
  echo "   minted sut-watch token -> .env SUT_FORGEJO_TOKEN (read:repository,read:issue,write:issue)"
else
  echo "   SUT_FORGEJO_TOKEN already set — skip"
fi

echo "== 5/7 node-config repo =="
if API "https://git.localhost/api/v1/repos/$ADMIN/node-config" | grep -q '"full_name"'; then
  echo "   repo exists — skip create"
else
  API -X POST "https://git.localhost/api/v1/user/repos" \
      -d '{"name":"node-config","private":true,"description":"This node, as code."}' >/dev/null
  echo "   created $ADMIN/node-config (private)"
fi
# agent-dev collaborates with write access (its PR path)
API -X PUT "https://git.localhost/api/v1/repos/$ADMIN/node-config/collaborators/agent-dev" \
    -d '{"permission":"write"}' >/dev/null || true

echo "   pushing current history..."
docker run --rm -v "$PWD:/src" -w /src --network "$EDGE_NET" \
  alpine/git -c safe.directory=/src push --all \
  "http://$ADMIN:$FORGEJO_TOKEN@forgejo:3000/$ADMIN/node-config.git" 2>&1 | tail -1

echo "== 6/7 coordination repo (the agents' shared notebook) =="
# Issues + the project board here are how tenants track work and hand off:
# resident sessions file what they left unfinished, ephemeral tenants record
# their artifact (or their failure) before teardown, the operator reads one
# board. Protocol: agent/AGENTS.md; skill: skills/coordination. Memory belongs
# to git, not to a process — this repo is where that memory lives.
if API "https://git.localhost/api/v1/repos/$ADMIN/coordination" | grep -q '"full_name"'; then
  echo "   repo exists — skip create"
else
  API -X POST "https://git.localhost/api/v1/user/repos" \
      -d '{"name":"coordination","private":true,"description":"Agent notebook: issues are notes, the board is state."}' >/dev/null
  echo "   created $ADMIN/coordination (private)"
fi
API -X PUT "https://git.localhost/api/v1/repos/$ADMIN/coordination/collaborators/agent-dev" \
    -d '{"permission":"write"}' >/dev/null || true
# Standing labels: the note taxonomy agents file under. TRUE idempotency —
# Forgejo does NOT reject a duplicate label name (no 409), so a blind re-POST
# triplicates them (it did). Create only names that don't already exist.
# Seeded with the AGENT token: labels need write:issue, which the operator's
# node-ops token (repo/org/user scopes) deliberately lacks. $TOKEN when step 3
# just minted it, .env's AGENT_FORGEJO_TOKEN on re-runs.
AAPI() { /usr/bin/curl -sk --resolve "git.localhost:443:127.0.0.1" \
        -H "Authorization: token ${TOKEN:-$AGENT_FORGEJO_TOKEN}" -H "Content-Type: application/json" "$@"; }
EXISTING=$(AAPI "https://git.localhost/api/v1/repos/$ADMIN/coordination/labels?limit=100" \
           | python3 -c 'import json,sys; print("\n".join(l["name"] for l in json.load(sys.stdin)))' 2>/dev/null || true)
for LABEL in '{"name":"handoff","color":"#1f6feb","description":"for the next tenant: state + next step"}' \
             '{"name":"blocked","color":"#d73a4a","description":"needs the operator: scope, secret, or merge"}' \
             '{"name":"digest","color":"#0e8a16","description":"ambient task output (morning digest etc.)"}' \
             '{"name":"observation","color":"#a2eeef","description":"something noticed, no action required yet"}' \
             '{"name":"mirror-request","color":"#5319e7","description":"proposed upstream repo mirror; host runner requires operator digest approval"}' \
             '{"name":"task-request","color":"#fbca04","description":"run a tracked brief: title run: <name>; task-dispatcher.sh executes"}' \
             '{"name":"in-progress","color":"#fbca04","description":"claimed by an ephemeral tenant — dispatcher lock; do not reassign"}'; do
  NAME=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$LABEL")
  if grep -qx "$NAME" <<<"$EXISTING"; then continue; fi
  AAPI -X POST "https://git.localhost/api/v1/repos/$ADMIN/coordination/labels" -d "$LABEL" >/dev/null || true
done
saveenv COORDINATION_REPO "$ADMIN/coordination"
echo "   labels seeded; COORDINATION_REPO -> .env"

echo "== 7/7 assistant user + weaker token =="
# The front-door assistant (agent/ASSISTANT.md): converses, reads, files
# issues. Deliberately weaker than agent-dev — read:repository (docs, skills,
# clones) + write:issue (the notebook). No code-write scope: it has no PR
# path, so "do X to the node" always flows through a handoff to agent-dev.
if FJ admin user list | grep -qw assistant; then
  echo "   assistant exists — skip"
else
  FJ admin user create --username assistant --password "$(openssl rand -base64 18)" \
     --email "assistant@node.invalid" --must-change-password=false
fi
if [[ -z "${ASSISTANT_FORGEJO_TOKEN:-}" ]]; then
  ATOKEN=$(FJ admin user generate-access-token --username assistant \
      --token-name front-door --scopes read:repository,write:issue --raw)
  saveenv ASSISTANT_FORGEJO_TOKEN "$ATOKEN"
  echo "   minted assistant token -> .env ASSISTANT_FORGEJO_TOKEN (read:repository,write:issue)"
else
  echo "   ASSISTANT_FORGEJO_TOKEN already set — skip"
fi
# read on node-config (its window into how the node works), write on the
# notebook (its only write surface — repo-level write; the token still
# caps it to issues).
API -X PUT "https://git.localhost/api/v1/repos/$ADMIN/node-config/collaborators/assistant" \
    -d '{"permission":"read"}' >/dev/null || true
API -X PUT "https://git.localhost/api/v1/repos/$ADMIN/coordination/collaborators/assistant" \
    -d '{"permission":"write"}' >/dev/null || true

echo
echo "Done. Forgejo is config-as-code: no installer ran, state is one volume,"
echo "that volume is in scripts/backup.sh's restic include list."
