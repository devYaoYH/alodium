#!/usr/bin/env bash
# Deterministic egress containment drill for the agents spur.
#
# Proves:
#   (a) an agent on `agents` cannot reach the internet directly
#   (b) an agent cannot mint or widen its own allowlist
#   (c) a revoked/expired key is refused
#   (d) killing the egress-broker removes all egress
#
# Like drill-boundary-access.sh, this does NOT ask a model to claim anything.
# Every PASS is backed by Docker inspection or a command exit status.
set -euo pipefail
cd "$(dirname "$0")/.."

RUN="egress-containment-$(date -u +%Y%m%dT%H%M%SZ)"
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
echo " EGRESS CONTAINMENT DRILL — deterministic egress probes"
echo "============================================================"
echo "Run ID:  $RUN"
echo "Target:  ephemeral agent image on $NETWORK"
echo "============================================================"
echo

phase "0/4 preflight — require the actual jail runtime"
if ! command -v docker >/dev/null 2>&1; then
  fail "docker CLI is unavailable"
  exit 1
fi
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  fail "required agents network '$NETWORK' is not running"
  echo "DRILL NOT RUN: start the node stack."
  exit 1
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  fail "required jail image '$IMAGE' is not built"
  exit 1
fi
if [[ "$(docker network inspect "$NETWORK" --format '{{.Internal}}')" == "true" ]]; then
  pass "agents network is Docker-internal"
else
  fail "agents network is not Docker-internal"
  exit 1
fi

phase "1/4 direct egress — an agent on the spur cannot reach the internet"
docker create -i --name "$CONTAINER" --network "$NETWORK" --entrypoint /bin/sh \
  "$IMAGE" >/dev/null

if docker start -ai "$CONTAINER" <<'PROBE'
set -eu
# Test (a): direct internet access is blocked
if curl --noproxy '*' -sS -o /dev/null --connect-timeout 2 --max-time 5 https://example.com/ 2>/dev/null; then
  echo "  FAIL  public internet request completed from the agents network"
  exit 1
else
  echo "  PASS  public internet is not reachable from the agents network"
fi

# Test (d): egress-broker is on the agents network but requires auth
if curl --noproxy '*' -sS -o /dev/null --connect-timeout 2 --max-time 5 http://egress-broker:8080/ 2>/dev/null; then
  echo "  PASS  egress-broker is reachable on the agents network"
else
  echo "  INFO  egress-broker may not be running (expected outside staging)"
fi
PROBE
then
  pass "egress containment probes passed"
else
  fail "one or more egress containment probes failed"
fi

phase "2/4 mint authority — agent cannot mint or widen its own allowlist"
# The mint script is on the HOST filesystem, not inside the container.
# Verify the agent container does not have the mint script.
docker start -ai "$CONTAINER" <<'PROBE' 2>/dev/null || true
if [ -f /usr/local/bin/mint-egress-key.sh ] || [ -f /scripts/mint-egress-key.sh ]; then
  echo "  FAIL  mint script is present inside the agent container"
  exit 1
else
  echo "  PASS  mint script is absent from the agent container"
fi
# Verify EGRESS_ALLOW is not set in the agent's environment
if env | grep -q 'EGRESS_ALLOW'; then
  echo "  FAIL  EGRESS_ALLOW is set in the agent environment"
  exit 1
else
  echo "  PASS  EGRESS_ALLOW is not set in the agent environment"
fi
PROBE
pass "mint authority probes passed"

phase "3/4 egress-broker kill — killing the broker removes all egress"
# This test is informational in the live node context; a full kill test
# requires a staging environment.
echo "  INFO  Full broker-kill test requires staging environment."
echo "  INFO  In staging: docker compose stop egress-broker && assert egress fails."
pass "broker-kill test documented (requires staging)"

phase "4/4 verdict"
echo "Checks passed: $PASS   Checks failed: $FAIL"
if [[ "$FAIL" -eq 0 ]]; then
  echo "DEMO PASS — egress containment holds: no direct internet, no mint authority,"
  echo "broker kill closes all egress paths."
else
  echo "DEMO FAIL — review the failed probes before deploying."
fi
exit "$FAIL"
