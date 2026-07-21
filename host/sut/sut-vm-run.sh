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
    --profile apps --profile feeds logs --no-color >>"$LOG" 2>&1 || true
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

# App templates deliberately contain blank real credentials. Each required
# value receives a deterministic-but-non-secret placeholder so service startup
# tests exercise wiring without reading or reusing the node's secret files.
python3 - <<'PY'
import pathlib, re
for path in pathlib.Path("secrets").glob("*.env"):
    rows = []
    for row in path.read_text().splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=\s*(?:#.*)?$", row)
        rows.append(f"{match.group(1)}=sut-{match.group(1).lower()}" if match else row)
    path.write_text("\n".join(rows) + "\n")
PY

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

# Production bootstrapping creates Radicale's htpasswd and rights files after
# the stack first comes up.  A disposable staging volume needs the minimum
# equivalent state before the server can start, otherwise the SUT would report
# a harness-only crash rather than testing the candidate.  The contents are
# synthetic and live only in this worker's named volume.
if "${compose[@]}" config --services | grep -qx radicale; then
  docker volume create "${PROJECT}_radicale_data" >>"$LOG" 2>&1
  docker run --rm --user 0:0 -v "${PROJECT}_radicale_data:/data" alpine \
    sh -c 'mkdir -p /data/collections; : > /data/users; cat > /data/rights <<"EOF"
[root]
user: .+
collection:
permissions: R

[principal]
user: .+
collection: {user}
permissions: RW

[calendars]
user: .+
collection: {user}/[^/]+
permissions: rw
EOF' >>"$LOG" 2>&1
fi

# A clean worker has no locally-built Geth images. Build every app that follows
# the node's `apps/<name>/Dockerfile -> sovereign-node/<name>:local` convention
# before Compose resolves image-only fragments (notably search-broker). The
# candidate controls these Dockerfiles, but only inside this single-use VM.
for appdir in apps/*; do
  [[ -f "$appdir/Dockerfile" ]] || continue
  app="$(basename "$appdir")"
  if ! docker build -t "sovereign-node/${app}:local" "$appdir" >>"$LOG" 2>&1; then
    REASON="local image build failed: ${app}"; exit 1
  fi
done

# External apps and mirrored upstream images are fetched by the host according
# to its reviewed source allowlist, then arrive as source-only snapshots under
# dependencies/. The candidate cannot ask the host to clone another repo.
if [[ -f dependencies/build-sources.json ]]; then
  while IFS=$'\t' read -r image name args_json; do
    context="dependencies/$name"
    [[ -f "$context/Dockerfile" ]] || { REASON="allowed source has no Dockerfile: ${name}"; exit 1; }
    build=(docker build -t "$image")
    while IFS= read -r arg; do build+=(--build-arg "$arg"); done < <(
      python3 - "$args_json" <<'PY'
import json, sys
for key, value in json.loads(sys.argv[1]).items():
    print(f"{key}={value}")
PY
    )
    if ! "${build[@]}" "$context" >>"$LOG" 2>&1; then
      REASON="allowed source image build failed: ${name}"; exit 1
    fi
  done < <(
    python3 - <<'PY'
import json
for source in json.load(open("dependencies/build-sources.json")):
    print("\t".join((source["image"], source["name"], json.dumps(source.get("args", {}), sort_keys=True))))
PY
  )
fi

if ! "${compose[@]}" up -d --build --quiet-pull >>"$LOG" 2>&1; then
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

# `docker compose up -d` can return success while a service immediately enters
# a crash loop.  Let initial processes settle, then fail before manifest tests
# if any declared container is already restarting, dead, or exited.  This is
# intentionally independent of the app manifest so shared infrastructure and
# new services are covered too.
for _ in 1 2 3; do sleep 5; done
unstable="$(
  "${compose[@]}" ps --services --status restarting
  "${compose[@]}" ps --services --status dead
  "${compose[@]}" ps --services --status exited
)"
if [[ -n "$unstable" ]]; then
  REASON="service failed to stabilize: $(tr '\n' ',' <<<"$unstable" | sed 's/,$//')"; exit 1
fi

# The candidate declares per-app smoke commands in its manifests.  Run those
# across the staging networks, then retain the complete worker log as evidence.
if ! ./scripts/run-tests.sh >>"$LOG" 2>&1; then
  REASON="manifest smoke tests failed"; exit 1
fi
STATUS="pass"
REASON="compose started; manifest smoke tests passed"
