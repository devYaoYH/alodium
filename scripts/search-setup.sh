#!/usr/bin/env bash
# Provision the narrow search capability after the operator adds EXA_API_KEY to
# .env. It is safe to re-run: existing capability tokens are retained, and the
# provider key is copied into the app-specific secret file without printing it.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "search-setup: missing .env (run scripts/install.sh first)" >&2; exit 1; }
set -a; source .env; set +a

mkdir -p secrets
SECRET_FILE="secrets/search-broker.env"
[[ -f "$SECRET_FILE" ]] || cp apps/search-broker/env.example "$SECRET_FILE"

value_of() {
  local key="$1" file="$2"
  grep -m1 "^${key}=" "$file" 2>/dev/null | cut -d= -f2- || true
}

upsert() {
  local key="$1" value="$2" file="$3"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i '' "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

# The key may have been placed in .env for first-time setup. We copy it into
# this app's secret file; it still never enters agent containers. Keep .env
# unchanged so this migration is reversible until the operator verifies search.
if [[ -z "$(value_of EXA_API_KEY "$SECRET_FILE")" ]]; then
  : "${EXA_API_KEY:?search-setup: set EXA_API_KEY in .env first}"
  upsert EXA_API_KEY "$EXA_API_KEY" "$SECRET_FILE"
  echo "   copied EXA_API_KEY into host-only $SECRET_FILE"
fi

if [[ -z "$(value_of SEARCH_EGRESS_TOKEN "$SECRET_FILE")" ]]; then
  upsert SEARCH_EGRESS_TOKEN "$(openssl rand -hex 32)" "$SECRET_FILE"
  echo "   minted SEARCH_EGRESS_TOKEN (broker <-> egress only)"
fi

if [[ -z "${AGENT_SEARCH_TOKEN:-}" ]]; then
  upsert AGENT_SEARCH_TOKEN "$(openssl rand -hex 32)" .env
  echo "   minted AGENT_SEARCH_TOKEN (agent-dev -> broker capability)"
else
  echo "   AGENT_SEARCH_TOKEN already exists — retained"
fi

if [[ -z "${SEARCH_AUDIT_TOKEN:-}" ]]; then
  upsert SEARCH_AUDIT_TOKEN "$(openssl rand -hex 32)" .env
  echo "   minted SEARCH_AUDIT_TOKEN (Caddy -> audit dashboard only)"
else
  echo "   SEARCH_AUDIT_TOKEN already exists — retained"
fi

echo "search capability provisioned; build the app image, then run: docker compose --profile apps up -d search-broker search-egress caddy"
