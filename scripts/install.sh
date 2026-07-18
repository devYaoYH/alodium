#!/usr/bin/env bash
# The installer (M1): the interview that replaces the README.
#
#   ./scripts/install.sh          # interview (first run) + validate (every run)
#   ./scripts/install.sh --check  # validation only, no prompts (CI-friendly)
#
# Reads and writes the placement manifest — manifest/node.yaml is the contract,
# this script is merely its scribe: it detects reachability, PROPOSES front-door
# placement, scaffolds .env, and validates that manifest, compose, and app
# manifests agree. Idempotent; owns nothing it can't re-derive.
#
# Deliberately deterministic. The M1 "installer-agent" wraps THIS in
# conversation (and drives DNS/VPS with operator-granted tokens); the
# mechanical steps live here where they can be read, tested, and re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
CHECK_ONLY="${1:-}"
ask() { local v; read -r -p "$1 [$2]: " v; echo "${v:-$2}"; }

# --- 1. .env scaffold --------------------------------------------------------
echo "== 1/4 .env =="
if [[ -f .env ]]; then
  echo "   exists — skip scaffold"
elif [[ "$CHECK_ONLY" == "--check" ]]; then
  echo "   MISSING (run without --check to scaffold)"; exit 1
else
  cp .env.example .env
  DOMAIN=$(ask "your domain (you own it at a registrar)" "example.com")
  EMAIL=$(ask "operator email (ACME notices)" "you@$DOMAIN")
  sed -i '' -e "s|^NODE_DOMAIN=.*|NODE_DOMAIN=$DOMAIN|" \
            -e "s|^ACME_EMAIL=.*|ACME_EMAIL=$EMAIL|" \
            -e "s|^LITELLM_MASTER_KEY=.*|LITELLM_MASTER_KEY=sk-$(openssl rand -hex 32)|" \
            -e "s|^LITELLM_SALT_KEY=.*|LITELLM_SALT_KEY=$(openssl rand -hex 32)|" \
            -e "s|^LITELLM_DB_PASSWORD=.*|LITELLM_DB_PASSWORD=$(openssl rand -hex 24)|" .env
  echo "   scaffolded .env (secrets generated; add your provider API key yourself)"
fi
set -a; source .env; set +a

# --- 2. reachability -> front-door proposal ---------------------------------
echo "== 2/4 reachability =="
PUB4=$(curl -4fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)
PUB6=$(curl -6fsS --max-time 5 https://api64.ipify.org 2>/dev/null || true)
if [[ -z "$PUB4" && -z "$PUB6" ]]; then
  PROPOSAL=none; WHY="no internet route detected — off-grid profile (or run this again online)"
elif [[ "$PUB4" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. ]]; then
  PROPOSAL=guided-vps; WHY="IPv4 is CGNAT ($PUB4) — inbound is impossible; an outbound-dialed anchor is the fix"
else
  PROPOSAL=direct; WHY="public IPv4 $PUB4${PUB6:+ (+IPv6)} — direct works IF you can forward 80/443 to this box; otherwise choose an anchor"
fi
echo "   proposal: front_door=$PROPOSAL — $WHY"

# --- 3. placement manifest ---------------------------------------------------
echo "== 3/4 placement manifest =="
if [[ -f manifest/node.yaml ]]; then
  echo "   exists — skip interview"
elif [[ "$CHECK_ONLY" == "--check" ]]; then
  echo "   MISSING (run without --check to create)"; exit 1
else
  NAME=$(ask "node name (pick something you love)" "hearth-01")
  DOOR=$(ask "front_door (direct|byo-anchor|guided-vps|none)" "$PROPOSAL")
  sed -e "s|name: hearth-01 .*|name: $NAME|" \
      -e "s|domain: example.com .*|domain: ${NODE_DOMAIN}|" \
      -e "s|operator: you@example.com|operator: ${ACME_EMAIL}|" \
      -e "s|front_door: direct .*|front_door: $DOOR|" \
      manifest/node.example.yaml > manifest/node.yaml
  echo "   wrote manifest/node.yaml (front_door: $DOOR)"
  [[ "$DOOR" == "guided-vps" || "$DOOR" == "byo-anchor" ]] && \
    echo "   -> anchor/README.md has the 20-minute bring-up"
fi

# --- 4. validate: manifest <-> compose <-> app manifests agree ---------------
echo "== 4/4 validate =="
ERRORS=0
COMPOSE_SERVICES=$(docker compose --profile apps --profile feeds --profile bridge \
                   --profile agent --profile authshim config --services 2>/dev/null)
ENABLED=$(python3 - <<'EOF'
import re, pathlib
# services.enabled from node.yaml without a YAML dependency: one flat list line
m = re.search(r"enabled:\s*\[([^\]]*)\]", pathlib.Path("manifest/node.yaml").read_text())
print(" ".join(s.strip() for s in m.group(1).split(",")) if m else "")
EOF
)
for svc in $ENABLED; do
  if ! grep -qx "$svc" <<<"$COMPOSE_SERVICES"; then
    echo "   ERROR: manifest enables '$svc' but compose defines no such service"; ERRORS=$((ERRORS+1))
  fi
done
python3 - <<'EOF' || ERRORS=$((ERRORS+1))
import tomllib, pathlib, sys
bad = 0
for p in sorted(pathlib.Path("manifest").glob("*.toml")):
    try: tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e: print(f"   ERROR: {p}: {e}"); bad = 1
sys.exit(bad)
EOF
grep -q "CHANGE-ME" .env && { echo "   ERROR: .env still contains CHANGE-ME values"; ERRORS=$((ERRORS+1)); }
docker compose config --quiet 2>/dev/null || { echo "   ERROR: compose file invalid"; ERRORS=$((ERRORS+1)); }

if [[ $ERRORS -gt 0 ]]; then echo; echo "$ERRORS problem(s) — fix before bringing the stack up."; exit 1; fi
echo "   manifest, compose, and app manifests agree"
echo
echo "Next:  docker compose up -d"
echo "       ./scripts/bootstrap-forgejo.sh     (git + agent user + coordination)"
echo "       ./scripts/sso-setup.sh             (one passkey at every door)"
echo "       cp scripts/backup.env.example scripts/backup.env && ./scripts/backup.sh init"
