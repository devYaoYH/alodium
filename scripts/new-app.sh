#!/usr/bin/env bash
# Seed a new app repo in Forgejo from the skeleton.
#
# "Up a service of kind X" starts here: one operator command creates
# apps/<name> seeded from templates/app-skeleton (contracts already
# satisfied: manifest, OpenAPI stub, MCP tool stub, healthz, smoke tests),
# grants agent-dev write, and the dev-agent takes it from a clone.
# Repo creation stays an operator moment by design — the agent's token
# cannot create repos, only fill them.
#
# Usage:
#   ./scripts/new-app.sh <name>
#
# Env (from ../.env or exported):
#   FORGEJO_TOKEN  — operator API token (bootstrap-forgejo.sh minted it)
#   FORGEJO_URL    — default https://git.$NODE_DOMAIN (falls back to
#                    https://git.localhost with TLS verify off for local dev)
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

NAME="${1:?usage: new-app.sh <name>}"
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "name must be lowercase alnum + dashes"; exit 1; }

ORG="apps"
ADMIN="${FORGEJO_ADMIN_USER:-operator}"
EDGE_NET="sovereign-node_edge"
FORGEJO_URL="${FORGEJO_URL:-https://git.${NODE_DOMAIN:-localhost}}"
: "${FORGEJO_TOKEN:?set FORGEJO_TOKEN in .env (bootstrap-forgejo.sh mints it)}"

CURL=(curl -sS -H "Authorization: token $FORGEJO_TOKEN" -H "Content-Type: application/json")
# local dev: git.localhost carries Caddy's internal CA
[[ "$FORGEJO_URL" == *localhost* ]] && CURL+=(-k --resolve "git.localhost:443:127.0.0.1")

# ensure the "apps" org exists (idempotent, mirrors.sh pattern)
"${CURL[@]}" -o /dev/null "$FORGEJO_URL/api/v1/orgs" \
  -d "{\"username\":\"$ORG\",\"description\":\"Apps built on this node, seeded from templates/app-skeleton\"}" || true

if "${CURL[@]}" "$FORGEJO_URL/api/v1/repos/$ORG/$NAME" | grep -q '"full_name"'; then
  echo "apps/$NAME already exists — refusing to overwrite. Clone it instead."
  exit 1
fi

echo "== creating $ORG/$NAME =="
"${CURL[@]}" -o /dev/null "$FORGEJO_URL/api/v1/orgs/$ORG/repos" \
  -d "{\"name\":\"$NAME\",\"private\":true,\"description\":\"Seeded from app-skeleton\"}"

# agent-dev develops it (its PR path); write, never admin
"${CURL[@]}" -o /dev/null -X PUT "$FORGEJO_URL/api/v1/repos/$ORG/$NAME/collaborators/agent-dev" \
  -d '{"permission":"write"}' || true

echo "== seeding from templates/app-skeleton =="
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cp -R templates/app-skeleton/. "$TMP/"
# stamp the app name into every stub (portable BSD/GNU sed)
grep -rl __APP_NAME__ "$TMP" | while read -r f; do
  sed -i.bak "s/__APP_NAME__/$NAME/g" "$f" && rm -f "$f.bak"
done

git -C "$TMP" init -q -b main
git -C "$TMP" -c user.name="$ADMIN" -c user.email="$ADMIN@node.invalid" \
  add -A
git -C "$TMP" -c user.name="$ADMIN" -c user.email="$ADMIN@node.invalid" \
  commit -qm "Seed $NAME from app-skeleton"
docker run --rm -v "$TMP:/src" -w /src --network "$EDGE_NET" \
  alpine/git -c safe.directory=/src push -q \
  "http://$ADMIN:$FORGEJO_TOKEN@forgejo:3000/$ORG/$NAME.git" main

echo
echo "Done: $FORGEJO_URL/$ORG/$NAME (agent-dev: write)"
echo "Next, in an agent session:  clone http://forgejo:3000/$ORG/$NAME,"
echo "fill the contracts per its README, PR the app + its node-config registration."
echo "Mint whatever app.toml [needs] declares when that PR lands — not before."
