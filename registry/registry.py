#!/usr/bin/env python3
"""The service registry: one endpoint answering "what services exist on this
node and what can I call" (docs/DESIGN.md, "App manifests and the service
registry").

It OWNS nothing. App manifests (manifest/*.toml) are the source of truth;
this process re-reads them per request — no cache to invalidate, no state to
back up, and a merged node-config PR is live here the moment deploy syncs the
checkout. Discovery and audit only, by decision-log policy: never a gateway,
never in an authorization path. Being readable here is not permission to call
— per-caller credentials still come from the manifest `needs` flow.

Stdlib only (agent legibility: boring beats clever), read-only mounts,
~150 lines. Consumers:
  humans     https://registry.<domain>          (ring 0 via Caddy)
  agents     http://registry:8090/v1/services   (the spur; effectively MCP discovery)
  installer  scripts/install.sh validation
"""
import json
import os
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MANIFEST_DIR = Path("/srv/manifest")
PORT = 8090


def load_services() -> list[dict]:
    """Aggregate every real app manifest; *.example.toml is documentation."""
    services = []
    for path in sorted(MANIFEST_DIR.glob("*.toml")):
        if path.name.endswith(".example.toml"):
            continue
        try:
            manifest = tomllib.loads(path.read_text())
        except (tomllib.TOMLDecodeError, OSError) as exc:
            # A broken manifest is a fact worth serving, not hiding.
            services.append({"manifest": path.name, "error": str(exc)})
            continue
        app = manifest.get("app", {})
        svc = manifest.get("service", {})
        services.append({
            "manifest": path.name,
            "name": app.get("name", path.stem),
            "version": app.get("version"),
            "description": app.get("description"),
            "ring": app.get("ring"),
            # The callable surface: where it listens, what contracts it ships.
            "endpoint": f"http://{app.get('name', path.stem)}:{svc.get('port')}"
                        if svc.get("port") else None,
            "api": svc.get("api"),          # OpenAPI contract, in the app repo
            "mcp": svc.get("mcp"),          # typed tool surface for agents
            "health": svc.get("health"),
            # Audit inventory: what it declared, therefore all it can hold.
            "needs": manifest.get("needs", {}),
            "backup": manifest.get("lifecycle", {}).get("backup", []),
        })
    return services


class Handler(BaseHTTPRequestHandler):
    server_version = "sovereign-registry/0"

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, indent=2).encode() + b"\n")

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.rstrip("/") or "/"
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
        elif path == "/" or path == "/v1":
            self._json(200, {
                "node": os.environ.get("NODE_DOMAIN", "unset"),
                "endpoints": ["/v1/services", "/v1/services/<name>", "/healthz"],
                "note": "discovery and audit only; credentials come from manifests",
            })
        elif path == "/v1/services":
            self._json(200, load_services())
        elif path.startswith("/v1/services/"):
            name = path.removeprefix("/v1/services/")
            for svc in load_services():
                if svc.get("name") == name:
                    self._json(200, svc)
                    return
            self._json(404, {"error": f"no manifest declares app.name={name!r}"})
        else:
            self._json(404, {"error": "unknown path", "try": "/v1/services"})

    def log_message(self, fmt: str, *args) -> None:
        # One line per request to stdout; docker logs is the audit trail.
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"registry: serving {MANIFEST_DIR} on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
