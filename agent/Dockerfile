# The dev-agent jail. See docs/AGENT.md for the contract this implements:
# no docker socket, no secrets, agents network only (internal — no egress),
# inference via LiteLLM virtual key, write path = PRs to Forgejo.
FROM node:22-slim

# The jail has no internet; tell Claude Code not to try (updates, telemetry).
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates python3 ripgrep jq \
    && rm -rf /var/lib/apt/lists/*

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
