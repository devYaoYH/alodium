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
            # Flip the type: a key forge knows must reject this outright.
            yield path, k, ('99999' if isinstance(v, str) else '"bogus"')

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

# --- 4. The backup harness still runs --------------------------------------
sec "claude harness present"
if docker run --rm --entrypoint sh "$IMAGE" -c 'timeout 30 claude --version' >/tmp/tj_claude.log 2>&1; then
  note "OK: $(tr -d '\r' </tmp/tj_claude.log | head -1)"
else
  note "FAIL: claude harness did not run —"; sed 's/^/    /' /tmp/tj_claude.log | tail -5
  FAIL=1
fi

echo
if [[ "$FAIL" -eq 0 ]]; then echo "test-jail-image: PASS"; else echo "test-jail-image: FAIL"; fi
exit "$FAIL"
