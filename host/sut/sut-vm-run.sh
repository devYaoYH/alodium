#!/usr/bin/env bash
# Trusted helper copied into the isolated SUT VM by sutctl.sh.  The candidate
# tree is data at $1; this helper does not source candidate scripts for setup.
set -euo pipefail
ROOT="${1:?candidate root required}"
TIMEOUT="${SUT_TIMEOUT:-240}"
PROJECT="sovereign-staging"
RESULT="$ROOT/.sut-result.json"
LOG="$ROOT/.sut-worker.log"
STATUS="fail"
REASON="unknown"

write_result() {
  python3 - "$RESULT" "$STATUS" "$REASON" <<'PY'
import json, sys, time
open(sys.argv[1], "w").write(json.dumps({
  "status": sys.argv[2], "reason": sys.argv[3],
  "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}) + "\n")
PY
}
cleanup() {
  docker compose -p "$PROJECT" -f "$ROOT/docker-compose.yml" -f "$ROOT/docker-compose.staging.yml" \
    --profile apps --profile feeds down -v --remove-orphans >>"$LOG" 2>&1 || true
  write_result
}
trap cleanup EXIT

cd "$ROOT"
[[ -f .env.example ]] || { REASON="candidate has no .env.example"; exit 1; }
cp .env.example .env
mkdir -p secrets
for example in apps/*/env.example; do
  [[ -f "$example" ]] || continue
  cp "$example" "secrets/$(basename "$(dirname "$example")").env"
done

# Synthetic values satisfy Compose interpolation. They are deliberately not
# production credentials and remain only in this worker's disposable disk.
setenv() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then sed -i "s|^${key}=.*|${key}=${value}|" .env
  else printf '%s=%s\n' "$key" "$value" >> .env; fi
}
setenv NODE_DOMAIN sut.invalid
setenv ACME_EMAIL sut@example.invalid
setenv LITELLM_MASTER_KEY sut-master-key
setenv LITELLM_SALT_KEY sut-salt-key
setenv LITELLM_DB_PASSWORD sut-db-password
setenv FORGEJO_TOKEN sut-forgejo-token
setenv AGENT_FORGEJO_TOKEN sut-agent-token
setenv AGENT_LLM_KEY sut-agent-llm-key

compose=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.staging.yml --profile apps --profile feeds)
if ! "${compose[@]}" config --quiet >>"$LOG" 2>&1; then
  REASON="compose configuration failed"; exit 1
fi
if ! "${compose[@]}" up -d --quiet-pull >>"$LOG" 2>&1; then
  REASON="compose startup failed"; exit 1
fi

deadline=$(( $(date +%s) + TIMEOUT ))
while (( $(date +%s) < deadline )); do
  services=$("${compose[@]}" ps --services --status running | wc -l | tr -d ' ')
  [[ "$services" -gt 0 ]] && break
  sleep 2
done
if [[ "${services:-0}" -eq 0 ]]; then
  REASON="no service reached running state within ${TIMEOUT}s"; exit 1
fi

# The candidate declares per-app smoke commands in its manifests.  Run those
# across the staging networks, then retain the complete worker log as evidence.
if ! ./scripts/run-tests.sh >>"$LOG" 2>&1; then
  REASON="manifest smoke tests failed"; exit 1
fi
STATUS="pass"
REASON="compose started; manifest smoke tests passed"
