# copilot-headroom — a local token-optimization proxy for the copilot seat.
#
# Sits BETWEEN Claude Code (the copilot points ANTHROPIC_BASE_URL here) and the
# copilot-egress allowlist proxy: it compresses / semantic-caches the Anthropic
# traffic so the operator's flat-rate subscription buys more turns, while the
# ONLY internet path is still copilot-egress (Anthropic-owned hosts, nothing
# else). See docs/COPILOT.md for the contract.
#
# Distinct from copilot-egress: that is the egress GATE (deny-by-default
# allowlist); this is a token-cost OPTIMIZER that itself reaches Anthropic ONLY
# through that gate (via --http-proxy + HTTPS_PROXY). It joins NO other network
# — never `edge` (no direct internet around the allowlist), never `front`
# (the door), never `agents` (the spur). scripts/verify-config.sh asserts this.
#
# The base is tag-pinned (python:3.12-slim), matching the copilot seat's own
# posture (node:22-slim); a sha256 digest pin is a reasonable hardening
# follow-up, same as the copilot's ttyd pin.
FROM python:3.12-slim

# headroom-ai[proxy] = the proxy server + the local ONNX Kompress path. The ML
# Kompress model would otherwise fetch weights at runtime over general internet
# (huggingface.co et al), which the Anthropic-only egress refuses — so Kompress
# is DISABLED at runtime (HEADROOM_DISABLE_KOMPRESS in compose) and we keep the
# structural compressors (SmartCrusher / CacheAligner) + semantic cache, the
# parts that work air-gapped. The host (which has internet) builds this image.
RUN pip install --no-cache-dir "headroom-ai[proxy]"

# unprivileged, distinct uid from the egress companion (also 10001, different
# container/user-namespace) so the optimizer owns nothing it does not need to.
RUN useradd -m -u 10002 headroom

USER headroom
WORKDIR /home/headroom

EXPOSE 8787
# Args (--host/--port/--mode/--http-proxy ...) come from the compose `command:`.
ENTRYPOINT ["headroom", "proxy"]
