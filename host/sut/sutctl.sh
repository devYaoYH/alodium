#!/usr/bin/env bash
# Isolated, deterministic SUT controller.  This is deliberately HOST-owned:
# it polls Forgejo and passes a secret-free candidate tree into a dedicated
# Docker VM.  It never executes workflow YAML from a pull request.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"
STATE="$ROOT/.task-sut"
CONFIG="$STATE/config.env"

SUT_PROFILE="${SUT_PROFILE:-geth-sut-01}"
SUT_CONTEXT="${SUT_CONTEXT:-colima-$SUT_PROFILE}"
SUT_CPUS="${SUT_CPUS:-2}"
SUT_MEMORY="${SUT_MEMORY:-4}"
SUT_DISK="${SUT_DISK:-30}"
SUT_TIMEOUT="${SUT_TIMEOUT:-240}"
SUT_EPHEMERAL="${SUT_EPHEMERAL:-1}"
[[ -f "$CONFIG" ]] && source "$CONFIG"

die() { echo "sutctl: $*" >&2; exit 1; }
note() { echo "[sut] $*"; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }
valid_sha() { [[ "$1" =~ ^[0-9a-fA-F]{7,64}$ ]]; }
valid_pr() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }

write_config() {
  mkdir -p "$STATE"
  cat > "$CONFIG" <<EOF
# Host-only SUT worker settings.  This file is gitignored.
SUT_PROFILE=$SUT_PROFILE
SUT_CONTEXT=$SUT_CONTEXT
SUT_CPUS=$SUT_CPUS
SUT_MEMORY=$SUT_MEMORY
SUT_DISK=$SUT_DISK
SUT_TIMEOUT=$SUT_TIMEOUT
SUT_EPHEMERAL=$SUT_EPHEMERAL
EOF
  chmod 600 "$CONFIG"
}

require_colima() {
  [[ "$(uname -s)" == "Darwin" ]] || die "this first implementation provisions Colima on macOS; use the same sutctl interface with the forthcoming KVM provider on Linux"
  need colima
  need docker
  need git
  need tar
}

provision_worker() {
  # No host filesystem or host port forwarding: candidate compose files and
  # containers are confined to the worker VM. Input is sent via colima ssh.
  colima start "$SUT_PROFILE" --runtime docker --cpus "$SUT_CPUS" \
    --memory "$SUT_MEMORY" --disk "$SUT_DISK" --mount=none --port-forwarder=none \
    --activate=false
}

start_worker() {
  require_colima
  # A PR controls Compose and must be treated as hostile to its worker. Fresh
  # profiles are therefore the default; a stopped profile only exists before
  # its first run. Never create a missing profile with Colima defaults.
  if [[ -f "$HOME/.colima/$SUT_PROFILE/colima.yaml" ]]; then
    colima start --profile "$SUT_PROFILE" --activate=false >/dev/null
  else
    provision_worker >/dev/null
  fi
  docker --context "$SUT_CONTEXT" info >/dev/null
}

stop_worker() {
  require_colima
  colima stop --profile "$SUT_PROFILE" >/dev/null || true
}

reset_worker() {
  [[ "$SUT_EPHEMERAL" == "1" ]] || return 0
  # `docker compose down` cannot prove a candidate did not create another
  # privileged VM-local container. Destroy the complete worker and its data
  # before a different PR gets a turn.
  colima delete --profile "$SUT_PROFILE" --data --force >/dev/null 2>&1 || true
}

doctor() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "SUT provider: KVM (not provisioned by this macOS controller yet)"
    exit 0
  fi
  need docker
  if ! command -v colima >/dev/null 2>&1; then
    cat <<'EOF'
SUT worker is not installed.
Install Colima, then run: host/sut/sutctl.sh init
The live Geth Docker context is untouched; the SUT uses its own geth-sut profile.
EOF
    exit 2
  fi
  if [[ -f "$HOME/.colima/$SUT_PROFILE/colima.yaml" ]] \
    && grep -qx 'mounts: null' "$HOME/.colima/$SUT_PROFILE/colima.yaml" \
    && grep -qx 'portForwarder: none' "$HOME/.colima/$SUT_PROFILE/colima.yaml"; then
    echo "SUT worker profile ready: $SUT_PROFILE (context $SUT_CONTEXT appears while the worker is running)"
  else
    echo "Colima is installed, but the SUT worker is not initialized. Run: host/sut/sutctl.sh init"
    exit 2
  fi
}

init() {
  require_colima
  write_config
  note "creating isolated profile $SUT_PROFILE (CPU=$SUT_CPUS, memory=${SUT_MEMORY}GiB, disk=${SUT_DISK}GiB)"
  provision_worker
  docker --context "$SUT_CONTEXT" info >/dev/null
  colima stop --profile "$SUT_PROFILE" >/dev/null
  note "ready and stopped. Run host/sut/sutctl.sh watch once, or install the watcher."
}

load_node_env() {
  [[ -f "$ROOT/.env" ]] || die "missing .env; bring the node up before enabling the SUT watcher"
  set -a; source "$ROOT/.env"; set +a
  : "${NODE_DOMAIN:?NODE_DOMAIN is required}"
  : "${NODE_CONFIG_REPO:?NODE_CONFIG_REPO is required}"
  : "${FORGEJO_TOKEN:?FORGEJO_TOKEN is required}"
}

api() {
  /usr/bin/curl -sk --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \
    -H "Authorization: token $FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"
}

candidate_checkout() {
  local pr="$1" sha="$2" checkout="$3" url
  url="https://git.${NODE_DOMAIN}/${NODE_CONFIG_REPO}.git"
  rm -rf "$checkout"
  # The token is an ephemeral git configuration value, never part of the
  # remote URL or candidate tree sent to the VM.
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
    GIT_CONFIG_VALUE_0="Authorization: token $FORGEJO_TOKEN" \
    git clone --quiet --no-checkout "$url" "$checkout"
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
    GIT_CONFIG_VALUE_0="Authorization: token $FORGEJO_TOKEN" \
    git -C "$checkout" fetch --quiet --depth 1 origin "$sha"
  git -C "$checkout" checkout --quiet --detach FETCH_HEAD
  [[ "$(git -C "$checkout" rev-parse HEAD)" == "$sha"* ]] || die "checkout head did not match PR #$pr SHA"
}

run() (
  # A subshell gives every failure path a real cleanup boundary. In particular,
  # a malformed candidate archive must not leave a reusable VM behind.
  set -euo pipefail
  local pr="${1:-}" sha="${2:-}" run checkout vmroot result log rc
  valid_pr "$pr" || die "usage: $0 run <pr-number> <head-sha>"
  valid_sha "$sha" || die "invalid head SHA"
  load_node_env
  start_worker
  run="pr-${pr}-${sha:0:12}"
  checkout="$STATE/work/$run"
  vmroot="/tmp/geth-sut/$run"
  result="$STATE/results/$run.json"
  log="$STATE/results/$run.log"
  mkdir -p "$STATE/work" "$STATE/results"
  trap 'rm -rf "$checkout"; reset_worker' EXIT

  candidate_checkout "$pr" "$sha" "$checkout"
  note "sending secret-free candidate #$pr ($sha) to $SUT_PROFILE"
  colima ssh --profile "$SUT_PROFILE" -- "rm -rf '$vmroot'; mkdir -p '$vmroot'"
  tar -C "$checkout" --exclude=.git --exclude=.env --exclude=secrets \
    --exclude=.task-dispatch --exclude=.task-sut -cf - . \
    | colima ssh --profile "$SUT_PROFILE" -- "tar -xf - -C '$vmroot'"
  # The runner comes from the trusted, merged host checkout — never the PR.
  colima ssh --profile "$SUT_PROFILE" -- "rm -f /tmp/geth-sut-vm-run.sh"
  cat "$ROOT/host/sut/sut-vm-run.sh" \
    | colima ssh --profile "$SUT_PROFILE" -- "cat > /tmp/geth-sut-vm-run.sh && chmod 700 /tmp/geth-sut-vm-run.sh"

  set +e
  colima ssh --profile "$SUT_PROFILE" -- "SUT_TIMEOUT='$SUT_TIMEOUT' /tmp/geth-sut-vm-run.sh '$vmroot'" >"$log" 2>&1
  rc=$?
  set -e
  colima ssh --profile "$SUT_PROFILE" -- "cat '$vmroot/.sut-result.json'" >"$result" 2>/dev/null || \
    printf '{"status":"error","reason":"worker did not emit a result"}\n' >"$result"
  colima ssh --profile "$SUT_PROFILE" -- "rm -rf '$vmroot'" || true
  if [[ "$rc" -eq 0 ]]; then
    note "PASS: $result"
  else
    note "FAIL: $result (log: $log)"
  fi
  exit "$rc"
)

comment_result() {
  local pr="$1" sha="$2" rc="$3" result="$STATE/results/pr-${pr}-${sha:0:12}.json" status body
  status="FAIL"
  [[ "$rc" -eq 0 ]] && status="PASS"
  body="### Isolated SUT: ${status}\n\n- Head: \`${sha}\`\n- Worker: \`${SUT_PROFILE}\`\n- Result: \`$(tr -d '\n' < "$result" 2>/dev/null || echo unavailable)\`\n\nThis result was produced by the host-owned SUT controller in an isolated Docker VM. It is evidence, not merge authorization."
  api -X POST "https://git.${NODE_DOMAIN}/api/v1/repos/${NODE_CONFIG_REPO}/issues/${pr}/comments" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"body":sys.stdin.read()}))' <<<"$body")" >/dev/null
}

watch() {
  load_node_env
  mkdir -p "$STATE/seen"
  local lock="$STATE/watch.lock" rows pr sha actor stamp rc
  if ! mkdir "$lock" 2>/dev/null; then note "watch already running"; return 0; fi
  trap 'rmdir "$lock" 2>/dev/null || true' RETURN
  rows=$(api "https://git.${NODE_DOMAIN}/api/v1/repos/${NODE_CONFIG_REPO}/pulls?state=open&limit=50" \
    | python3 -c 'import json,sys
for p in json.load(sys.stdin):
 h=p.get("head") or {}; u=(h.get("user") or {}).get("login", "")
 if u == "agent-dev": print(p["number"], h.get("sha", ""))')
  while read -r pr sha; do
    [[ -n "${pr:-}" ]] || continue
    valid_pr "$pr" && valid_sha "$sha" || { note "skip malformed PR record"; continue; }
    stamp="$STATE/seen/pr-${pr}-${sha:0:12}"
    [[ -e "$stamp" ]] && continue
    note "testing Forgejo PR #$pr at ${sha:0:12}"
    set +e; run "$pr" "$sha"; rc=$?; set -e
    comment_result "$pr" "$sha" "$rc"
    : > "$stamp"
  done <<<"$rows"
}

case "${1:-}" in
  doctor) doctor ;;
  init) init ;;
  start) start_worker ;;
  stop) stop_worker ;;
  run) shift; run "$@" ;;
  watch) watch ;;
  *) cat <<EOF
usage: $0 doctor|init|start|stop|run <pr-number> <head-sha>|watch
EOF
     exit 2 ;;
esac
