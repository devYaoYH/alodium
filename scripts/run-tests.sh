#!/usr/bin/env bash
# Run every app manifest's declared [tests] against the staging stack.
# The manifest is the test registry: an app with no [tests] block is skipped
# loudly — untested is a fact worth printing, not hiding.
#
#   ./scripts/staging.sh up && ./scripts/run-tests.sh
#
# Exit code = number of failures; promote.sh refuses promotion on nonzero.
# Each test runs in a minimal curl container ON the staging network with
# APP_URL pointing at the app's manifest-declared endpoint — the same wire a
# real caller uses, not a mock.
set -uo pipefail
cd "$(dirname "$0")/.."

RUNNER=curlimages/curl:latest@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13
FAILURES=0

while IFS=$'\t' read -r name port cmd timeout; do
  if [[ -z "$cmd" ]]; then
    echo "SKIP  $name — manifest declares no [tests]"
    continue
  fi
  # Test on the wire the app actually lives on (a spur-only app like the
  # bridge is not on edge). Not running in staging = skipped loudly.
  NET=$(docker inspect --format '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}} {{end}}' \
        "sovereign-staging-$name-1" 2>/dev/null | awk '{print $1}')
  if [[ -z "$NET" ]]; then
    echo "SKIP  $name — not running in staging (profile not enabled?)"
    continue
  fi
  echo "TEST  $name: $cmd"
  if docker run --rm --network "$NET" -e "APP_URL=http://$name:$port" \
       --entrypoint sh "$RUNNER" -c "timeout ${timeout:-300} $cmd" >/dev/null 2>&1; then
    echo "GREEN $name"
  else
    echo "RED   $name"
    FAILURES=$((FAILURES+1))
  fi
done < <(python3 - <<'EOF'
import tomllib, pathlib
for p in sorted(pathlib.Path("manifest").glob("*.toml")):
    if p.name.endswith(".example.toml"): continue
    m = tomllib.loads(p.read_text())
    print("\t".join([
        m.get("app", {}).get("name", p.stem),
        str(m.get("service", {}).get("port", "")),
        m.get("tests", {}).get("command", ""),
        str(m.get("tests", {}).get("timeout_seconds", "")),
    ]))
EOF
)

echo
echo "$FAILURES failure(s)."
exit "$FAILURES"
