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
  # Container states and kernel OOM kills: a service that dies silently is
  # usually out of memory, and the worker's size is the host's to change.
  { echo "== containers at teardown"
    docker ps -a --format '{{.Names}}\t{{.State}}\t{{.Status}}'
    echo "== kernel OOM kills"
    sudo dmesg 2>/dev/null | grep -iE 'out of memory|oom-kill|killed process' | tail -n 20 || true
  } >>"$LOG" 2>&1 || true
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
# The operator identity services bootstrap their first admin from (redash-init
# refuses to start without one).
setenv RADICALE_OPERATOR_EMAIL operator@sut.invalid

compose=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.staging.yml --profile apps --profile feeds)
if ! "${compose[@]}" config --quiet >>"$LOG" 2>&1; then
  REASON="compose configuration failed"; exit 1
fi

# Browser-side dependencies ([[vendor]] in manifests) are gitignored and staged
# on the host by deploy.sh before any build, so a fresh checkout cannot build
# those apps without them. The node's own package mirror needs a read:package
# credential this worker must never hold; the public registry serves the same
# tarballs, and fetch-vendor.sh verifies each against the manifest's pinned
# sha512, so the source does not change what gets built.
if [[ -x scripts/fetch-vendor.sh ]]; then
  if ! VENDOR_REGISTRY=https://registry.npmjs.org VENDOR_TOKEN=none \
       ./scripts/fetch-vendor.sh >>"$LOG" 2>&1; then
    REASON="vendor staging failed (scripts/fetch-vendor.sh)"; exit 1
  fi
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

# Mirrored upstream images: the host cloned each [build] repo named by the
# candidate's manifests (only if it is on the host's allowlist), and the
# snapshots arrive source-only under dependencies/. The node-config patch comes
# from the candidate tree and is applied here, inside the disposable VM, as
# build-mirrored.sh applies it on deploy.
if [[ -f dependencies/build-sources.json ]]; then
  while IFS=$'\t' read -r image name args_json patch; do
    context="dependencies/$name"
    [[ -f "$context/Dockerfile" ]] || { REASON="allowed source has no Dockerfile: ${name}"; exit 1; }
    if [[ -n "$patch" ]] && ! (cd "$context" && git apply "$ROOT/$patch") >>"$LOG" 2>&1; then
      REASON="node-config patch did not apply: ${patch}"; exit 1
    fi
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
    # patch last: tab is IFS whitespace, so an empty middle field would collapse.
    print("\t".join((source["image"], source["name"],
                     json.dumps(source.get("args", {}), sort_keys=True), source.get("patch", ""))))
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
# One-shot init services (redash-init, ...) exit 0 by design; only a non-zero
# exit is a failure.
unstable="$(
  "${compose[@]}" ps --services --status restarting
  "${compose[@]}" ps --services --status dead
  "${compose[@]}" ps -a --status exited --format '{{.Service}} {{.ExitCode}}' | awk '$2 != 0 {print $1}'
)"
unstable="$(grep -v '^[[:space:]]*$' <<<"$unstable" || true)"
if [[ -n "$unstable" ]]; then
  REASON="service failed to stabilize: $(paste -sd, - <<<"$unstable")"; exit 1
fi

# The candidate declares per-app smoke commands in its manifests.  Run those
# across the staging networks, then retain the complete worker log as evidence.
if ! ./scripts/run-tests.sh >>"$LOG" 2>&1; then
  REASON="manifest smoke tests failed"; exit 1
fi
STATUS="pass"
REASON="compose started; manifest smoke tests passed"
