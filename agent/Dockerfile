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
      xxd file \
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
# Opt-in tool tracing shim: with AGENT_TRACE=1 the entrypoint makes it forge's
# $SHELL, timing every shell tool call (costs and rationale: file header).
COPY trace-sh.py /usr/local/bin/trace-sh
# ...and ui-trace runs forge under a pty, timestamping its per-tool status
# lines so built-in tools (read, write, patch, ...) get measured starts.
COPY ui-trace.py /usr/local/bin/ui-trace
# Forgejo API helper — the default way agents talk to coordination + node-config
# (issue view/comment/label, PR list/view/create/request-review). Stdlib only,
# no docker sockets, no secrets in argv: reads AGENT_FORGEJO_TOKEN from env.
COPY forgejo.py /usr/local/bin/forgejo
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/trace-sh \
              /usr/local/bin/ui-trace /usr/local/bin/forgejo

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
# forgecode global config: session limits, model defaults, etc.
# ForgeCode reads ~/forge/.forge.toml automatically — no symlinks needed.
#
# ~/forge is ALSO forge's writable state dir: on startup it migrates provider
# credentials from env into it. This RUN executes as root (USER agent is
# below), and COPY --chown fixes only the FILE — so the chown here is
# load-bearing, not tidiness. Without it forge cannot write its own config dir
# and does not error: it BLOCKS FOREVER with no output, hanging every jail
# session and every dispatched issue-work container.
# Regression test: scripts/test-jail-image.sh.
RUN mkdir -p /home/agent/forge && chown agent:agent /home/agent/forge
COPY --chown=agent:agent .forge.toml /home/agent/forge/.forge.toml
USER agent
WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
