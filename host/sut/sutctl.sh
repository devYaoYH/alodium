#!/usr/bin/env bash
# Isolated, deterministic SUT controller.  This is deliberately HOST-owned:
# it polls Forgejo and passes a secret-free candidate tree into a dedicated
# Docker VM.  It never executes workflow YAML from a pull request.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"
STATE="$ROOT/.task-sut"
CONFIG="$STATE/config.env"
# launchd starts jobs with a minimal PATH. The plist written at install time
# only listed Homebrew, so every scheduled run died on "missing required
# command: docker" while the same command worked from a terminal. Find the
# usual Homebrew and Docker Desktop locations whatever PATH we were given.
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin"

SUT_PROFILE="${SUT_PROFILE:-geth-sut-01}"
SUT_CONTEXT="${SUT_CONTEXT:-colima-$SUT_PROFILE}"
# The full stack starves on 2 CPUs (redash workers miss their boot timeout).
# CPU is time-shared with the host, not reserved; memory is.
SUT_CPUS="${SUT_CPUS:-4}"
SUT_MEMORY="${SUT_MEMORY:-4}"
SUT_DISK="${SUT_DISK:-30}"
SUT_TIMEOUT="${SUT_TIMEOUT:-240}"
SUT_EPHEMERAL="${SUT_EPHEMERAL:-1}"
SUT_POOL_SIZE="${SUT_POOL_SIZE:-1}"
SUT_LABEL="${SUT_LABEL:-requires-sut}"
[[ -f "$CONFIG" ]] && source "$CONFIG"
[[ "$SUT_POOL_SIZE" =~ ^[1-9]$ ]] || { echo "sutctl: SUT_POOL_SIZE must be 1-9" >&2; exit 1; }

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
  # profiles are therefore the default. A profile left behind by a run that
  # was killed before its cleanup may hold that run's containers, so a
  # single-use worker is always recreated rather than restarted. Never create
  # a missing profile with Colima defaults.
  if [[ "$SUT_EPHEMERAL" == "1" ]]; then
    reset_worker
    provision_worker >/dev/null
  elif [[ -f "$HOME/.colima/$SUT_PROFILE/colima.yaml" ]]; then
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

vm_exec() {
  # Colima passes arguments after `--` directly to exec, rather than through a
  # shell. Always invoke bash explicitly; otherwise a compound command such as
  # `rm ...; mkdir ...` is looked up as a literal binary name.
  colima ssh --profile "$SUT_PROFILE" -- bash -lc "$1"
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
  elif [[ -f "$CONFIG" ]] && [[ "$SUT_EPHEMERAL" == "1" ]]; then
    # A successful test destroys its whole Colima profile. The retained
    # host-only config proves initialization happened; the next run will
    # recreate the profile with the fixed isolation flags in provision_worker.
    echo "SUT worker configured for single-use execution: $SUT_PROFILE will be recreated on the next PR"
  else
    echo "Colima is installed, but the SUT worker is not initialized. Run: host/sut/sutctl.sh init"
    exit 2
  fi
  # `install-launchd.sh` calls doctor, so validate the capability that makes a
  # test result useful before a periodic job can be enabled. A repository-only
  # node-ops token is intentionally insufficient: Forgejo separates issue
  # read/write scopes from repository scopes.
  load_node_env
  api "https://git.${NODE_DOMAIN}/api/v1/repos/${NODE_CONFIG_REPO}" >/dev/null
  api "https://git.${NODE_DOMAIN}/api/v1/repos/${NODE_CONFIG_REPO}/pulls?state=open&limit=1" >/dev/null
  echo "SUT Forgejo prerequisites ready: dedicated token can read repository and PR metadata"
  if api "$(repo_api)/labels?limit=50" | grep -q "\"name\":\"$SUT_LABEL\""; then
    echo "Request label ready: '$SUT_LABEL' on $NODE_CONFIG_REPO (pool of $SUT_POOL_SIZE)"
  else
    echo "Request label '$SUT_LABEL' is missing. Run: host/sut/sutctl.sh label"
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
  # The normal watcher reads the node-local .env.  Keeping environment values
  # when the file is absent also permits an operator to reproduce a result
  # from a clean checkout without copying any secret file into it.
  if [[ -f "$ROOT/.env" ]]; then
    set -a; source "$ROOT/.env"; set +a
  fi
  : "${NODE_DOMAIN:?NODE_DOMAIN is required}"
  : "${NODE_CONFIG_REPO:?NODE_CONFIG_REPO is required}"
  # The watcher needs only repository reads plus PR read/comment access. Its
  # dedicated token keeps it separate from the broader node-operations token;
  # never fall back, because that fallback may test successfully but lose the
  # evidence comment that gates the operator's decision.
  : "${SUT_FORGEJO_TOKEN:?SUT_FORGEJO_TOKEN is required}"
}

api() {
  /usr/bin/curl -fskS --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \
    -H "Authorization: token $SUT_FORGEJO_TOKEN" -H "Content-Type: application/json" "$@"
}

candidate_checkout() {
  local pr="$1" sha="$2" checkout="$3" url
  url="https://git.${NODE_DOMAIN}/${NODE_CONFIG_REPO}.git"
  rm -rf "$checkout"
  # The token is an ephemeral git configuration value, never part of the
  # remote URL or candidate tree sent to the VM.
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
    GIT_CONFIG_VALUE_0="Authorization: token $SUT_FORGEJO_TOKEN" \
    git clone --quiet --no-checkout "$url" "$checkout"
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
    GIT_CONFIG_VALUE_0="Authorization: token $SUT_FORGEJO_TOKEN" \
    git -C "$checkout" fetch --quiet --depth 1 origin "$sha"
  git -C "$checkout" checkout --quiet --detach FETCH_HEAD
  [[ "$(git -C "$checkout" rev-parse HEAD)" == "$sha"* ]] || die "checkout head did not match PR #$pr SHA"
}

checkout_build_sources() {
  # Images built from a mirrored repo come from the candidate's own manifests
  # ([build] image, repo, ref, args, patch): the same source of truth deploy's
  # build-mirrored.sh reads, so the gate cannot drift from deploy. The host
  # clones only repositories on the reviewed allowlist. A PR can move a ref
  # or change build args, but it cannot make the host fetch another repository.
  local checkout="$1" destination="$2" allow_file="$ROOT/host/sut/sources.toml"
  [[ -f "$allow_file" ]] || die "missing trusted SUT repository allowlist: $allow_file"
  mkdir -p "$destination"
  python3 - "$allow_file" "$checkout" "$destination/build-sources.json" <<'PY'
import json, pathlib, re, string, sys, tomllib
allow_file, checkout, out = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
allowed = {r["repo"] for r in tomllib.loads(pathlib.Path(allow_file).read_text()).get("repo", [])}
safe = re.compile(r"[A-Za-z0-9._/:-]+")
items = []
for path in sorted((checkout / "manifest").glob("*.toml")):
    if path.name.endswith(".example.toml"):
        continue
    manifest = tomllib.loads(path.read_text())
    build = manifest.get("build")
    if not build:
        continue
    image = manifest.get("app", {}).get("image", "")
    repo, ref, patch = build.get("repo", ""), build.get("ref", ""), build.get("patch", "")
    if repo not in allowed:
        raise SystemExit(f"{path.name}: [build].repo {repo!r} is not in host/sut/sources.toml")
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise SystemExit(f"{path.name}: [build].ref must be a 40-character commit SHA")
    if not safe.fullmatch(image):
        raise SystemExit(f"{path.name}: unsafe [app].image")
    if patch and (not patch.endswith(".patch") or patch.startswith("/")
                  or ".." in patch.split("/") or not safe.fullmatch(patch)):
        raise SystemExit(f"{path.name}: [build].patch must be a relative .patch path")
    # Build args see the worker's synthetic domain, never the node's.
    args = {k: string.Template(str(v)).safe_substitute(NODE_DOMAIN="sut.invalid")
            for k, v in build.get("args", {}).items()}
    items.append({"name": path.stem, "image": image, "repo": repo, "ref": ref,
                  "patch": patch, "args": args})
out.write_text(json.dumps(items, sort_keys=True))
PY
  while IFS=$'\t' read -r name repo ref; do
    [[ -n "$name" ]] || continue
    local target="$destination/$name"
    rm -rf "$target"
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
      GIT_CONFIG_VALUE_0="Authorization: token $SUT_FORGEJO_TOKEN" \
      git clone --quiet --no-checkout "https://git.${NODE_DOMAIN}/${repo}.git" "$target"
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
      GIT_CONFIG_VALUE_0="Authorization: token $SUT_FORGEJO_TOKEN" \
      git -C "$target" fetch --quiet --depth 1 origin "$ref"
    git -C "$target" checkout --quiet --detach FETCH_HEAD
  done < <(python3 - "$destination/build-sources.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1])):
    print("\t".join((item["name"], item["repo"], item["ref"])))
PY
)
}

run() (
  # A subshell gives every failure path a real cleanup boundary. In particular,
  # a malformed candidate archive must not leave a reusable VM behind.
  set -euo pipefail
  local pr="${1:-}" sha="${2:-}" run checkout dependencies vmroot result log worker_log rc
  valid_pr "$pr" || die "usage: $0 run <pr-number> <head-sha>"
  valid_sha "$sha" || die "invalid head SHA"
  load_node_env
  run="pr-${pr}-${sha:0:12}"
  checkout="$STATE/work/$run"
  dependencies="$STATE/dependencies/$run"
  vmroot="/tmp/geth-sut/$run"
  result="$STATE/results/$run.json"
  log="$STATE/results/$run.log"
  worker_log="$STATE/results/$run.worker.log"
  mkdir -p "$STATE/work" "$STATE/dependencies" "$STATE/results"
  trap 'rm -rf "$checkout" "$dependencies"; reset_worker' EXIT

  # Capture setup failures in the controller log. The watcher turns any path
  # that still exits before a worker result into structured PR evidence.
  if ! start_worker >>"$log" 2>&1; then
    exit 1
  fi

  candidate_checkout "$pr" "$sha" "$checkout"
  checkout_build_sources "$checkout" "$dependencies"
  note "sending secret-free candidate #$pr ($sha) to $SUT_PROFILE"
  vm_exec "rm -rf '$vmroot'; mkdir -p '$vmroot'"
  COPYFILE_DISABLE=1 tar -C "$checkout" --exclude=.git --exclude=.env --exclude=secrets \
    --exclude=.task-dispatch --exclude=.task-sut -cf - . 2>>"$log" \
    | colima ssh --profile "$SUT_PROFILE" -- bash -lc "tar --warning=no-unknown-keyword -xf - -C '$vmroot'"
  vm_exec "mkdir -p '$vmroot/dependencies'"
  COPYFILE_DISABLE=1 tar -C "$dependencies" --exclude=.git --exclude='*/.git' -cf - . 2>>"$log" \
    | colima ssh --profile "$SUT_PROFILE" -- bash -lc "tar --warning=no-unknown-keyword -xf - -C '$vmroot/dependencies'"
  # The runner comes from the trusted, merged host checkout — never the PR.
  vm_exec "rm -f /tmp/geth-sut-vm-run.sh"
  cat "$ROOT/host/sut/sut-vm-run.sh" \
    | colima ssh --profile "$SUT_PROFILE" -- bash -lc "cat > /tmp/geth-sut-vm-run.sh && chmod 700 /tmp/geth-sut-vm-run.sh"

  set +e
  colima ssh --profile "$SUT_PROFILE" -- bash -lc "SUT_TIMEOUT='$SUT_TIMEOUT' /tmp/geth-sut-vm-run.sh '$vmroot'" >>"$log" 2>&1
  rc=$?
  set -e
  colima ssh --profile "$SUT_PROFILE" -- bash -lc "cat '$vmroot/.sut-result.json'" >"$result" 2>/dev/null || \
    printf '{"status":"error","reason":"worker did not emit a result"}\n' >"$result"
  # The VM root is destroyed after this run. Pull its full Compose/test output
  # first; the compact JSON alone is not enough to repair a red PR.
  colima ssh --profile "$SUT_PROFILE" -- bash -lc "cat '$vmroot/.sut-worker.log'" >"$worker_log" 2>/dev/null || true
  vm_exec "rm -rf '$vmroot'" || true
  if [[ "$rc" -eq 0 ]]; then
    note "PASS: $result"
  else
    note "FAIL: $result (log: $log)"
  fi
  exit "$rc"
)


# --- Label-driven queue ------------------------------------------------------
# The operator asks for a full-stack test by adding the `requires-sut` label to
# a node-config PR. Each head pushed while the label stays on gets one test;
# removing and re-adding the label asks for a fresh run of the same head. Only
# the operator's label counts: agents hold write on node-config and can label
# their own PRs, but a worker VM is host capacity only the operator allocates
# (the same rule dispatch-run.sh applies to difficulty labels).
#
# Capacity is a pool of SUT_POOL_SIZE single-use workers (default 1). Requests
# wait FIFO, ordered by when the operator labeled them. A pass stays alive until
# the queue drains, re-reading Forgejo between runs, and launchd never starts a
# second instance of a job that is still running.

repo_api() { echo "https://git.${NODE_DOMAIN}/api/v1/repos/${NODE_CONFIG_REPO}"; }

slot_profile() {  # the Colima profile owned by pool slot <n>
  if [[ "$1" -eq 1 ]]; then echo "$SUT_PROFILE"; else printf '%s-%02d\n' "${SUT_PROFILE%-*}" "$1"; fi
}

take_lock() {  # atomic mkdir; a lock whose holder process is gone is stale
  local dir="$1" pid age
  if mkdir "$dir" 2>/dev/null; then echo "$$" >"$dir/pid"; return 0; fi
  pid="$(cat "$dir/pid" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then return 1; fi
  # A lock without a pid may belong to a holder between its mkdir and write.
  # Anything else was killed mid-run (sleep, reboot, launchd) and, left alone,
  # would block every later pass: the old watcher went silent that way.
  age=$(( $(date +%s) - $(stat -f %m "$dir" 2>/dev/null || stat -c %Y "$dir") ))
  if [[ -z "$pid" && "$age" -lt 60 ]]; then return 1; fi
  note "removing stale lock $dir (holder ${pid:-unknown} is gone)"
  rm -rf "$dir"
  mkdir "$dir" 2>/dev/null || return 1
  echo "$$" >"$dir/pid"
}

slot_busy() {  # slot <n> is running a live job
  local pid
  pid="$(cat "$STATE/slots/$1/pid" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

say() {  # say <pr> <markdown>
  api -X POST "$(repo_api)/issues/$1/comments" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"body":sys.argv[1]}))' "$2")" >/dev/null
}

ensure_label() {
  local labels
  labels="$(api "$(repo_api)/labels?limit=50")" || die "cannot list labels on $NODE_CONFIG_REPO"
  if python3 -c 'import json,sys; sys.exit(0 if any(l["name"]==sys.argv[1] for l in json.load(sys.stdin)) else 1)' \
       "$SUT_LABEL" <<<"$labels"; then
    return 0
  fi
  api -X POST "$(repo_api)/labels" -d "$(python3 -c 'import json,sys; print(json.dumps({"name":sys.argv[1],"color":"#1d76db","description":"Operator: run the isolated full-stack SUT test on each head of this PR"}))' "$SUT_LABEL")" >/dev/null
  note "created label '$SUT_LABEL' on $NODE_CONFIG_REPO"
}

label_request() {  # prints the operator's label time for PR <n>, "refused:<who>", or nothing
  local pr="$1" page=1 body
  while :; do
    body="$(api "$(repo_api)/issues/$pr/timeline?limit=50&page=$page")" || return 1
    printf '%s\n' "$body"
    [[ "$(python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' <<<"$body")" -ge 50 ]] || break
    page=$((page + 1))
  done | python3 -c '
import json, sys
label, operator = sys.argv[1], sys.argv[2]
events = []
for line in sys.stdin:
    if line.strip():
        events += [e for e in json.loads(line)
                   if e.get("type") == "label" and (e.get("label") or {}).get("name") == label]
events.sort(key=lambda e: e.get("created_at", ""))
if events and events[-1].get("body") == "1":
    who = (events[-1].get("user") or {}).get("login", "")
    print(events[-1]["created_at"] if who == operator else "refused:" + who)' "$SUT_LABEL" "$OPERATOR_LOGIN"
}

queue() {  # "<labeled-at> <pr> <sha>" per operator-requested head, oldest first
  local pulls pr sha at stamp
  pulls="$(api "$(repo_api)/pulls?state=open&limit=50")" || { note "Forgejo unreachable; skipping this pass"; return 1; }
  while read -r pr sha; do
    [[ -n "${pr:-}" ]] || continue
    valid_pr "$pr" && valid_sha "$sha" || { note "skip malformed PR record"; continue; }
    at="$(label_request "$pr")" || continue
    case "$at" in
      "") ;;
      refused:*)
        # Tell the requester once per head, then stay quiet.
        stamp="$STATE/refused/pr-${pr}-${sha:0:12}"
        if [[ ! -e "$stamp" ]]; then
          say "$pr" "$(printf '### Isolated SUT: not queued\n\n`%s` was added by `%s`. Only the operator'"'"'s label allocates a test worker; the operator can re-add it.' "$SUT_LABEL" "${at#refused:}")" || true
          : >"$stamp"
        fi ;;
      *) echo "$at $pr $sha" ;;
    esac
  done < <(python3 -c '
import json, sys
for p in json.load(sys.stdin):
    if any(l.get("name") == sys.argv[1] for l in p.get("labels") or []):
        print(p["number"], (p.get("head") or {}).get("sha", ""))' "$SUT_LABEL" <<<"$pulls") | sort
}

request_key() { echo "pr-$2-${3:0:12}-$(tr -cd 0-9 <<<"$1")"; }  # <labeled-at> <pr> <sha>

comment_result() {  # comment_result <pr> <sha> <rc> <worker> <seconds>
  local pr="$1" sha="$2" rc="$3" worker="$4" secs="$5" run status reason logtail="" fence='```'
  run="pr-${pr}-${sha:0:12}"
  status="FAIL"; [[ "$rc" -eq 0 ]] && status="PASS"
  reason="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("reason",""))' "$STATE/results/$run.json" 2>/dev/null || echo unavailable)"
  if [[ "$rc" -ne 0 ]]; then
    # The worker only ever sees synthetic config, so its log tail is safe to
    # attach; the host keeps the complete log.
    logtail="$(tail -n 40 "$STATE/results/$run.worker.log" 2>/dev/null | cut -c1-300 || true)"
    [[ -n "$logtail" ]] || logtail="$(tail -n 20 "$STATE/results/$run.log" 2>/dev/null | cut -c1-300 || true)"
    logtail="

<details><summary>Last lines of the worker log</summary>

$fence
${logtail//$fence/\'\'\'}
$fence
</details>"
  fi
  say "$pr" "### Isolated SUT: ${status}

- Head: \`${sha}\`
- Reason: ${reason}
- Worker: \`${worker}\`, $((secs / 60))m$((secs % 60))s
- Host logs: \`.task-sut/results/${run}.{log,worker.log,json}\`${logtail}

Produced by the host-owned SUT controller in a single-use Docker VM. It is evidence, not merge authorization."
}

test_request() {  # one run in pool slot <n>, then its evidence comment
  local slot="$1" pr="$2" sha="$3" rc start result
  SUT_PROFILE="$(slot_profile "$slot")"
  SUT_CONTEXT="colima-$SUT_PROFILE"
  start=$(date +%s)
  set +e; run "$pr" "$sha"; rc=$?; set -e
  result="$STATE/results/pr-${pr}-${sha:0:12}.json"
  if [[ ! -s "$result" ]]; then
    python3 - "$result" "$rc" <<'PY'
import json, sys, time
open(sys.argv[1], "w").write(json.dumps({
  "status": "error",
  "reason": f"SUT controller exited with status {sys.argv[2]} before a worker result; inspect the controller log",
  "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}) + "\n")
PY
  fi
  comment_result "$pr" "$sha" "$rc" "$SUT_PROFILE" "$(( $(date +%s) - start ))" || note "could not post the result on #$pr"
  return "$rc"
}

free_slot() {  # claims and prints a free slot number, or fails
  local n
  for ((n = 1; n <= SUT_POOL_SIZE; n++)); do
    if take_lock "$STATE/slots/$n"; then echo "$n"; return 0; fi
  done
  return 1
}

dispatch() {
  load_node_env
  mkdir -p "$STATE/seen" "$STATE/queued" "$STATE/refused" "$STATE/results" "$STATE/slots"
  take_lock "$STATE/dispatch.lock" || { note "another dispatch pass is running"; return 0; }
  trap 'rm -rf "$STATE/dispatch.lock"' EXIT
  local rows at pr sha key slot waiting busy n
  while :; do
    rows="$(queue)" || rows=""
    waiting=0
    while read -r at pr sha; do
      [[ -n "${pr:-}" ]] || continue
      key="$(request_key "$at" "$pr" "$sha")"
      [[ -e "$STATE/seen/$key" ]] && continue
      if slot="$(free_slot)"; then
        : >"$STATE/seen/$key"
        note "testing PR #$pr at ${sha:0:12} in slot $slot ($(slot_profile "$slot"))"
        say "$pr" "### Isolated SUT: started

Head \`${sha}\` is running on \`$(slot_profile "$slot")\` (pool of ${SUT_POOL_SIZE}). A full bring-up takes several minutes; the result follows as a new comment." || true
        # The job owns the slot: its pid replaces ours, and it frees the slot
        # when done. A job killed mid-run leaves a dead pid, which take_lock
        # treats as stale.
        ( test_request "$slot" "$pr" "$sha"; rm -rf "$STATE/slots/$slot" ) &
        echo "$!" >"$STATE/slots/$slot/pid"
      else
        waiting=$((waiting + 1))
        if [[ ! -e "$STATE/queued/$key" ]]; then
          say "$pr" "### Isolated SUT: queued

Head \`${sha}\` is number ${waiting} in line for the pool of ${SUT_POOL_SIZE} worker(s)." || true
          : >"$STATE/queued/$key"
        fi
      fi
    done <<<"$rows"
    busy=0
    for ((n = 1; n <= SUT_POOL_SIZE; n++)); do if slot_busy "$n"; then busy=$((busy + 1)); fi; done
    [[ "$busy" -eq 0 && "$waiting" -eq 0 ]] && break
    sleep 30
  done
  wait
}

test_pr() {  # run and report the current head of one PR now, label or not
  local pr="${1:-}" sha slot
  valid_pr "$pr" || die "usage: $0 test <pr-number>"
  load_node_env
  mkdir -p "$STATE/results" "$STATE/slots"
  sha="$(api "$(repo_api)/pulls/$pr" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("head") or {}).get("sha",""))')"
  valid_sha "$sha" || die "cannot resolve the head of PR #$pr"
  slot="$(free_slot)" || die "every pool slot is busy; add the '$SUT_LABEL' label to queue it instead"
  trap 'rm -rf "$STATE/slots/$slot"' EXIT
  test_request "$slot" "$pr" "$sha"
}

status() {
  local n job
  echo "pool: $SUT_POOL_SIZE worker(s), label '$SUT_LABEL'"
  for ((n = 1; n <= SUT_POOL_SIZE; n++)); do
    if slot_busy "$n"; then echo "  slot $n ($(slot_profile "$n")): busy"; else echo "  slot $n ($(slot_profile "$n")): idle"; fi
  done
  echo "recent results:"
  for job in $(ls -t "$STATE/results/"*.json 2>/dev/null | head -5); do
    echo "  $(basename "$job" .json): $(tr -d '\n' <"$job")"
  done
}

case "${1:-}" in
  doctor) doctor ;;
  init) init ;;
  start) start_worker ;;
  stop) stop_worker ;;
  run) shift; run "$@" ;;
  test) shift; test_pr "$@" ;;
  dispatch) dispatch ;;
  label) load_node_env; ensure_label ;;
  status) status ;;
  *) cat <<EOF
usage: $0 doctor|init|status|dispatch|label|test <pr>|run <pr> <sha>|start|stop
EOF
     exit 2 ;;
esac
