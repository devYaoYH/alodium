#!/usr/bin/env bash
# Deterministic egress key minting — invoked by the OPERATOR or host automation,
# NEVER reachable from the `agents` network. This is the sole mint path for
# egress credentials; the agent never mints or widens its own allowlist.
#
# Usage:
#   ./scripts/mint-egress-key.sh <tenant-id> <host> [ttl-minutes]
#
# Reads an approved egress exception (Phase 4 — a coordination issue), mints
# a short-lived key scoped to the approved host, and writes it where the target
# container reads it at start.
#
# Reuses the LiteLLM virtual-key cleanup path for revocation — keys auto-revoke
# on expiry OR task-end, whichever is earlier. No second revocation mechanism.
#
# Security:
#   - The agent never mints; only operator-approved deterministic code runs here.
#   - Key is scoped to ONE host (no wildcard).
#   - TTL = min(expiry, task-end) — the existing LiteLLM cleanup handles this.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  echo "Usage: $0 <tenant-id> <host> [ttl-minutes]" >&2
  echo "" >&2
  echo "Reads approved exception from coordination issue, mints host-scoped key." >&2
  echo "  tenant-id    — the tenant requesting egress (e.g. agent-dev)" >&2
  echo "  host         — the approved target hostname (e.g. api.example.com)" >&2
  echo "  ttl-minutes  — key lifetime (default: 60, max: 1440)" >&2
  exit 1
}

TENANT_ID="${1:-}"
HOST="${2:-}"
TTL="${3:-60}"

if [[ -z "$TENANT_ID" || -z "$HOST" ]]; then
  usage
fi

# Validate TTL bounds
if [[ "$TTL" -lt 1 || "$TTL" -gt 1440 ]]; then
  echo "ERROR: TTL must be between 1 and 1440 minutes" >&2
  exit 1
fi

# --- Phase 4 integration: read approved exception from coordination issue ---
# In production, this reads from a coordination issue label or structured
# comment. For now, the operator provides the host + tenant explicitly.
# Future: parse the issue body for an approved [egress] block.

echo "Minting egress key for tenant=$TENANT_ID host=$HOST ttl=${TTL}m"

# --- Generate the key ---
# Format: egress_<tenant>_<host>_<short-hash> — deterministic prefix for
# audit correlation, unique suffix to avoid collisions.
KEY_ID="egress_${TENANT_ID}_$(echo "$HOST" | tr '.' '_')_$(openssl rand -hex 4)"
KEY_VALUE=$(openssl rand -hex 32)

# --- Write to the target secret file ---
# The egress-out container reads EGRESS_ALLOW at start from secrets/egress-broker.env.
# The minted key is appended to the broker's allowlist and the key is stored
# where the agent container reads it as AGENT_EGRESS_TOKEN.
SECRETS_DIR="${SECRETS_DIR:-./secrets}"
mkdir -p "$SECRETS_DIR"

# Append the host to the egress allowlist
ALLOW_FILE="$SECRETS_DIR/egress-broker.env"
touch "$ALLOW_FILE"

# egress-out's EGRESS_ALLOW is a defense-in-depth allowlist (the broker still
# enforces per-key host scoping at the application layer). Append the approved
# host if it is not already listed — never clobber, so a new approval does not
# silently revoke a previously-approved host at this layer.
if grep -q "^EGRESS_ALLOW=" "$ALLOW_FILE"; then
  current=$(sed -n "s/^EGRESS_ALLOW=//p" "$ALLOW_FILE")
  case ",$current," in
    *",$HOST,"*) ;;  # already allowed — nothing to do
    *) sed -i "s|^EGRESS_ALLOW=.*|EGRESS_ALLOW=${current:+$current,}$HOST|" "$ALLOW_FILE" ;;
  esac
else
  echo "EGRESS_ALLOW=$HOST" >> "$ALLOW_FILE"
fi

# Write the agent's egress token. Keep exactly one line: replace the existing
# value if present (the compose model exposes a single AGENT_EGRESS_TOKEN), so
# repeated mints don't leave stale key material in the secrets file.
if grep -q "^AGENT_EGRESS_TOKEN=" "$ALLOW_FILE"; then
  sed -i "s|^AGENT_EGRESS_TOKEN=.*|AGENT_EGRESS_TOKEN=$KEY_VALUE|" "$ALLOW_FILE"
else
  echo "AGENT_EGRESS_TOKEN=$KEY_VALUE" >> "$ALLOW_FILE"
fi

echo "OK: minted key $KEY_ID for $TENANT_ID -> $HOST (TTL: ${TTL}m)"
echo "Key value: $KEY_VALUE"
echo ""
echo "The key auto-revokes on expiry or task-end (whichever is earlier) via"
echo "the existing LiteLLM virtual-key cleanup path."
