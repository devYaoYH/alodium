#!/usr/bin/env python3
"""__APP_NAME__ — skeleton service.

Stdlib only, on purpose: zero supply chain until the app earns dependencies.
The two routes here are the contract minimum: /healthz (the dashboard and
change pipeline poll it) and one example endpoint mirrored in openapi.yaml
and mcp-tools.json. Replace echo; keep healthz.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/v1/echo":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid json"})
                return
            self._send(200, {"echo": payload})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print(f"[__APP_NAME__] {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    print(f"[__APP_NAME__] listening on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
