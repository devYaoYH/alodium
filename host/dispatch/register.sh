#!/usr/bin/env bash
# Register the powerless doorbell runner against the COORDINATION repo only.
# Usage: ./host/dispatch/register.sh <REPO_SCOPED_REGISTRATION_TOKEN>
# The token comes from Forgejo → coordination repo → Settings → Actions →
# Runners → "Create new runner". It MUST be repo-scoped, never instance/org —
# an instance token would let this runner execute node-config's workflows too.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .env; set +a

TOKEN="${1:?usage: register.sh <repo-scoped registration token>}"

# One-off registration container: writes /data/.runner into the named volume
# the daemon later reads. Labels match runner-config.yml (host executor).
docker run --rm \
  -e GITEA_INSTANCE_URL="https://git.${NODE_DOMAIN}" \
  -e GITEA_RUNNER_REGISTRATION_TOKEN="$TOKEN" \
  -v doorbell_runner_data:/data \
  code.forgejo.org/forgejo/runner:6.0.1 \
  forgejo-runner register --no-interactive \
    --instance "https://git.${NODE_DOMAIN}" \
    --token "$TOKEN" \
    --name doorbell \
    --labels "doorbell:host"

echo "registered. bring the daemon up: docker compose -f host/dispatch/runner.compose.yml up -d"
