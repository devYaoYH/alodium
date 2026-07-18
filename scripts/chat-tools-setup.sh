#!/usr/bin/env bash
# Declarative chat-tool wiring: every app manifest may declare [expose.chat]
# tools = "<internal tool-server URL>" — this script makes Open WebUI agree.
# The manifest is the wire: the registered tool-server list in Open WebUI is
# rebuilt from the manifests on every run (add an app by adding a manifest
# line; remove it the same way). sso-setup.sh's sibling: idempotent, host-side,
# re-run whenever a manifest's [expose.chat] changes.
#
#   1. build + start the declared toolshims (apps/<name>/toolshim, service
#      <name>-toolshim by convention) whose base app is running
#   2. rebuild Open WebUI's tool_server.connections persistent config from
#      the manifests; restart it only if the wiring actually changed
#
# Shim upstream credentials are the shim's business: minted into
# secrets/<app>.env by an app-specific block a shim's PR adds here (see the
# retired memos example in git history for the pattern), never hand-placed.
set -euo pipefail
cd "$(dirname "$0")/.."

loadenv() {
  set -a; source .env
  for f in secrets/*.env; do [[ -e "$f" ]] && source "$f"; done
  set +a
}
loadenv

saveenv() {  # saveenv <key> <value> [file=.env]
  local f="${3:-.env}"
  grep -q "^$1=" "$f" && sed -i '' "s|^$1=.*|$1=$2|" "$f" || printf '%s=%s\n' "$1" "$2" >> "$f"
}


echo "== 0/2 radicale: ensure htpasswd auth (for chat write) =="
# A write-capable chat surface must NOT ride an unauthenticated backend.
# Create/update an htpasswd-backed Radicale config on first run, and mint a
# dedicated Basic-auth user for the shim. Idempotent: re-runs are no-ops.
if docker ps --format '{{.Names}}' | grep -qx radicale; then
  RAD_USER="${RADICALE_TOOL_USER:-operator}"
  if [[ -z "${RADICALE_TOOL_PASSWORD:-}" ]]; then
    RAD_PASS=$(openssl rand -base64 18)
    saveenv RADICALE_TOOL_PASSWORD "$RAD_PASS" secrets/radicale.env
    echo "   minted RADICALE_TOOL_PASSWORD -> secrets/radicale.env"
  else
    RAD_PASS="$RADICALE_TOOL_PASSWORD"
  fi
  saveenv RADICALE_TOOL_USER "$RAD_USER" secrets/radicale.env
  # Build or update /data/users, /data/rights inside the container (config is tracked
  # in-repo at apps/radicale/config and mounted at /config/config by compose)
  HTPASS=$(openssl passwd -apr1 "$RAD_PASS")
  docker exec radicale sh -c 'mkdir -p /data/collections && touch /data/users /data/rights'
  # Ensure user entry exists/updated (replace or append)
  docker exec radicale sh -lc "grep -v '^${RAD_USER}:' /data/users > /data/users.new || true; echo '${RAD_USER}:${HTPASS}' >> /data/users.new; mv /data/users.new /data/users"
  # Minimal rights: owner full access (login -> own collections)
  if ! docker exec radicale sh -c "grep -q '\[owner-write\]' /data/rights" >/dev/null 2>&1; then
    docker exec radicale sh -c 'cat > /data/rights <<EOF
[owner-write]
user = .+
collection = ^%(login)s(/.*)?$
permission = rw
EOF'
    echo "   wrote /data/rights"
  else
    echo "   rights file present — skip"
  fi
  docker restart radicale >/dev/null 2>&1 && echo "   radicale restarted to pick up auth"
else
  echo "   radicale not running — skip auth setup; re-run after enabling --profile apps"
fi


DECLARED=$(python3 - <<'EOF'
import glob, json, tomllib
conns = []
for path in sorted(glob.glob("manifest/*.toml")):
    with open(path, "rb") as f:
        m = tomllib.load(f)
    url = m.get("expose", {}).get("chat", {}).get("tools")
    if url:
        conns.append({"app": m.get("app", {}).get("name"), "url": url, "path": path})
print(json.dumps(conns))
EOF
)

echo "== 1/2 toolshim services =="
for app in $(echo "$DECLARED" | python3 -c 'import json,sys; print(" ".join(c["app"] for c in json.load(sys.stdin)))'); do
  if [[ -d "apps/$app/toolshim" ]] && docker ps --format '{{.Names}}' | grep -qx "$app"; then
    docker compose --profile apps --profile feeds --profile chat up -d --build "${app}-toolshim" >/dev/null 2>&1 \
      && echo "   ${app}-toolshim up" || echo "   ${app}-toolshim FAILED to start"
  else
    echo "   $app: shim dir or base app missing — skip"
  fi
done
[[ "$DECLARED" == "[]" ]] && echo "   no [expose.chat] declarations — chat gets no tools"

echo "== 2/2 register in Open WebUI (manifests -> tool_server.connections) =="
if ! docker ps --format '{{.Names}}' | grep -qx open-webui; then
  echo "   open-webui not running — skip; re-run after enabling --profile chat"
  exit 0
fi
CONNECTIONS=$(python3 - "$DECLARED" <<'EOF'
import json, sys
conns = []
for c in json.loads(sys.argv[1]):
    conns.append({
        "url": c["url"], "path": "openapi.json", "auth_type": "none", "key": "",
        "config": {"enable": True, "access_control": None},
        "info": {"name": c["app"],
                 "description": "declared by " + c["path"] + " [expose.chat]"},
    })
print(json.dumps(conns))
EOF
)
echo "   declared: $(echo "$CONNECTIONS" | python3 -c 'import json,sys; print([c["url"] for c in json.load(sys.stdin)])')"
CHANGED=$(docker exec -i open-webui python3 - "$CONNECTIONS" <<'EOF'
import json, sqlite3, sys, time
want = json.loads(sys.argv[1])
db = sqlite3.connect("/app/backend/data/webui.db")
row = db.execute("select value from config where key='tool_server.connections'").fetchone()
have = json.loads(row[0]) if row else []

# Open WebUI normalizes the value at startup (e.g. access_control ->
# access_grants), so compare only the fields the manifests own.
def owned(conns):
    return [(c.get("url"), c.get("path"), c.get("auth_type"),
             c.get("config", {}).get("enable"),
             c.get("info", {}).get("name")) for c in conns]

if owned(have) == owned(want):
    print("unchanged")
else:
    now = int(time.time())
    db.execute(
        "insert into config (key, value, updated_at) values ('tool_server.connections', ?, ?) "
        "on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at",
        (json.dumps(want), now))
    db.commit()
    print("updated")
EOF
)
if [[ "$CHANGED" == "updated" ]]; then
  docker restart open-webui >/dev/null   # persistent config loads at startup
  echo "   wiring updated — open-webui restarted"
else
  echo "   wiring unchanged — skip restart"
fi
echo
echo "Done. Chat's tool surface now matches the manifests."
