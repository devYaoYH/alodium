#!/usr/bin/env bash
# Invite a trusted user: create them in Pocket ID and print a one-time access
# link (the M4 QR-invite backend). They open the link on their phone, enroll
# a passkey, done — no password exists to set.
#
# Usage:  ./scripts/invite.sh <username> <email> [display-name]
# Env:    POCKET_ID_API_KEY in .env (Pocket ID admin -> API Keys)
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

USERNAME="${1:?usage: invite.sh <username> <email> [display-name]}"
EMAIL="${2:?usage: invite.sh <username> <email> [display-name]}"
DISPLAY="${3:-$USERNAME}"
AUTH_URL="https://auth.${NODE_DOMAIN:-localhost}"
: "${POCKET_ID_API_KEY:?set POCKET_ID_API_KEY in .env (Pocket ID admin -> API Keys)}"

CURL=(curl -sS -H "X-API-KEY: $POCKET_ID_API_KEY" -H "Content-Type: application/json")
[[ "$AUTH_URL" == *localhost* ]] && CURL+=(-k --resolve "auth.localhost:443:127.0.0.1")

FIRST="${DISPLAY%% *}"; LAST="${DISPLAY#* }"; [[ "$LAST" == "$DISPLAY" ]] && LAST=""

USER_ID=$("${CURL[@]}" "$AUTH_URL/api/users" -d "{
  \"username\": \"$USERNAME\", \"email\": \"$EMAIL\",
  \"firstName\": \"$FIRST\", \"lastName\": \"$LAST\"
}" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r.get("id") or sys.exit("create failed: %s" % r))')

TOKEN=$("${CURL[@]}" "$AUTH_URL/api/users/$USER_ID/one-time-access-token" \
  -d '{"expiresAt": "'"$(date -u -v+72H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+72 hours' +%Y-%m-%dT%H:%M:%SZ)"'"}' \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r.get("token") or sys.exit("token failed: %s" % r))')

LINK="$AUTH_URL/lc/$TOKEN"
echo
echo "  Invite for $DISPLAY <$EMAIL> — valid 72h, single use:"
echo
echo "  $LINK"
echo
command -v qrencode >/dev/null && qrencode -t ANSIUTF8 "$LINK" || \
  echo "  (brew install qrencode for a scannable QR here)"
