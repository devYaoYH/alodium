#!/usr/bin/env python3
"""On-demand app lifecycle controller — a narrow, typed HTTP API.

Starts and stops declared on-demand apps (allowlist from APPS env).
Requires a pre-shared API key (LAUNCHER_API_KEY env) in the
X-API-Key header. Records every request to an audit log.

Intended to be reached from Caddy at the /api/launcher/* path, behind
the ring1 guard. No agent-network caller can route here.
"""

import datetime
import json
import os
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "sovereign-node")
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "/srv/compose")
ALLOWLIST = os.environ.get("ALLOWED_ON_DEMAND_APPS", "snake").split(",")
AUDIT_LOG = os.environ.get("AUDIT_LOG", "/var/log/launcher/audit.jsonl")
LAUNCHER_API_KEY = os.environ.get("LAUNCHER_API_KEY", "")
PORT = int(os.environ.get("PORT", "8081"))


def audit(action, app, actor, result, detail=""):
    """Append a structured audit entry to the JSONL log."""
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "actor": actor,
        "action": action,
        "app": app,
        "result": result,
        "detail": detail,
    }
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def compose_up(service):
    """Start a compose service by its service name, using the on-demand profile.

    Never builds. The launcher runs as an unprivileged user (nobody, HOME=
    /nonexistent) through a scoped socket proxy — it structurally cannot build
    an image (BuildKit can't write its state under a non-existent HOME, failing
    with a cryptic `mkdir /nonexistent: permission denied`). On-demand images
    are pre-built by the deploy (scripts/deploy.sh step 4c) precisely so launch
    is a pure start. `--pull never --no-build` makes that contract explicit: a
    missing image fails fast and legibly ("No such image: ...") instead of
    detouring into an impossible build.
    """
    return subprocess.run(
        ["docker", "compose", "--project-directory", COMPOSE_DIR, "--project-name", COMPOSE_PROJECT,
         "--profile", "on-demand", "up", "-d", "--pull", "never", "--no-build", service],
        capture_output=True, text=True, timeout=60,
    )


def compose_stop(service):
    """Stop a compose service by its service name."""
    return subprocess.run(
        ["docker", "compose", "--project-directory", COMPOSE_DIR, "--project-name", COMPOSE_PROJECT,
         "stop", service],
        capture_output=True, text=True, timeout=30,
    )


def container_status(service):
    """Check if a container (by name) is running. Returns 'running' or 'stopped'."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", service],
            capture_output=True, text=True, timeout=10,
        )
        status = r.stdout.strip()
        if status == "running":
            return "running"
        return "stopped"
    except Exception:
        return "stopped"


def check_api_key(headers):
    """Validate the X-API-Key header. Returns actor string or None."""
    if not LAUNCHER_API_KEY:
        return None  # fail closed — no key configured = no auth possible
    key = headers.get("X-API-Key", "")
    if key == LAUNCHER_API_KEY:
        return "homepage"
    return None


class LauncherHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the on-demand launcher API."""

    def log_message(self, format, *args):
        """Suppress default stderr logging; use our audit log."""
        pass

    def _send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            return json.loads(self.rfile.read(length))
        return {}

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-API-Key, Content-Type")
        self.end_headers()

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        actor = check_api_key(self.headers)

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "status":
            app = parts[2]
            if app not in ALLOWLIST:
                self._send_json(403, {"error": "app not in allowlist"})
                return
            status = container_status(app)
            self._send_json(200, {"app": app, "status": status})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        actor = check_api_key(self.headers)
        if not actor:
            self._send_json(401, {"error": "unauthorized"})
            return

        if len(parts) == 3 and parts[0] == "api" and parts[2] in ALLOWLIST:
            app = parts[2]
            action = parts[1]

            if action == "launch":
                status_before = container_status(app)
                if status_before == "running":
                    audit("launch", app, actor, "skipped", "already running")
                    self._send_json(200, {"app": app, "status": "running", "detail": "already running"})
                    return

                audit("launch", app, actor, "started")
                r = compose_up(app)
                if r.returncode == 0:
                    audit("launch", app, actor, "success")
                    self._send_json(200, {"app": app, "status": "starting"})
                else:
                    detail = r.stderr.strip()[:500]
                    # A missing image is not a launch bug — it means the deploy
                    # never pre-built this on-demand app's image (the launcher
                    # cannot build; see compose_up). Return an actionable hint
                    # instead of the raw daemon error so the operator knows to
                    # re-run the deploy rather than debug the launcher.
                    if "No such image" in detail or "no such image" in detail:
                        detail = (f"image for '{app}' is not built — on-demand images are "
                                  f"pre-built by the deploy (scripts/deploy.sh step 4c). "
                                  f"Re-run the deploy to build it. [{detail}]")
                    audit("launch", app, actor, "failed", detail)
                    self._send_json(500, {"error": "launch failed", "detail": detail})
                return

            elif action == "stop":
                status_before = container_status(app)
                if status_before == "stopped":
                    audit("stop", app, actor, "skipped", "already stopped")
                    self._send_json(200, {"app": app, "status": "stopped", "detail": "already stopped"})
                    return

                audit("stop", app, actor, "started")
                r = compose_stop(app)
                if r.returncode == 0:
                    audit("stop", app, actor, "success")
                    self._send_json(200, {"app": app, "status": "stopped"})
                else:
                    detail = r.stderr.strip()[:500]
                    audit("stop", app, actor, "failed", detail)
                    self._send_json(500, {"error": "stop failed", "detail": detail})
                return

        self._send_json(404, {"error": "not found"})


def main():
    server = HTTPServer(("0.0.0.0", PORT), LauncherHandler)
    print(f"launcher listening on :{PORT}, allowlist={ALLOWLIST}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()