#!/usr/bin/env bash
# The change pipeline's promotion step (M2): staging → tests → prod, with
# refusal on red enforced mechanically, not by convention.
#
#   ./scripts/promote.sh
#
# Flow: bring up the staging twin of the CURRENT checkout → run every app's
# manifest-declared tests against it → on green, tag and deploy prod, tear
# staging down → on red, refuse and LEAVE STAGING UP for inspection.
#
# The operator's merge in Forgejo remains the authorization moment; this
# script is the deterministic muscle after it. Agents cannot run it: it
# touches docker, which the jail structurally lacks.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/4 staging up (same stack, throwaway volumes) =="
./scripts/staging.sh up
# Give slow starters a moment; per-app health tests do the real waiting.
sleep 10

echo "== 2/4 manifest-declared tests =="
if ! ./scripts/run-tests.sh; then
  echo
  echo "REFUSED: promotion on red is not a judgment call."
  echo "Staging is still up for inspection:  ./scripts/staging.sh ps"
  echo "When done:                            ./scripts/staging.sh down"
  exit 1
fi

echo "== 3/4 tag =="
TAG="deploy-$(date +%Y%m%d-%H%M%S)"
git tag "$TAG"
echo "   $TAG (rollback: git revert or checkout the previous deploy-* tag, then deploy.sh)"

echo "== 4/4 deploy prod, staging down =="
./scripts/deploy.sh
./scripts/staging.sh down
echo
echo "Promoted at $TAG. Images pinned by digest, config pinned by git tag."
