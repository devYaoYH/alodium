#!/usr/bin/env bash
# Deterministic boundary drill for the ephemeral agent jail.
#
# Unlike drill-injection.sh, this does NOT ask a model to claim that it refused
# an attack. It creates the same image/network shape as run-task.sh, inspects
# the Docker object before it starts, then runs concrete probes inside it.
# Every PASS below is backed by Docker inspection or a command exit status.
#
# This is the lightweight, live-node layer only. It proves absence of mounts,
# named secrets, and unauthorized paths; a future isolated SUT can add
# synthetic calendar/note/chat canaries without ever touching production data.
set -euo pipefail
cd "$(dirname "$0")/.."

RUN="boundary-access-$(date -u +%Y%m%dT%H%M%SZ)"
CONTAINER="$RUN"
NETWORK="sovereign-node_agents"
IMAGE="sovereign-node/agent:local"
PASS=0
FAIL=0

pass() { echo "  PASS  $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
phase() { echo; echo "== $1 =="; }
cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "============================================================"
echo " GETH BOUNDARY ACCESS DRILL — deterministic jail probes"
echo "============================================================"
echo "Run ID:  $RUN"
echo "Target:  ephemeral agent image on $NETWORK"
echo "Method:  Docker inspection plus commands executed inside the jail"
echo "Scope:   no model call, no production data, no real credential values"
echo
echo "A PASS means this exact probe was blocked or absent. It does not claim"
echo "that untested services or future configuration changes are safe."

phase "0/4 preflight — require the actual jail runtime"
if ! command -v docker >/dev/null 2>&1; then
  fail "docker CLI is unavailable"
  exit 1
fi
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  fail "required agents network '$NETWORK' is not running"
  echo "DRILL NOT RUN: start the node stack; this drill never fabricates a result."
  exit 1
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  fail "required jail image '$IMAGE' is not built"
  echo "DRILL NOT RUN: build the real agent image first."
  exit 1
fi
if [[ "$(docker network inspect "$NETWORK" --format '{{.Internal}}')" == "true" ]]; then
  pass "agents network is Docker-internal"
else
  fail "agents network is not Docker-internal"
  exit 1
fi

phase "1/4 launch specification — inspect the exact probe container"
# These are intentionally inert placeholders. The env NAME set mirrors the
# ephemeral runner's capability vocabulary without placing host values in this
# probe. Docker's mount list below is the authority for host-data access.
docker create -i --name "$CONTAINER" --network "$NETWORK" --entrypoint /bin/sh \
  -e AGENT_HARNESS=boundary-probe \
  -e AGENT_MODEL=none \
  -e ANTHROPIC_BASE_URL=http://litellm:4000 \
  -e ANTHROPIC_AUTH_TOKEN=drill-disposable-placeholder \
  -e OPENAI_URL=http://litellm:4000/v1 \
  -e OPENAI_API_KEY=drill-disposable-placeholder \
  -e AGENT_FORGEJO_TOKEN=drill-scoped-placeholder \
  -e NODE_CONFIG_REPO=operator/node-config \
  -e COORDINATION_REPO=operator/coordination \
  "$IMAGE" >/dev/null

MOUNTS=$(docker inspect --format '{{len .Mounts}}' "$CONTAINER")
NETMODE=$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$CONTAINER")
PRIVILEGED=$(docker inspect --format '{{.HostConfig.Privileged}}' "$CONTAINER")
if [[ "$MOUNTS" == "0" ]]; then
  pass "actual probe container has zero host or volume mounts"
else
  fail "actual probe container has $MOUNTS mount(s)"
fi
if [[ "$NETMODE" == "$NETWORK" ]]; then
  pass "actual probe container is attached only to $NETWORK"
else
  fail "probe container network mode is '$NETMODE', expected '$NETWORK'"
fi
if [[ "$PRIVILEGED" == "false" ]]; then
  pass "actual probe container is not privileged"
else
  fail "probe container is privileged"
fi

phase "2/4 in-jail probes — filesystem, environment, and network"
if docker start -ai "$CONTAINER" <<'PROBE'
set -eu
passes=0
failures=0
pass() { echo "  PASS  $1"; passes=$((passes + 1)); }
fail() { echo "  FAIL  $1"; failures=$((failures + 1)); }

for socket in /var/run/docker.sock /run/docker.sock; do
  if [ ! -S "$socket" ]; then
    pass "Docker socket absent: $socket"
  else
    fail "Docker socket present: $socket"
  fi
done

for candidate in /.env /workspace/.env /home/agent/.env; do
  if [ ! -e "$candidate" ]; then
    pass "host-secret candidate absent: $candidate"
  else
    fail "unexpected secret candidate present: $candidate"
  fi
done

# Do not print environment values: only prove that crown-jewel names did not
# cross the boundary. The disposable inference and scoped Forgejo placeholders
# are expected capability names and deliberately not on this denylist.
if env | cut -d= -f1 | grep -Eq '^(LITELLM_MASTER_KEY|EXA_API_KEY|ANTHROPIC_API_KEY|RADICALE_TOOL_PASSWORD|SEARCH_AUDIT_TOKEN|FORGEJO_TOKEN|POCKET_ID_API_KEY)$'; then
  fail "a forbidden host-secret environment name is present"
else
  pass "no forbidden host-secret environment names are present"
fi

probe_unauth_or_absent() {
  name=$1
  url=$2
  shift 2
  status=0
  code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 "$@" "$url" 2>/dev/null) || status=$?
  if [ "$status" -ne 0 ]; then
    pass "$name is not reachable from this jail"
  elif [ "$code" = 401 ] || [ "$code" = 403 ]; then
    pass "$name rejects an unauthenticated jail request (HTTP $code)"
  else
    fail "$name returned HTTP $code without a granted credential"
  fi
}

probe_unauth_or_absent "Radicale" "http://radicale:5232/"
probe_unauth_or_absent "Search Broker" "http://search-broker:8080/v1/search" -X POST -H 'Content-Type: application/json' --data '{"query":"GETH_BOUNDARY_PROBE"}'

# search-egress is deliberately not on the agents network. A successful
# connection would mean the tenant can bypass the broker's auth/audit gate.
if curl --noproxy '*' -sS -o /dev/null --connect-timeout 2 --max-time 5 http://search-egress:8081/ 2>/dev/null; then
  fail "search-egress is directly reachable, bypassing the broker"
else
  pass "search-egress is not directly reachable"
fi

# A real HTTP response from a public host proves direct egress. DNS may still
# resolve on some Docker setups; only a completed outbound request is failure.
if curl --noproxy '*' -sS -o /dev/null --connect-timeout 2 --max-time 5 https://example.com/ 2>/dev/null; then
  fail "public internet request completed from the agents network"
else
  pass "public internet request did not complete"
fi

echo "  In-jail checks passed: $passes   failed: $failures"
exit "$failures"
PROBE
then
  pass "in-jail probe process exited cleanly"
else
  fail "one or more in-jail probes failed"
fi

phase "3/4 verdict"
echo "Checks passed: $PASS   Checks failed: $FAIL"
if [[ "$FAIL" -eq 0 ]]; then
  echo "DEMO PASS — this ephemeral jail has no observed host mount, Docker socket,"
  echo "host-secret env name, unauthenticated data read, broker bypass, or direct egress."
else
  echo "DEMO FAIL — preserve this transcript and treat the failed probe as a boundary"
  echo "regression until the container, service, or capability policy is corrected."
fi
exit "$FAIL"
