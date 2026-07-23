#!/usr/bin/env python3
"""copilot-egress — a deny-by-default HTTPS forward proxy.

The copilot seat has no direct internet: it sits on internal networks only
(front, agents, copilot-egress). Its ONE sanctioned egress is this proxy, which
accepts CONNECT tunnels to an allowlist of hosts and refuses everything else.
The allowlist is Anthropic's API only — so a compromised copilot session can
reach the model it needs and nothing else on the internet.

Stdlib only, no dependencies; the allowlist is right here in the open for the
operator (and scripts/verify-config.sh) to read. Only CONNECT is handled: this
proxy exists for TLS tunnels to the model endpoint, not general HTTP forwarding.
"""
import os
import select
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))

# Deny-by-default: only these host suffixes may be tunneled. A leading dot means
# "this domain and any subdomain"; an entry without a dot must match exactly.
ALLOW = tuple(
    h.strip().lower()
    for h in os.environ.get("EGRESS_ALLOW", ".anthropic.com").split(",")
    if h.strip()
)

CONNECT_TIMEOUT = 10   # seconds to establish the upstream socket
IDLE_TIMEOUT = 300     # seconds a tunnel may sit idle before we tear it down


def host_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    for rule in ALLOW:
        if rule.startswith("."):
            if host == rule[1:] or host.endswith(rule):
                return True
        elif host == rule:
            return True
    return False


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _deny(self, code, msg):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        sys.stderr.write(f"[egress] DENY {msg}\n")

    def do_CONNECT(self):
        host, _, port = self.path.partition(":")
        try:
            port = int(port or "443")
        except ValueError:
            self._deny(400, f"bad target {self.path!r}")
            return
        if not host_allowed(host):
            self._deny(403, f"{host}:{port} not in allowlist {ALLOW}")
            return
        try:
            upstream = socket.create_connection((host, port), CONNECT_TIMEOUT)
        except OSError as e:
            self._deny(502, f"{host}:{port} connect failed: {e}")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        sys.stderr.write(f"[egress] ALLOW {host}:{port}\n")
        self.close_connection = True  # we own the socket now; end the HTTP loop
        self._tunnel(self.connection, upstream)

    def _tunnel(self, client, upstream):
        socks = [client, upstream]
        try:
            while True:
                r, _, x = select.select(socks, [], socks, IDLE_TIMEOUT)
                if x or not r:
                    return
                for s in r:
                    other = upstream if s is client else client
                    data = s.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
        except OSError:
            return
        finally:
            for s in (client, upstream):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                upstream.close()
            except OSError:
                pass

    # Everything that is not CONNECT is refused — no plain-HTTP forwarding.
    def _reject_plain(self):
        self._deny(405, f"{self.command} {self.path} (only CONNECT permitted)")

    do_GET = _reject_plain
    do_POST = _reject_plain
    do_PUT = _reject_plain
    do_DELETE = _reject_plain
    do_HEAD = _reject_plain
    do_PATCH = _reject_plain
    do_OPTIONS = _reject_plain

    def log_message(self, *args):  # decisions are logged in _deny/_tunnel
        pass


def main():
    sys.stderr.write(f"[egress] listening on :{PORT}; allow={ALLOW}\n")
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Proxy)
    server.serve_forever()


if __name__ == "__main__":
    main()
