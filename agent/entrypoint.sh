#!/bin/sh
# Prepare the jail's workspace, then hand off to Claude Code.
set -eu

if [ -n "${AGENT_FORGEJO_TOKEN:-}" ]; then
  git config --global credential.helper store
  # forgejo:3000 is HTTP inside the agents network; TLS is Caddy's job at the door
  printf 'http://agent-dev:%s@forgejo:3000\n' "$AGENT_FORGEJO_TOKEN" \
    > "$HOME/.git-credentials"
  git config --global user.name  "agent-dev"
  git config --global user.email "agent-dev@node.invalid"

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
exec claude "$@"
