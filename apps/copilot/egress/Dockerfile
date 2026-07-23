# copilot-egress — the copilot seat's ONLY internet path: a deny-by-default
# CONNECT proxy that tunnels TLS to the Anthropic API and refuses all else.
# Stdlib Python on the same pinned base the floor/registry use — no third-party
# proxy binary to trust, allowlist auditable in proxy.py.
FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

RUN adduser -D -u 10001 egress
COPY proxy.py /srv/proxy.py
USER egress
EXPOSE 8080
ENTRYPOINT ["python3", "/srv/proxy.py"]
