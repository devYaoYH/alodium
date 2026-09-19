#!/usr/bin/env bash
# Run every app manifest's declared [tests] against the staging stack.
# The manifest is the test registry: an app with no [tests] block is skipped
# loudly — untested is a fact worth printing, not hiding.
#
#   ./scripts/staging.sh up && ./scripts/run-tests.sh
#
# Exit code = number of failures; promote.sh refuses promotion on nonzero.
# Each test runs in a throwaway container attached to EVERY network the app's
# container is on, with APP_URL pointing at the app's manifest-declared
# endpoint — the same wire a real caller uses, not a mock. The app is found by
# its compose labels (project + service), so a fixed container_name does not
# hide it. [tests].service names the compose service when it differs from the
# app name (redash's is redash-server).
#
# `curl ...` commands run in a curl image. `python3 ...` commands (the
# app-skeleton's tests/smoke.py contract) run in a stdlib Python image with the
# app's directory mounted read-only as the working directory: apps/<name>, or
# dependencies/<name> when the tests live in a mirrored repo (the SUT worker
# stages those there).
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT="${STAGE_PROJECT:-sovereign-staging}"
CURL_RUNNER=curlimages/curl:latest@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13
PY_RUNNER=python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
FAILURES=0

while IFS=$'\t' read -r name service port timeout cmd; do
  if [[ -z "$cmd" ]]; then
    echo "SKIP  $name — manifest declares no [tests]"
    continue
  fi
  CID=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
        --filter "label=com.docker.compose.service=$service" | head -1)
  if [[ -z "$CID" ]]; then
    echo "SKIP  $name — service '$service' not running in $PROJECT (profile not enabled?)"
    continue
  fi
  NETS=$(docker inspect --format '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CID")
  runner=("$CURL_RUNNER") mount=()
  if [[ "$cmd" == python3* ]]; then
    runner=("$PY_RUNNER")
    dir="apps/$name"
    [[ -d "$dir/tests" || ! -d "dependencies/$name/tests" ]] || dir="dependencies/$name"
    mount=(-v "$PWD/$dir:/app:ro" -w /app)
  fi
  echo "TEST  $name ($service): $cmd"
  # create → connect every network → start: `docker run` joins only one.
  first="${NETS%% *}"
  TID=$(docker create --network "$first" -e "APP_URL=http://$service:$port" \
        "${mount[@]+"${mount[@]}"}" --entrypoint sh "${runner[@]}" -c "timeout ${timeout:-300} $cmd")
  for net in $NETS; do
    [[ "$net" == "$first" ]] || docker network connect "$net" "$TID" >/dev/null
  done
  if docker start -a "$TID" >/tmp/run-tests.$$.log 2>&1; then
    echo "GREEN $name"
  else
    echo "RED   $name"
    sed 's/^/      | /' /tmp/run-tests.$$.log | tail -n 15
    FAILURES=$((FAILURES+1))
  fi
  docker rm -f "$TID" >/dev/null 2>&1
  rm -f /tmp/run-tests.$$.log
done < <(python3 - <<'EOF'
import tomllib, pathlib
for p in sorted(pathlib.Path("manifest").glob("*.toml")):
    if p.name.endswith(".example.toml"): continue
    m = tomllib.loads(p.read_text())
    name = m.get("app", {}).get("name", p.stem)
    tests = m.get("tests", {})
    print("\t".join([
        name,
        tests.get("service", name),
        str(m.get("service", {}).get("port", "")),
        str(tests.get("timeout_seconds", "")),
        tests.get("command", ""),   # last: may be empty, and tab is IFS whitespace
    ]))
EOF
)

echo
echo "$FAILURES failure(s)."
exit "$FAILURES"
