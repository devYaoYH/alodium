#!/bin/sh
# Prepare the jail's workspace, then hand off to the session's harness:
# forgecode by default, Claude Code as the backup utility
# (AGENT_HARNESS=claude). Both speak to LiteLLM with the same virtual key —
# forge via OPENAI_URL (any model family the key allows), claude via
# ANTHROPIC_BASE_URL.
set -eu

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
fi

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

# Claude leg: same knob, its native vars. Explicit ANTHROPIC_* env wins.
if [ -n "${AGENT_MODEL:-}" ]; then
  export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$AGENT_MODEL}"
  export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$AGENT_MODEL}"
fi

HARNESS="${AGENT_HARNESS:-forge}"
echo "[jail] harness: $HARNESS (AGENT_HARNESS=forge|claude), model: ${AGENT_MODEL:-image default} (AGENT_MODEL=<litellm alias>)"
exec "$HARNESS" "$@"
