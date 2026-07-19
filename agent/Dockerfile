# The dev-agent jail. See docs/AGENT.md for the contract this implements:
# no docker socket, no secrets, agents network only (internal — no egress),
# inference via LiteLLM virtual key, write path = PRs to Forgejo.
#
# Offline validators: the jail carries the SAME caddy binary prod runs (pinned
# digest below), so the agent can `caddy validate` a Caddyfile before pushing —
# a pure text check, no daemon, no socket, no network. This closes the recurring
# "config PR doesn't even load" loop (scripts/verify-config.sh drives it).
FROM caddy:2-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648 AS caddybin

FROM node:22-slim

# The jail has no internet; tell Claude Code not to try (updates, telemetry).
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates python3 python3-yaml ripgrep jq shellcheck \
    && rm -rf /var/lib/apt/lists/*

# The caddy binary is a static PIE — copy it straight in; it runs offline for
# `caddy validate` / `caddy adapt`. Version MUST track the compose caddy digest
# above so the agent validates against the same parser prod uses.
COPY --from=caddybin /usr/bin/caddy /usr/local/bin/caddy

# Two harnesses, one contract (AGENTS.md): forgecode is the DEFAULT (open
# source, provider-agnostic — any OpenAI-compatible model family via
# LiteLLM); Claude Code stays as the backup utility. Select per-session
# with AGENT_HARNESS=forge|claude (entrypoint). forgecode pinned: its npm
# postinstall fetches the platform binary at BUILD time (host has internet;
# the jail never does).
RUN npm install -g @anthropic-ai/claude-code forgecode@2.13.18

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# unprivileged, high-uid user; rootless podman later maps this differently per-app
RUN useradd -m -u 10001 agent
# AGENTS.md is the canonical, framework-agnostic operating contract; each
# tenant image wires it wherever its framework looks. Claude Code: user
# memory. Forgecode: the AGENTS.md standard (entrypoint links it into the
# workspace repo, where forge looks). Forge's provider/model come from env
# (OPENAI_URL/OPENAI_API_KEY + FORGE_SESSION__* pins, see entrypoint) —
# forge 2.13 ignores project .forge.toml [[providers]] despite its docs.
COPY --chown=agent:agent AGENTS.md /home/agent/.claude/CLAUDE.md
COPY --chown=agent:agent AGENTS.md /home/agent/AGENTS.md
USER agent
WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
