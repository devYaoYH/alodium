#!/bin/sh
# Prepare the jail's workspace, then hand off to the session's harness:
# forgecode by default, Claude Code as the backup utility
# (AGENT_HARNESS=claude). Both speak to LiteLLM with the same virtual key —
# forge via OPENAI_URL (any model family the key allows), claude via
# ANTHROPIC_BASE_URL.
set -eu

# Opt-in wall-clock tracing (AGENT_TRACE=1, off by default). scripts/run-task.sh
# sets it and copies /tmp/trace out of the stopped container; nothing leaves
# the jail on its own. These marks time the entrypoint's phases; forge's shell
# tool is timed by trace-sh (see the SHELL export below).
trace_mark() {
  [ "${AGENT_TRACE:-0}" = 1 ] || return 0
  mkdir -p /tmp/trace
  printf '{"t_ns":%s,"event":"%s"}\n' "$(date +%s%N)" "$1" >> /tmp/trace/events.jsonl
}
trace_mark entrypoint_start

if [ -n "${AGENT_FORGEJO_TOKEN:-}" ]; then
  # Which tenant identity this session runs as (agent-dev, assistant, ...)
  GIT_USER="${AGENT_GIT_USER:-agent-dev}"
  git config --global credential.helper store
  # forgejo:3000 is HTTP inside the agents network; TLS is Caddy's job at the door
  printf 'http://%s:%s@forgejo:3000\n' "$GIT_USER" "$AGENT_FORGEJO_TOKEN" \
    > "$HOME/.git-credentials"
  git config --global user.name  "$GIT_USER"
  git config --global user.email "$GIT_USER@node.invalid"

  if [ ! -d /workspace/node-config/.git ]; then
    echo "[jail] cloning node-config from Forgejo..."
    git clone http://forgejo:3000/"${NODE_CONFIG_REPO:-$(whoami)/node-config}" \
      /workspace/node-config 2>/dev/null \
      || echo "[jail] clone failed — create node-config in Forgejo first (docs/MIRRORING.md)"
  fi
else
  echo "[jail] AGENT_FORGEJO_TOKEN unset — read-only sandbox, no PR path."
fi

cd /workspace/node-config 2>/dev/null || cd /workspace

# Forge follows the AGENTS.md standard in the project root; link the
# jail's copy into whatever repo we landed in, untracked (the contract is
# the image's business, never a commit in node-config).
if [ -d .git ]; then
  [ -e AGENTS.md ] || { ln -s "$HOME/AGENTS.md" AGENTS.md; echo "AGENTS.md" >> .git/info/exclude; }

  # Forge 2.13.18 discovers skills from `.forge/skills/<name>/SKILL.md`
  # (CWD-relative). There is NO config key to redirect that path — verified
  # against the binary: `forge config list` exposes no skills knob, and the
  # only skill strings in it are the built-in `forge://skills/...` entries.
  # The procedure library itself lives at `skills/<name>/SKILL.md`, the single
  # source of truth AGENTS.md already points every tenant at.
  #
  # Materialise it as REAL files under .forge/skills at boot — not a symlink
  # into the shared tree. Forge then sees explicit, self-contained agent skills
  # for this jail runtime (its own copies, resolvable with no indirection), and
  # the source stays `skills/` alone: copy-on-boot means a `skills/` edit lands
  # on the next container with no second location to drift. Without this,
  # `skill propose-change` (and the rest of the library) fails with
  # "Skill '<name>' not found" inside every jail run.
  if [ -d skills ]; then
    # Fresh materialisation each boot: a re-clone of node-config into a
    # persistent workspace would otherwise leave stale skills behind, so
    # drop any prior copy before re-seeding.
    mkdir -p .forge
    rm -rf .forge/skills
    mkdir -p .forge/skills
    # cp -r skills/* (not `cp -r skills .forge/skills`) so each skill lands
    # directly under .forge/skills/<name>/ on both GNU and busybox cp, with no
    # chance of a `.forge/skills/skills/` double-nest.
    cp -r skills/* .forge/skills/ 2>/dev/null || cp -r skills .forge/skills
    # Exclude the whole .forge/ tree — only the materialised skills live here,
    # and any future per-session forge state (conversation db, etc.) is the
    # image's business too.
    grep -qxF '.forge/' .git/info/exclude 2>/dev/null || echo '.forge/' >> .git/info/exclude
  fi
fi
trace_mark workspace_ready

# AGENT_MODEL is the harness-agnostic model knob (a LiteLLM alias). The
# tenant's virtual-key allowlist is the authority — this is only a request.
#
# Forge leg: forge 2.13 IGNORES project .forge.toml [[providers]] (despite
# its docs); the working non-interactive surface is its figment env layer.
# Provider auth arrives via forge's startup migration of OPENAI_URL +
# OPENAI_API_KEY (both point at LiteLLM), and the session pin below skips
# the interactive provider/model picker. Without the pin, an unpinned forge
# picks its own model — verified sending deepseek-flash traffic.
export FORGE_SESSION__PROVIDER_ID="${FORGE_SESSION__PROVIDER_ID:-openai_compatible}"
# Fallback when nothing is configured = deepseek-flash (operator decision:
# the unconfigured default should be the cheap model, never a premium one).
export FORGE_SESSION__MODEL_ID="${FORGE_SESSION__MODEL_ID:-${AGENT_MODEL:-deepseek-flash}}"
# Autonomous-mode ceiling (raise forge's default 100-request cap so harder
# tasks don't hit the wall mid-turn): max_requests_per_turn in the image's
# ~/forge/.forge.toml, TOP-LEVEL — nested in a table forge ignores it.
# There is NO env override: forge 2.13.18's binaries contain no
# FORGE_MAX_REQUESTS_PER_TURN string (unlike FORGE_SESSION__*, which they do
# read), so the export that used to sit here was inert and read as a runtime
# knob that did not exist. Change the cap in agent/.forge.toml and rebuild.

# Claude leg: same knob, its native vars. Explicit ANTHROPIC_* env wins.
if [ -n "${AGENT_MODEL:-}" ]; then
  export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$AGENT_MODEL}"
  export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$AGENT_MODEL}"
fi

HARNESS="${AGENT_HARNESS:-forge}"

# Forge runs its shell tool as `$SHELL -c <command>`, so pointing SHELL at
# trace-sh (agent/trace-sh.py) times every command. Forge only: that is the
# path scripts/test-jail-image.sh proves; Claude Code is untested with the shim.
if [ "${AGENT_TRACE:-0}" = 1 ] && [ "$HARNESS" = forge ]; then
  export SHELL=/usr/local/bin/trace-sh TOOL_TRACE_LOG=/tmp/trace/tools.jsonl
fi

echo "[jail] harness: $HARNESS (AGENT_HARNESS=forge|claude), model: ${AGENT_MODEL:-image default} (AGENT_MODEL=<litellm alias>)"
trace_mark harness_exec
# Built-in tools (read, write, patch, ...) run inside forge, where no shim
# reaches; ui-trace (agent/ui-trace.py) timestamps the status line forge
# prints as each one starts. Output, exit status and signals pass through.
if [ "${AGENT_TRACE:-0}" = 1 ] && [ "$HARNESS" = forge ]; then
  FORGE_UI_TRACE=/tmp/trace/ui.jsonl exec /usr/local/bin/ui-trace "$HARNESS" "$@"
fi
exec "$HARNESS" "$@"
