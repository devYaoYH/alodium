#!/bin/sh
# Prepare the co-pilot's workspace, then serve an interactive Claude Code
# session into the browser via ttyd over a durable tmux session. The seat holds:
# the operator's subscription token (CLAUDE_CODE_OAUTH_TOKEN) and a scoped
# Forgejo token. It does NOT hold: a data-plane credential, the docker socket,
# host data mounts, or deploy access. Its only internet path is copilot-egress.
set -eu

GIT_USER="${COPILOT_GIT_USER:-copilot}"

if [ -n "${COPILOT_FORGEJO_TOKEN:-}" ]; then
  # forgejo:3000 is HTTP inside the agents network; TLS is Caddy's job at the door.
  git config --global credential.helper store
  printf 'http://%s:%s@forgejo:3000\n' "$GIT_USER" "$COPILOT_FORGEJO_TOKEN" \
    > "$HOME/.git-credentials"
  git config --global user.name  "$GIT_USER"
  git config --global user.email "$GIT_USER@node.invalid"

  if [ ! -d /workspace/node-config/.git ]; then
    echo "[copilot] cloning node-config from Forgejo..."
    git clone "http://forgejo:3000/${NODE_CONFIG_REPO:-operator/node-config}" \
      /workspace/node-config 2>/dev/null \
      || echo "[copilot] clone failed — check COPILOT_FORGEJO_TOKEN and that the repo exists."
  fi
else
  echo "[copilot] COPILOT_FORGEJO_TOKEN unset — no git identity; the PR/propose path is disabled."
fi

cd /workspace/node-config 2>/dev/null || cd /workspace

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "[copilot] WARNING: CLAUDE_CODE_OAUTH_TOKEN is unset — Claude Code has no"
  echo "[copilot]          subscription credential and will not authenticate. Run"
  echo "[copilot]          'claude setup-token' on a trusted machine, then put the"
  echo "[copilot]          token in secrets/copilot.env (see apps/copilot/env.example)."
fi

# Skip the first-run onboarding wizard. The interactive Claude Code TUI runs a
# theme + LOGIN wizard whenever ~/.claude.json has not recorded completion, and
# that wizard opens a BROWSER login even though CLAUDE_CODE_OAUTH_TOKEN already
# authenticates the seat (claude -p round-trips on the subscription token). Seed
# onboarding-complete + a theme + per-folder trust for the launch dir so the seat
# drops straight into a session on the token instead of prompting to log in.
# Idempotent: merges into whatever Claude has already written to the file.
CLAUDE_JSON="$HOME/.claude.json" LAUNCH_DIR="$(pwd)" node -e '
  const fs = require("fs");
  const p = process.env.CLAUDE_JSON, dir = process.env.LAUNCH_DIR;
  let d = {};
  try { d = JSON.parse(fs.readFileSync(p, "utf8")); } catch (e) {}
  d.hasCompletedOnboarding = true;
  if (!d.theme) d.theme = "dark";
  d.projects = d.projects || {};
  d.projects[dir] = Object.assign({}, d.projects[dir], { hasTrustDialogAccepted: true });
  fs.writeFileSync(p, JSON.stringify(d, null, 2));
' || echo "[copilot] WARNING: could not seed ~/.claude.json — the interactive TUI may show onboarding/login."

# ttyd serves the terminal; tmux makes the session survive tab-close/reconnect
# ('new -A' attaches if the session exists, else creates it). --writable lets the
# operator type. ttyd itself is unauthenticated: the Caddy door (ring0 + passkey
# SSO + operator-email-only) IS the authentication boundary.
exec ttyd \
  --port 7681 \
  --writable \
  tmux new -A -s copilot "${COPILOT_CMD:-claude}"
