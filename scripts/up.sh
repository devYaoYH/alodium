#!/usr/bin/env bash
# One command from a fresh clone to a running node. Chains the pieces that were
# previously a documented "now run these five scripts by hand" list, in order,
# each already idempotent so re-running is safe (it's also the upgrade path).
#
#   ./scripts/up.sh            # full bring-up / reconcile
#   ./scripts/up.sh --check    # validate only; start & change nothing
#
# What still needs a human afterwards is printed at the end — and ONLY that:
# the irreducible steps (register a passkey in the browser; supply a provider
# API key if you haven't). Everything a credential-holding script can do, it does.
set -euo pipefail
cd "$(dirname "$0")/.."
CHECK="${1:-}"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# 1. .env + manifest + validate (install.sh is the scribe; --check is CI-safe)
step "1/7 install (scaffold + validate)"
if [[ "$CHECK" == "--check" ]]; then ./scripts/install.sh --check; else ./scripts/install.sh; fi

if [[ "$CHECK" == "--check" ]]; then
  step "check mode — not starting containers"
  echo "install validated; run without --check to bring the node up."
  exit 0
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

# 2. the stack
step "2/7 docker compose up"
docker compose up -d

# 3. wait for the identity + git + llm plane to answer before bootstrapping them
step "3/7 wait for core plane"
wait_for() { local url="$1" name="$2"; for _ in $(seq 1 60); do
    curl -skf --resolve "${url#https://}:443:127.0.0.1" "$url" >/dev/null 2>&1 && { echo "   $name ready"; return 0; }
    sleep 2; done; echo "   WARN: $name not ready after 120s (continuing)"; }
wait_for "https://git.${NODE_DOMAIN}/api/healthz"  forgejo
wait_for "https://auth.${NODE_DOMAIN}/healthz"     pocket-id
wait_for "https://llm.${NODE_DOMAIN}/health/liveliness" litellm

# 4. git plane: agent user, coordination repo, labels (idempotent, dedup-safe now)
step "4/7 bootstrap-forgejo"
./scripts/bootstrap-forgejo.sh

# 5. one passkey at every door (per-app OIDC clients; reruns = all-skips)
step "5/7 sso-setup"
./scripts/sso-setup.sh

# 6. assigned-issue dispatch: register the powerless runner (repo-scoped token
#    minted via admin API — no UI), start it, install the host dispatcher.
step "6/7 dispatch (doorbell runner + host gate)"
if [[ "${ENABLE_DISPATCH:-1}" == "1" ]]; then
  # One spool path shared by the runner (writes) and the dispatcher (watches).
  export DISPATCH_SPOOL="${DISPATCH_SPOOL:-$PWD/.task-dispatch/spool}"
  mkdir -p "$DISPATCH_SPOOL"
  ./host/dispatch/register.sh
  docker compose -f host/dispatch/runner.compose.yml up -d
  # install the workflow into coordination (idempotent push of one file)
  ./host/dispatch/install-workflow.sh || echo "   (workflow install skipped — see host/dispatch/README.md)"
  # host dispatcher: launchd on darwin, cron hint elsewhere
  if [[ "$(uname)" == "Darwin" ]]; then
    ./host/dispatch/install-launchd.sh || echo "   (launchd install skipped — see host/dispatch/README.md)"
  else
    echo "   non-darwin: add the cron line from host/dispatch/README.md"
  fi
else
  echo "   ENABLE_DISPATCH=0 — skipped"
fi

# 7. auto-deploy: merged PRs apply themselves — a launchd heartbeat polls
#    forgejo/main every 2 minutes and runs scripts/deploy.sh on change. Merge
#    stays the authorization moment; this removes the manual keystroke after it.
step "7/7 auto-deploy watcher"
if [[ "${ENABLE_AUTODEPLOY:-1}" == "1" ]]; then
  if [[ "$(uname)" == "Darwin" ]]; then
    ./host/deploy-watch/install-launchd.sh || echo "   (launchd install skipped — see host/deploy-watch/README.md)"
  else
    echo "   non-darwin: add the cron line from host/deploy-watch/README.md"
  fi
else
  echo "   ENABLE_AUTODEPLOY=0 — skipped"
fi

# --- the irreducible human remainder ----------------------------------------
step "DONE — human steps that remain (only these)"
cat <<EOF
1. Register a passkey at each door in your browser (SSO can't be scripted):
   home.${NODE_DOMAIN}  git.${NODE_DOMAIN}  chat.${NODE_DOMAIN}  cal.${NODE_DOMAIN}
2. If inference isn't working, put a provider API key in .env (ANTHROPIC_API_KEY
   / OPENAI_API_KEY / OPENROUTER_API_KEY) and: docker compose up -d litellm
Everything else — users, tokens, OIDC clients, labels, the dispatch runner,
auto-deploy — is already provisioned. Re-run ./scripts/up.sh any time to
reconcile. Merged PRs deploy themselves within ~2 minutes.

The isolated SUT development plane is intentionally disabled by default for
this stable build. Enable it later only on a development machine:
   ENABLE_SUT=1 ./scripts/bootstrap-forgejo.sh
Then follow host/sut/README.md to provision its separate worker.
EOF
