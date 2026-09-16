#!/usr/bin/env bash
# Agent-jail image smoke test — does the jail actually start?
#
#   ./scripts/test-jail-image.sh            # build, then test
#   JAIL_IMAGE=sovereign-node/agent:local ./scripts/test-jail-image.sh --no-build
#
# Why this exists: verify-config.sh is a pure TEXT check — it never builds or
# runs anything, so a config that parses perfectly can still produce an image
# whose harness hangs on startup. That is not hypothetical: shipping
# ~/forge/.forge.toml via a root-owned `RUN mkdir` left forge unable to write
# its own config dir, and forge does not fail on that — it BLOCKS FOREVER with
# no output. Every jail session and every dispatched issue-work container hung
# until its timeout, and no text-level gate could have seen it.
#
# So the load-bearing assertion here is behavioral: boot the real harness in
# the real image and require it to REACH THE PROVIDER. We point it at a dead
# port, so "connection refused" is the PASS signal — it proves forge got all
# the way through config load, credential migration and model dispatch. A hang
# is the failure we are hunting.
#
# Needs docker. Not part of verify-config.sh (which must stay offline and
# daemon-free so it can run inside the jail itself).
set -uo pipefail
cd "$(dirname "$0")/.."

IMAGE="${JAIL_IMAGE:-sovereign-node/agent:test}"
BUILD=1
[[ "${1:-}" == "--no-build" ]] && BUILD=0

FAIL=0
note() { printf '  %s\n' "$1"; }
sec()  { printf '\n== %s ==\n' "$1"; }

if ! docker version >/dev/null 2>&1; then
  echo "test-jail-image: SKIP — no reachable docker daemon"
  exit 0
fi

if [[ "$BUILD" -eq 1 ]]; then
  sec "build $IMAGE"
  if docker build -t "$IMAGE" ./agent >/tmp/tj_build.log 2>&1; then
    note "OK: image builds"
  else
    note "FAIL: build errored —"; tail -20 /tmp/tj_build.log | sed 's/^/    /'
    echo; echo "test-jail-image: FAIL"; exit 1
  fi
fi

# --- 1. The agent user owns its whole home ---------------------------------
# A `RUN mkdir` before `USER agent` silently creates root-owned directories,
# and COPY --chown fixes the FILE while leaving the parent DIR root-owned.
# Harnesses keep state in $HOME (forge: ~/forge, claude: ~/.claude), so any
# unwritable path under it is a startup failure waiting to happen.
sec "home writability"
UNWRITABLE=$(docker run --rm --entrypoint sh "$IMAGE" -c \
  'find "$HOME" -maxdepth 2 \( ! -writable -o ! -user agent \) -print 2>/dev/null' 2>/dev/null)
if [[ -z "$UNWRITABLE" ]]; then
  note "OK: every path under /home/agent is agent-owned and writable"
else
  note "FAIL: agent cannot write these paths in its own \$HOME —"
  printf '%s\n' "$UNWRITABLE" | sed 's/^/    /'
  note "(a RUN mkdir before USER agent? add a matching chown)"
  FAIL=1
fi

# --- 2. The shipped forge config is valid TOML -----------------------------
sec "forge config parses"
if docker run --rm --entrypoint python3 "$IMAGE" -c \
     'import tomllib;tomllib.load(open("/home/agent/forge/.forge.toml","rb"))' 2>/tmp/tj_toml.log
then
  note "OK: ~/forge/.forge.toml is valid TOML"
else
  note "FAIL: ~/forge/.forge.toml does not parse —"; sed 's/^/    /' /tmp/tj_toml.log | tail -5
  FAIL=1
fi

# --- 2b. ...and forge ACTUALLY RECOGNISES every key in it ------------------
# Valid TOML is not enough. Forge hard-fails on malformed syntax but silently
# ignores unknown keys, so a misspelled key — or a real key nested under a
# table forge doesn't use — configures NOTHING and says nothing. That is how
# `[session] max_requests_per_turn = 500` shipped: perfect TOML, zero effect,
# the cap it was meant to raise still at forge's default of 100.
#
# The oracle: forge DOES type-check keys it knows. So feed each key back with
# a deliberately wrong-typed value — a recognised key must be REJECTED. If
# forge shrugs, that key is dead config. This turns "is it applied?" into an
# offline, provider-free check.
#
# The wrong-typed value is an array, `[1]`, for every key. Forge coerces
# scalars INTO string keys (`services_url = 99999` and `= true` are both
# accepted), so flipping a string to a number would call a live string key
# dead. An array is rejected by string and integer keys alike, while a
# misspelled key still swallows it silently — verified against forge 2.13.18.
sec "forge recognises every configured key"
# One probe per key: "<label><TAB><a .forge.toml body with that key mistyped>".
# Built here on the host so the container side stays a plain loop.
PROBES=$(python3 - agent/.forge.toml <<'PY'
import sys, tomllib

def walk(d, path=()):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from walk(v, path + (k,))
        else:
            # An array fits no scalar key: a key forge knows must reject it.
            yield path, k, '[1]'

with open(sys.argv[1], 'rb') as f:
    cfg = tomllib.load(f)

for path, k, bad in walk(cfg):
    label = '.'.join(path + (k,))
    header = '[' + '.'.join(path) + ']\\n' if path else ''
    print(f'{label}\t{header}{k} = {bad}')
PY
)
if [[ -z "$PROBES" ]]; then
  note "SKIP: agent/.forge.toml sets no keys"
else
  KEYFAIL=0
  while IFS=$'\t' read -r label body; do
    [[ -n "$label" ]] || continue
    OUT=$(docker run --rm --entrypoint sh \
            -e PROBE_BODY="$body" \
            -e OPENAI_URL=http://127.0.0.1:9/v1 -e OPENAI_API_KEY=dummy \
            -e FORGE_SESSION__PROVIDER_ID=openai_compatible \
            -e FORGE_SESSION__MODEL_ID=deepseek-flash \
            "$IMAGE" -c '
              d=$(mktemp -d) && mkdir -p "$d/forge" || exit 9
              printf "%b\n" "$PROBE_BODY" > "$d/forge/.forge.toml"
              cd /tmp && HOME="$d" timeout 40 forge -p ping 2>&1' 2>&1)
    if printf '%s' "$OUT" | grep -qa "invalid type\|Config error"; then
      note "  live: $label"
    else
      note "  DEAD: $label — forge accepted a wrong-typed value, so it ignores this key"
      KEYFAIL=1
    fi
  done <<< "$PROBES"
  if [[ "$KEYFAIL" -eq 0 ]]; then
    note "OK: forge type-checks every key, so every key actually applies"
  else
    note "FAIL: dead config above — wrong table or misspelled."
    note "See https://forgecode.dev/docs/forgecode-config/ (keys are TOP-LEVEL)."
    FAIL=1
  fi
fi

# --- 3. THE REGRESSION TEST: forge boots and reaches the provider ----------
# 127.0.0.1:9 (discard) refuses instantly, so a healthy forge finishes in a
# couple of seconds with a connect error. Exit 124 = it never got there.
sec "forge harness boots (does not hang)"
docker run --rm --entrypoint sh \
  -e OPENAI_URL=http://127.0.0.1:9/v1 \
  -e OPENAI_API_KEY=dummy \
  -e FORGE_SESSION__PROVIDER_ID=openai_compatible \
  -e FORGE_SESSION__MODEL_ID=deepseek-flash \
  "$IMAGE" -c 'cd /tmp && timeout 60 forge -p "ping" 2>&1' \
  >/tmp/tj_forge.log 2>&1
RC=$?

if [[ "$RC" -eq 124 ]]; then
  note "FAIL: forge HUNG (no provider attempt within 60s) — the jail is dead on arrival."
  note "Almost always \$HOME state it cannot write; check section 1 above."
  FAIL=1
elif grep -qa "Failed to fetch models\|Connection refused\|tcp connect error" /tmp/tj_forge.log; then
  note "OK: forge loaded config and reached the provider (connect refused, as expected)"
else
  note "FAIL: forge exited $RC without reaching the provider —"
  sed 's/^/    /' /tmp/tj_forge.log | tail -12
  FAIL=1
fi

# --- 3b. Edits never wait on forge's remote syntax check -------------------
# After every successful write/patch/multi_patch, forge uploads the edited
# file to services_url for a remote syntax check (forge_services
# fs_write.rs / fs_patch.rs -> validate_file) and ignores the outcome. With
# forge's default (https://api.forgecode.dev/) the jail cannot resolve it, so
# every edit silently waited out a 5s DNS timeout. agent/.forge.toml points
# services_url at a loopback port that refuses instantly. Forge says nothing
# either way — the tool result is identical — so THIS check is the alarm.
# Both halves: the resolved value must be the loopback target shipped in
# agent/.forge.toml, and the gap between the mock model sending a `write`
# tool call and forge's next request (tool run + upload attempt) must stay
# under a second. Whole-turn wall time is NOT a usable signal: with the
# default URL a plain `read` turn also takes ~5s, so write-vs-read hides it.
sec "forge edits don't wait on services_url"
WANT=$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb")).get("services_url",""))' agent/.forge.toml)
GOT=$(timeout 60 docker run --rm --network none --entrypoint sh "$IMAGE" -c \
  'forge config list --porcelain 2>/dev/null' 2>/dev/null \
  | sed -n 's/^services_url *= *"\(.*\)"$/\1/p' | head -1)
# shellcheck disable=SC2016  # $TOOL_ARGS and the python expand inside the container
timeout 120 docker run --rm --network none -v "$PWD/scripts/testdata:/testdata:ro" --entrypoint sh \
  -e OPENAI_URL=http://127.0.0.1:8765/v1 -e OPENAI_API_KEY=dummy \
  -e FORGE_SESSION__PROVIDER_ID=openai_compatible \
  -e FORGE_SESSION__MODEL_ID=deepseek-flash \
  -e MOCK_LOG=/tmp/mock.jsonl \
  -e TOOL_ARGS='{"file_path":"/tmp/probe-w.txt","content":"x"}' \
  "$IMAGE" -c '
    python3 /testdata/mock-openai.py write "$TOOL_ARGS" & sleep 1
    cd /tmp && timeout 60 forge -p ping >/dev/null 2>&1
    echo "written=$(cat /tmp/probe-w.txt 2>/dev/null)"
    python3 -c "
import json
rows = [json.loads(l) for l in open(\"/tmp/mock.jsonl\")]
sent = [r[\"t\"] for r in rows if r[\"event\"] == \"tool_call_sent\"]
after = [r[\"t\"] for r in rows if r[\"event\"] == \"request\" and sent and r[\"t\"] > sent[0]]
print(\"gap_ms=%d\" % ((after[0] - sent[0]) * 1000) if after else \"gap_ms=\")"' >/tmp/tj_edit.log 2>&1
GAP_MS=$(sed -n 's/^gap_ms=//p' /tmp/tj_edit.log)
if [[ -z "$WANT" ]]; then
  note "FAIL: agent/.forge.toml sets no services_url — forge would upload every edited file to"
  note "      https://api.forgecode.dev/ and, in the jail, stall ~5s per edit waiting on DNS."
  FAIL=1
elif [[ "$GOT" != "$WANT" ]] || ! [[ "$GOT" =~ ^https?://(127\.[0-9.]+|localhost)(:[0-9]+)?/ ]]; then
  note "FAIL: forge resolves services_url to '${GOT:-<unset>}', not the loopback target '$WANT'"
  note "      from agent/.forge.toml — edited files would go to a remote syntax-check service."
  FAIL=1
elif ! grep -q '^written=x' /tmp/tj_edit.log || [[ -z "$GAP_MS" ]]; then
  note "FAIL: could not time a forge write against the mock model —"
  sed 's/^/    /' /tmp/tj_edit.log | tail -8
  FAIL=1
elif [[ "$GAP_MS" -gt 1000 ]]; then
  note "FAIL: forge took ${GAP_MS} ms between a write tool call and its next model request."
  note "      Forge is waiting on its remote syntax check (services_url) again: every"
  note "      edit in a run will stall like this. Check services_url in agent/.forge.toml."
  FAIL=1
else
  note "OK: services_url is $GOT; write tool call -> next request in ${GAP_MS} ms"
fi

# --- 4. The backup harness still runs --------------------------------------
sec "claude harness present"
if docker run --rm --entrypoint sh "$IMAGE" -c 'timeout 30 claude --version' >/tmp/tj_claude.log 2>&1; then
  note "OK: $(tr -d '\r' </tmp/tj_claude.log | head -1)"
else
  note "FAIL: claude harness did not run —"; sed 's/^/    /' /tmp/tj_claude.log | tail -5
  FAIL=1
fi

# --- 5. Opt-in tool tracing (AGENT_TRACE=1) ---------------------------------
# trace-sh must be invisible to the command it wraps (output, exit status) and
# write exactly one record per command — none when tracing is off.
sec "trace-sh shim"
OUT=$(docker run --rm --network none --entrypoint sh "$IMAGE" -c '
  TOOL_TRACE_LOG=/tmp/t.jsonl trace-sh -c "echo shim-out; exit 7"; echo "rc=$?"
  trace-sh -c true
  python3 -c "import json; r=[json.loads(l) for l in open(\"/tmp/t.jsonl\")]; print(\"records=%d exit=%s\" % (len(r), r[0][\"exit\"]))"
' 2>&1)
if printf '%s' "$OUT" | grep -q "shim-out" && printf '%s' "$OUT" | grep -q "rc=7" \
   && printf '%s' "$OUT" | grep -q "records=1 exit=7"; then
  note "OK: output + exit status pass through; one record per command; silent when off"
else
  note "FAIL: trace-sh misbehaved —"; printf '%s\n' "$OUT" | sed 's/^/    /' | tail -8
  FAIL=1
fi

# The load-bearing check: forge must really run its shell tool through $SHELL.
# If a forge upgrade stops honouring it, every trace silently loses its tool
# lane. Drive the REAL entrypoint with AGENT_TRACE=1 against an offline mock
# model that asks for one slow shell command, then require its record.
sec "forge shell tool is traced end to end"
docker run --rm --network none -v "$PWD/scripts/testdata:/testdata:ro" --entrypoint sh \
  -e AGENT_TRACE=1 -e OPENAI_URL=http://127.0.0.1:8765/v1 -e OPENAI_API_KEY=dummy \
  -e TOOL_ARGS='{"command":"sleep 1; echo traced-by-shim","cwd":"/tmp"}' \
  "$IMAGE" -c '
    python3 /testdata/mock-openai.py shell "$TOOL_ARGS" & sleep 1
    timeout 60 /usr/local/bin/entrypoint.sh -p ping >/dev/null 2>&1; echo "entrypoint rc=$?"
    cat /tmp/trace/events.jsonl /tmp/trace/tools.jsonl' >/tmp/tj_trace.log 2>&1
WALL=$(grep -a 'traced-by-shim' /tmp/tj_trace.log | head -1 \
  | python3 -c 'import json,sys; print(int(json.loads(sys.stdin.readline())["wall_ms"]))' 2>/dev/null || echo 0)
if grep -q '"event":"harness_exec"' /tmp/tj_trace.log && [[ "$WALL" -ge 1000 ]]; then
  note "OK: forge's shell tool call recorded (${WALL} ms), entrypoint phases marked"
else
  note "FAIL: no timed record of forge's shell tool call —"
  sed 's/^/    /' /tmp/tj_trace.log | tail -12
  FAIL=1
fi

# --- 6. Built-in tools are timed from forge's status lines (AGENT_TRACE=1) --
# Forge runs read/write/patch/... inside its own process, where no shim
# reaches, but prints a status line ("● [HH:MM:SS] Read …") as each starts.
# The entrypoint runs forge under agent/ui-trace.py, which timestamps those
# lines into /tmp/trace/ui.jsonl for scripts/trace-render.py. Drive the REAL
# entrypoint through read -> write -> shell twice: plain, and under
# `docker run -t` — how run-task.sh starts forge, so ui-trace nests in a pty.
sec "forge built-in tools are timed from its status lines"
UI_PLAN='[{"name":"read","args":{"file_path":"/etc/hostname"}},{"name":"write","args":{"file_path":"/tmp/ui-probe.txt","content":"x"}},{"name":"shell","args":{"command":"echo ui-probe","cwd":"/tmp"}}]'
for mode in plain tty; do
  UI_TTY=(); [[ "$mode" == tty ]] && UI_TTY=(-t)
  # shellcheck disable=SC2016  # $UI_PLAN and $UI_MODE expand inside the container
  timeout 240 docker run --rm ${UI_TTY[@]+"${UI_TTY[@]}"} --network none \
    -v "$PWD/scripts/testdata:/testdata:ro" --entrypoint sh \
    -e AGENT_TRACE=1 -e OPENAI_URL=http://127.0.0.1:8765/v1 -e OPENAI_API_KEY=dummy \
    -e UI_PLAN="$UI_PLAN" -e UI_MODE="$mode" \
    "$IMAGE" -c '
      mkdir -p /tmp/trace
      python3 /testdata/mock-openai-seq.py "$UI_PLAN" /tmp/trace/requests.jsonl & sleep 1
      if [ "$UI_MODE" = tty ]; then
        # --foreground keeps the entrypoint in the foreground group of the tty,
        # as when run-task.sh starts it: the raw-mode / keystroke-relay path.
        timeout --foreground 150 /usr/local/bin/entrypoint.sh -p ping; rc=$?
      else
        timeout 150 /usr/local/bin/entrypoint.sh -p ping >/tmp/forge.out 2>&1; rc=$?
      fi
      echo "entrypoint rc=$rc"
      echo UI-BEGIN; cat /tmp/trace/ui.jsonl 2>/dev/null; echo UI-END' >"/tmp/tj_ui_$mode.log" 2>&1
  UI_CHECK=$(tr -d '\r' <"/tmp/tj_ui_$mode.log" | python3 -c '
import json, sys
text = sys.stdin.read()
body = text.split("UI-BEGIN", 1)[1].split("UI-END", 1)[0] if "UI-BEGIN" in text else ""
recs = []
for line in body.splitlines():
    try:
        recs.append(json.loads(line))
    except ValueError:
        pass
kinds = [r["kind"] for r in recs if r.get("kind")]
stamps = [r.get("t_ns", 0) for r in recs]
steps = iter(kinds)
in_order = all(k in steps for k in ("read", "write", "shell"))
monotonic = bool(stamps) and all(t > 0 for t in stamps) and stamps == sorted(stamps)
# Each stamp must match the HH:MM:SS forge printed (container clock is UTC):
# a late burst of identical stamps means ui-trace was not reading as forge ran.
def skew(r):
    h, m, s = map(int, r["clock"].split(":"))
    d = abs((r["t_ns"] // 10**9) % 86400 - (h * 3600 + m * 60 + s))
    return min(d, 86400 - d)
worst = max((skew(r) for r in recs if r.get("clock") and r.get("t_ns")), default=99)
ok = in_order and monotonic and worst <= 2
print("ok" if ok else "kinds=%s monotonic=%s worst_clock_skew_s=%s" % (kinds, monotonic, worst))')
  if [[ "$UI_CHECK" == ok ]] && tr -d '\r' <"/tmp/tj_ui_$mode.log" | grep -q 'entrypoint rc=0'; then
    note "OK ($mode): read, write and shell each timestamped from forge's status line, in order"
  else
    note "FAIL ($mode): ui-trace did not time forge's built-in tools — ${UI_CHECK:-no records}"
    tr -d '\r' <"/tmp/tj_ui_$mode.log" | sed 's/^/    /' | tail -12
    FAIL=1
  fi
done

# --- 7. Skills from node-config are listed by forge -------------------------
# Real-world failure (coordination #58): across 77 dispatched issue-work runs,
# `skill propose-change` failed 20 times with "Skill '<name>' not found" and
# every successful `skill` call loaded a forge built-in. Forge 2.13.18
# discovers skills from `.forge/skills/<name>/SKILL.md` relative to its CWD,
# and nothing pointed that at the library in `skills/`.
#
# The wiring is now IN THE REPO — a tracked `.forge/skills -> ../skills`
# symlink — so the honest test is to clone this repo the way the jail does,
# mount it where the clone lands, and run the image's REAL entrypoint. A test
# that re-implements the wiring inline would pass against an image (or a repo)
# that has none of it, which is precisely the regression we are hunting.
sec "skills from node-config are listed by forge"
WS=$(mktemp -d)
# Clone, so only COMMITTED content is under test: delete the symlink and this
# fails, exactly as a fresh jail clone would. World-writable because the
# entrypoint links AGENTS.md and appends to .git/info/exclude as uid agent.
if git clone -q . "$WS/node-config" 2>/tmp/tj_clone.log; then
  chmod -R a+w "$WS/node-config"
  # The entrypoint execs its arguments as the harness, so these args run forge
  # after the full workspace setup — the same boot path a real session takes.
  timeout 90 docker run --rm --network none \
    -v "$WS/node-config:/workspace/node-config" \
    "$IMAGE" list skills --porcelain >/tmp/tj_skills.log 2>&1
  # Every skill in the library must list, and list FROM .forge/skills — a
  # forge:// path would mean a built-in shadowed it, not our library loading.
  MISSING=""
  for d in skills/*/; do
    n=$(basename "$d")
    grep -Eq "^${n}[[:space:]]+\.forge/skills/${n}/SKILL\.md" /tmp/tj_skills.log \
      || MISSING="$MISSING $n"
  done
  if [[ -z "$MISSING" ]]; then
    note "OK: forge lists every skill in skills/ from .forge/skills (library is wired)"
  else
    note "FAIL: forge did not list these skills from skills/ —$MISSING"
    sed 's/^/    /' /tmp/tj_skills.log | tail -12
    note "(is the tracked .forge/skills -> ../skills symlink still committed?)"
    FAIL=1
  fi
else
  note "FAIL: could not clone the repo for the skills check —"
  sed 's/^/    /' /tmp/tj_clone.log | tail -5
  FAIL=1
fi
rm -rf "$WS"

echo
if [[ "$FAIL" -eq 0 ]]; then echo "test-jail-image: PASS"; else echo "test-jail-image: FAIL"; fi
exit "$FAIL"
