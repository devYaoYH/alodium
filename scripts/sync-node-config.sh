#!/usr/bin/env bash
# Push the operator repo's main to Forgejo's node-config — the tree the
# JAIL clones. GitHub is the public template; Forgejo is what agents see:
# if this isn't run after pushing origin, agent-dev works against a stale
# node and its "self-repair" PRs fix a machine that no longer exists.
# Idempotent; safe to run reflexively after any push.
#
# The reverse direction (agent PRs merged in Forgejo) comes home with:
#   git fetch forgejo && git merge forgejo/main   (then push origin + here)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

REMOTE_URL="https://git.${NODE_DOMAIN}/${NODE_CONFIG_REPO}.git"
git remote get-url forgejo >/dev/null 2>&1 || git remote add forgejo "$REMOTE_URL"
[[ "$NODE_DOMAIN" == "localhost" ]] && git config http."https://git.localhost".sslVerify false

CRED='!f() { echo "username=operator"; echo "password='"$FORGEJO_TOKEN"'"; }; f'
git -c credential.helper="$CRED" push forgejo main
echo "node-config @ $(git -c credential.helper="$CRED" ls-remote forgejo main | cut -c1-7) (= local $(git rev-parse --short main))"
