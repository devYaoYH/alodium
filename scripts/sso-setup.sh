#!/usr/bin/env bash
# SSO at the door for EVERY human surface: git (Forgejo), llm/ui (LiteLLM),
# feeds (Miniflux), chat (Open WebUI), notes (Memos) — all native OIDC — and
# cal's web UI (Radicale, no native OIDC) via the oauth2-proxy authshim.
# One passkey via Pocket ID opens all of them. Idempotent; run once per node,
# re-run any time a new surface lands.
#
#   1. mint the OIDC clients in Pocket ID -> .env (+ shim cookie secret)
#   2. record the operator's Pocket ID user id (LiteLLM UI admin) -> .env
#   3. seed the Memos identity provider via its API (needs MEMOS_ADMIN_TOKEN;
#      otherwise prints the values for a one-time paste into Settings -> SSO)
#   4. [NODE_DOMAIN=localhost only] dev glue: compose override giving
#      containers DNS for *.localhost + trust for Caddy's local CA
#   5. register the auth source in Forgejo; recreate the SSO'd services that
#      are enabled on this node; start the authshim (it is part of the door)
#
# Break-glass stays: local admin password logins (Forgejo, Miniflux, Open
# WebUI's login form) remain enabled. DAV/API credential planes are untouched
# — only humans log in via OIDC.
set -euo pipefail
cd "$(dirname "$0")/.."

# Credentials are split by blast radius: .env holds node-plane keys (ring 0,
# tenants); each app's live in host-only secrets/<app>.env (see docs/SSO.md).
loadenv() {
  set -a; source .env
  for f in secrets/*.env; do [[ -e "$f" ]] && source "$f"; done
  set +a
}
loadenv

AUTH_URL="https://auth.${NODE_DOMAIN}"
: "${POCKET_ID_API_KEY:?set POCKET_ID_API_KEY in .env (mint one in Pocket ID: Settings -> Admin -> API Keys)}"
CURL=(/usr/bin/curl -sk -H "X-API-KEY: $POCKET_ID_API_KEY" -H "Content-Type: application/json")
[[ "$NODE_DOMAIN" == "localhost" ]] && CURL+=(--resolve "auth.localhost:443:127.0.0.1")

# pid_api <METHOD> <path> [curl args...] — one Pocket ID admin API call; prints
# the response body. A non-2xx answer fails with Pocket ID's own error and a
# hint, instead of a later `KeyError: 'data'` from parsing an error body as
# data — which is all an expired POCKET_ID_API_KEY used to look like.
pid_api() {
  local method=$1 path=$2 body code err
  shift 2
  body=$(mktemp)
  code=$("${CURL[@]}" -X "$method" -o "$body" -w '%{http_code}' "$@" "$AUTH_URL$path") || code=000
  if [[ "$code" != 2* ]]; then
    err=$(python3 -c '
import json, sys
raw = open(sys.argv[1], errors="replace").read()
try:
    d = json.loads(raw)
    print(d.get("error") or d.get("message") or "" if isinstance(d, dict) else "")
except ValueError:
    print(" ".join(raw.split())[:200])' "$body")
    rm -f "$body"
    echo "sso-setup: Pocket ID $method $path -> HTTP $code${err:+: $err}" >&2
    case "$code" in
      401|403) echo "sso-setup: Pocket ID rejected POCKET_ID_API_KEY — it is expired or revoked. Mint a new key in Pocket ID ($AUTH_URL -> Settings -> Admin -> API Keys), put it in .env as POCKET_ID_API_KEY, and re-run." >&2 ;;
      000)     echo "sso-setup: could not reach $AUTH_URL — are pocket-id and caddy running?" >&2 ;;
    esac
    return 1
  fi
  cat "$body"
  rm -f "$body"
}

# Preflight: fail fast, with the reason, before touching anything.
pid_api GET /api/oidc/clients >/dev/null
saveenv() {  # saveenv <key> <value> [file=.env]
  local f="${3:-.env}"
  grep -q "^$1=" "$f" && sed -i '' "s|^$1=.*|$1=$2|" "$f" || printf '%s=%s\n' "$1" "$2" >> "$f"
}

mint_client() {  # mint_client <name> <comma-separated callbacks> <ENV_PREFIX> [envfile]
  local name=$1 callbacks=$2 prefix=$3 envfile="${4:-.env}" id secret client payload changed clients created
  clients=$(pid_api GET /api/oidc/clients)
  id=$(printf '%s' "$clients" | python3 -c '
import json, sys
for client in json.load(sys.stdin)["data"]:
    if client["name"] == sys.argv[1]:
        print(client["id"])
        break
' "$name")
  if [[ -n "$id" ]]; then
    # Pocket ID's PUT endpoint expects the full supported DTO. Preserve it,
    # adding a callback only when a new browser surface needs one.
    client=$(pid_api GET "/api/oidc/clients/$id")
    changed=$(printf '%s' "$client" | python3 -c '
import json, sys
current = set(json.load(sys.stdin).get("callbackURLs", []))
needed = {url for url in sys.argv[1].split(",") if url}
print("yes" if not needed.issubset(current) else "no")
' "$callbacks")
    if [[ "$changed" == yes ]]; then
      payload=$(printf '%s' "$client" | python3 -c '
import json, sys
client = json.load(sys.stdin)
fields = ("name", "description", "logoutCallbackURLs", "isPublic", "pkceEnabled",
          "requiresReauthentication", "requiresPushedAuthorizationRequests",
          "launchURL", "hasLogo", "hasDarkLogo", "logoUrl", "darkLogoUrl",
          "isGroupRestricted")
payload = {field: client.get(field) for field in fields}
payload["callbackURLs"] = list(dict.fromkeys(client.get("callbackURLs", []) + [u for u in sys.argv[1].split(",") if u]))
print(json.dumps(payload))
' "$callbacks")
      pid_api PUT "/api/oidc/clients/$id" -d "$payload" >/dev/null
      echo "   client '$name' callbacks updated"
    else
      echo "   client '$name' exists — callbacks current"
    fi
  else
    payload=$(python3 -c '
import json, sys
print(json.dumps({"name": sys.argv[1], "callbackURLs": [u for u in sys.argv[2].split(",") if u]}))
' "$name" "$callbacks")
    created=$(pid_api POST /api/oidc/clients -d "$payload")
    id=$(printf '%s' "$created" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  fi
  if [[ -n "$(eval echo \${${prefix}_CLIENT_SECRET:-})" ]]; then
    saveenv "${prefix}_CLIENT_ID" "$id" "$envfile"
    return
  fi
  created=$(pid_api POST "/api/oidc/clients/$id/secret")
  secret=$(printf '%s' "$created" | python3 -c 'import json,sys; print(json.load(sys.stdin)["secret"])')
  saveenv "${prefix}_CLIENT_ID" "$id" "$envfile"
  saveenv "${prefix}_CLIENT_SECRET" "$secret" "$envfile"
  echo "   minted '$name' ($id) -> $envfile"
}

echo "== 1/5 OIDC clients in Pocket ID =="
mint_client forgejo "https://git.${NODE_DOMAIN}/user/oauth2/pocket-id/callback" FORGEJO_OIDC
mint_client litellm "https://llm.${NODE_DOMAIN}/sso/callback" LITELLM_OIDC
# Profile-gated surfaces (feeds/chat/apps): minting ahead of enablement is
# harmless — an unused client in Pocket ID grants nothing.
mint_client miniflux "https://feeds.${NODE_DOMAIN}/oauth2/oidc/callback" MINIFLUX_OIDC secrets/miniflux.env
mint_client open-webui "https://chat.${NODE_DOMAIN}/oauth/oidc/callback" OPENWEBUI_OIDC secrets/open-webui.env
mint_client memos "https://notes.${NODE_DOMAIN}/auth/callback" MEMOS_OIDC secrets/memos.env
mint_client oauth2-proxy "https://cal.${NODE_DOMAIN}/oauth2/callback,https://calino.${NODE_DOMAIN}/oauth2/callback,https://copilot.${NODE_DOMAIN}/oauth2/callback,https://traces.${NODE_DOMAIN}/oauth2/callback" OAUTH2_PROXY
if [[ -z "${OAUTH2_PROXY_COOKIE_SECRET:-}" ]]; then
  saveenv OAUTH2_PROXY_COOKIE_SECRET "$(openssl rand -base64 32 | head -c 32)"
  echo "   generated authshim cookie secret -> .env"
fi
# Open WebUI session-JWT key: must be OURS and stable — the image's fallback
# regenerates in the container fs on every recreate, logging everyone out.
if [[ -z "${OPENWEBUI_SECRET_KEY:-}" ]]; then
  saveenv OPENWEBUI_SECRET_KEY "$(openssl rand -hex 32)" secrets/open-webui.env
  echo "   generated open-webui session key -> secrets/open-webui.env"
fi
loadenv   # pick up whatever was just minted

echo "== 2/5 operator = LiteLLM UI admin =="
USERS_JSON=$(pid_api GET /api/users)
ADMIN_ID=$(printf '%s' "$USERS_JSON" | python3 -c "
import json,sys
users=[u for u in json.load(sys.stdin)['data'] if u.get('isAdmin')]
print(users[0]['id'] if users else '')")
OP_USERNAME=$(printf '%s' "$USERS_JSON" | python3 -c "
import json,sys
users=[u for u in json.load(sys.stdin)['data'] if u.get('isAdmin')]
print(users[0].get('username','') if users else '')")
[[ -n "$ADMIN_ID" ]] && saveenv LITELLM_PROXY_ADMIN_ID "$ADMIN_ID" && echo "   admin user id: $ADMIN_ID"

echo "== 3/5 memos: break-glass host + identity provider =="
# Memos keeps IdP config in its DB (no env plane) and can't be configured
# until a HOST account exists. Bootstrap both over the API (verified against
# memos 0.29): create the host named after the operator's Pocket ID username —
# memos maps SSO logins to accounts by username (fieldMapping.identifier =
# preferred_username), so the passkey lands on this same host account — then
# sign in with the break-glass password and seed the identity provider.
NOTES_URL="https://notes.${NODE_DOMAIN}"
NOTES_CURL=(/usr/bin/curl -sk -H "Content-Type: application/json")
[[ "$NODE_DOMAIN" == "localhost" ]] && NOTES_CURL+=(--resolve "notes.localhost:443:127.0.0.1")
MEMOS_IDP_JSON=$(cat <<EOF
{"title":"pocket-id","type":"OAUTH2","identifierFilter":"","config":{"oauth2Config":{
  "clientId":"${MEMOS_OIDC_CLIENT_ID:-}","clientSecret":"${MEMOS_OIDC_CLIENT_SECRET:-}",
  "authUrl":"$AUTH_URL/authorize","tokenUrl":"$AUTH_URL/api/oidc/token",
  "userInfoUrl":"$AUTH_URL/api/oidc/userinfo","scopes":["openid","profile","email"],
  "fieldMapping":{"identifier":"preferred_username","displayName":"name","email":"email"}}}}
EOF
)
if ! docker ps --format '{{.Names}}' | grep -qx memos; then
  echo "   memos not running (--profile apps) — skip; re-run after enabling"
else
  if "${NOTES_CURL[@]}" "$NOTES_URL/api/v1/instance/profile" | grep -q '"admin": *null'; then
    MEMOS_ADMIN_USER="${MEMOS_ADMIN_USER:-${OP_USERNAME:-admin}}"
    MEMOS_ADMIN_PASSWORD="${MEMOS_ADMIN_PASSWORD:-$(openssl rand -base64 18)}"
    saveenv MEMOS_ADMIN_USER "$MEMOS_ADMIN_USER" secrets/memos.env
    saveenv MEMOS_ADMIN_PASSWORD "$MEMOS_ADMIN_PASSWORD" secrets/memos.env
    "${NOTES_CURL[@]}" -X POST "$NOTES_URL/api/v1/users" \
      -d "{\"username\":\"$MEMOS_ADMIN_USER\",\"password\":\"$MEMOS_ADMIN_PASSWORD\"}" >/dev/null
    echo "   memos: host '$MEMOS_ADMIN_USER' created (break-glass password -> secrets/memos.env)"
  fi
  if "${NOTES_CURL[@]}" "$NOTES_URL/api/v1/identity-providers" \
      | grep -q '"title": *"pocket-id"'; then
    echo "   memos idp 'pocket-id' exists — skip"
  else
    # Any admin credential seeds it: a minted MEMOS_ADMIN_TOKEN if the
    # operator made one, else a session JWT from the break-glass signin.
    MEMOS_BEARER="${MEMOS_ADMIN_TOKEN:-}"
    if [[ -z "$MEMOS_BEARER" && -n "${MEMOS_ADMIN_PASSWORD:-}" ]]; then
      MEMOS_BEARER=$("${NOTES_CURL[@]}" -X POST "$NOTES_URL/api/v1/auth/signin" \
        -d "{\"passwordCredentials\":{\"username\":\"${MEMOS_ADMIN_USER:-admin}\",\"password\":\"$MEMOS_ADMIN_PASSWORD\"}}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("accessToken",""))' 2>/dev/null)
    fi
    if [[ -n "$MEMOS_BEARER" ]]; then
      "${NOTES_CURL[@]}" -X POST "$NOTES_URL/api/v1/identity-providers" \
        -H "Authorization: Bearer $MEMOS_BEARER" -d "$MEMOS_IDP_JSON" >/dev/null
      echo "   memos: pocket-id identity provider registered"
    else
      echo "   no memos admin credential (.env MEMOS_ADMIN_TOKEN or MEMOS_ADMIN_PASSWORD)"
      echo "   — paste into Memos Settings -> SSO by hand:"
      echo "$MEMOS_IDP_JSON" | sed 's/^/     /'
    fi
  fi
fi

echo "== 4/5 local-dev glue =="
if [[ "$NODE_DOMAIN" == "localhost" ]]; then
  # containers must reach https://auth.localhost (via Caddy) and trust its CA
  docker exec forgejo cat /etc/ssl/certs/ca-certificates.crt > .local-ca-bundle.pem
  docker exec caddy cat /data/caddy/pki/authorities/local/root.crt >> .local-ca-bundle.pem
  # Regenerated every run (it is a generated file) so new SSO'd services pick
  # up their glue on re-run.
  cat > docker-compose.override.yml <<'EOF'
# LOCAL-DEV ONLY (gitignored, generated by sso-setup.sh): with a real domain
# and public CA none of this exists. Gives containers DNS for *.localhost via
# Caddy and trust for Caddy's internal CA (system bundle + local root).
services:
  caddy:
    networks:
      edge:
        aliases: [auth.localhost, git.localhost, llm.localhost, notes.localhost]
  forgejo:
    environment:
      SSL_CERT_FILE: /certs/local-bundle.pem
    volumes:
      - ./.local-ca-bundle.pem:/certs/local-bundle.pem:ro
  litellm:
    environment:
      SSL_CERT_FILE: /certs/local-bundle.pem
    volumes:
      - ./.local-ca-bundle.pem:/certs/local-bundle.pem:ro
  memos:
    environment:
      SSL_CERT_FILE: /certs/local-bundle.pem
    volumes:
      - ./.local-ca-bundle.pem:/certs/local-bundle.pem:ro
  miniflux:
    environment:
      SSL_CERT_FILE: /certs/local-bundle.pem
    volumes:
      - ./.local-ca-bundle.pem:/certs/local-bundle.pem:ro
  open-webui:
    environment:
      SSL_CERT_FILE: /certs/local-bundle.pem
      REQUESTS_CA_BUNDLE: /certs/local-bundle.pem
    volumes:
      - ./.local-ca-bundle.pem:/certs/local-bundle.pem:ro
  oauth2-proxy:
    environment:
      SSL_CERT_FILE: /certs/local-bundle.pem
      # Chrome refuses Domain=.localhost cookies (public-suffix rule), which
      # silently drops the shim's CSRF cookie -> 403 on the OAuth callback.
      # Host-scope the cookies in local dev; real domains keep .${NODE_DOMAIN}.
      OAUTH2_PROXY_COOKIE_DOMAINS: cal.localhost,calino.localhost,copilot.localhost,traces.localhost
    volumes:
      - ./.local-ca-bundle.pem:/certs/local-bundle.pem:ro
EOF
  echo "   wrote .local-ca-bundle.pem + docker-compose.override.yml"
else
  echo "   real domain — no glue needed"
fi

echo "== 5/5 apply =="
loadenv
docker compose up -d caddy forgejo litellm >/dev/null 2>&1
# Recreate the newly-SSO'd surfaces the operator has enabled; start the
# authshim — cal's browser door depends on it now. (Miniflux reads its OIDC
# client from env too, so recreate it if the feeds profile is live.)
docker ps --format '{{.Names}}' | grep -qx open-webui && \
  docker compose --profile chat  up -d open-webui >/dev/null 2>&1
docker ps --format '{{.Names}}' | grep -qx memos && \
  docker compose --profile apps  up -d memos      >/dev/null 2>&1
docker ps --format '{{.Names}}' | grep -qx miniflux && \
  docker compose --profile feeds up -d miniflux   >/dev/null 2>&1
docker compose --profile authshim up -d oauth2-proxy >/dev/null 2>&1
# The Caddyfile is bind-mounted — a route change needs a reload, not a recreate.
docker exec -w /etc/caddy caddy caddy reload >/dev/null 2>&1
sleep 5
FJ() { docker exec -u 1000 forgejo forgejo "$@"; }
if FJ admin auth list | grep -q pocket-id; then
  echo "   forgejo auth source exists — skip"
else
  FJ admin auth add-oauth --name pocket-id --provider openidConnect \
     --key "$FORGEJO_OIDC_CLIENT_ID" --secret "$FORGEJO_OIDC_CLIENT_SECRET" \
     --auto-discover-url "$AUTH_URL/.well-known/openid-configuration" \
     --scopes "openid profile email"
  echo "   forgejo: pocket-id auth source registered"
fi
echo
echo "Done. One passkey now opens: git.$NODE_DOMAIN, llm.$NODE_DOMAIN/ui,"
echo "feeds.$NODE_DOMAIN, chat.$NODE_DOMAIN, notes.$NODE_DOMAIN, and"
echo "cal.$NODE_DOMAIN's web UI (via the authshim). DAV/API planes unchanged."
