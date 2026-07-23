# copilot — the operator's paired infra-sysadmin seat: an interactive Claude
# Code session served into the browser as a terminal (ttyd) over a durable tmux
# session. See docs/COPILOT.md for the contract.
#
# Distinct from the agent jail (agent/Dockerfile): the copilot authenticates to
# Anthropic with the operator's SUBSCRIPTION (flat-rate Opus/Sonnet via
# CLAUDE_CODE_OAUTH_TOKEN), reached ONLY through the copilot-egress allowlist
# proxy — never LiteLLM, never a metered API key. It is still a PROPOSER: its
# Forgejo token opens PRs, branch protection on main keeps merge (= deploy
# authorization) with the operator.
FROM node:22-slim

# No telemetry / autoupdate egress — keeps the egress allowlist to anthropic.com.
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# ttyd ships as a per-arch static binary; fetch the pinned release at BUILD time
# (the host has internet; the copilot never does — same pattern as the agent
# image's build-time binary fetch). Bump TTYD_VERSION to update; a sha256 pin is
# a reasonable hardening follow-up.
ARG TTYD_VERSION=1.7.7
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates tmux \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) ttyd_arch=x86_64 ;; \
         arm64) ttyd_arch=aarch64 ;; \
         *) echo "unsupported arch for ttyd: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /usr/local/bin/ttyd \
         "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${ttyd_arch}" \
    && chmod +x /usr/local/bin/ttyd \
    && ttyd --version

# Claude Code — the copilot's harness. Pinned by npm; the host fetches at build.
RUN npm install -g @anthropic-ai/claude-code

# unprivileged, high-uid user — no root in the seat.
RUN useradd -m -u 10001 copilot
COPY --chown=copilot:copilot entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER copilot
WORKDIR /workspace
EXPOSE 7681
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
