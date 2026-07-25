#!/usr/bin/env python3
"""Smoke: one check per surface the egress-broker ships. Stdlib only.

Mirrors the search-broker smoke (unit-style with fakes) rather than floor's
network-driven smoke, because the egress family is split across four isolated
networks by design — audit-api lives on egress-admin (caddy-only), so a single
test container can never reach all three roles on the wire. Instead we start
each role on a localhost port with the DB stubbed and exercise the auth /
allowlist / audit contract directly.

Run (inside the app image, which has python3 + this source):
  python3 tests/smoke.py
"""
import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# Configure before importing app so its module-level env reads pick these up.
os.environ.setdefault("AGENT_EGRESS_TOKEN", "agent-test-token")
os.environ.setdefault("EGRESS_EGRESS_TOKEN", "egress-test-token")
os.environ.setdefault("EGRESS_AUDIT_TOKEN", "audit-test-token")
os.environ.setdefault("EGRESS_AUDIT_DATABASE_URL", "postgresql://stub/stub")

import app  # noqa: E402

failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL") + f"  {name}")
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeStore:
    """In-memory stand-in for AuditStore; same method surface."""

    def __init__(self):
        self.rows = []

    def health(self):
        pass

    def begin_egress(self, tenant_id, request_id, key_id, target_host,
                     capability_id, method, path, headers):
        self.rows.append({
            "id": request_id, "tenant_id": tenant_id, "key_id": key_id,
            "target_host": target_host, "capability_id": capability_id,
            "request_method": method, "request_path": path, "state": "pending",
        })

    def complete_egress(self, request_id, upstream_status, response_bytes, duration_ms):
        for r in self.rows:
            if r.get("id") == request_id:
                r["state"] = "completed"
                r["upstream_status"] = upstream_status

    def fail_egress(self, request_id, reason, duration_ms):
        for r in self.rows:
            if r.get("id") == request_id:
                r["state"] = "failed"
                r["failure_reason"] = reason

    def deny_egress(self, tenant_id, request_id, key_id, target_host,
                    reason, method, path):
        self.rows.append({
            "id": request_id, "tenant_id": tenant_id, "key_id": key_id,
            "target_host": target_host, "request_method": method,
            "request_path": path, "state": "denied", "failure_reason": reason,
        })

    def events(self, limit):
        return list(self.rows[:limit])


class _FakeResponse:
    status = 200

    def __init__(self, body):
        self._body = body

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def getheader(self, name, default=None):
        return default


class FakeConn:
    """Stands in for http.client.HTTPConnection so the broker's forward to
    egress-out succeeds without a real egress-out running."""

    def __init__(self, *args, **kwargs):
        body = json.dumps({
            "status": 200,
            "content_type": "text/plain",
            "body_b64": base64.b64encode(b"ok").decode("ascii"),
        }).encode("utf-8")
        self._resp = _FakeResponse(body)

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self._resp

    def close(self):
        pass


# Wire the fakes into the app module before serving.
STORE = FakeStore()
app.AUDIT_STORE = STORE
app.profile_hosts = lambda: {"example.com"}          # the broker allowlist
app.HTTPConnection = FakeConn                         # broker -> egress-out


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #

def request(method, url, headers=None, body=None):
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_json(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return request("POST", url, h, data)


def start(role, port):
    app.ROLE = role
    srv = ThreadingHTTPServer(("127.0.0.1", port), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- #
# broker (:8080 contract)
# --------------------------------------------------------------------------- #

BPORT = 18080
start("broker", BPORT)
B = f"http://127.0.0.1:{BPORT}"

# healthz
status, body = request("GET", B + "/healthz")
check("broker healthz 200", status == 200)

req_body = {"host": "https://example.com", "path": "/"}

# missing token -> denied
status, _ = post_json(B + "/v1/egress", req_body)
check("broker missing token rejected (403)", status == 403)

# invalid token -> denied
status, _ = post_json(B + "/v1/egress", req_body, {"Authorization": "Bearer wrong"})
check("broker invalid token rejected (403)", status == 403)

# valid token, non-allowlisted host -> denied
status, _ = post_json(B + "/v1/egress", {"host": "https://evil.com", "path": "/"},
                      {"Authorization": "Bearer agent-test-token"})
check("broker non-allowlisted host denied (403)", status == 403)

denied_rows = [r for r in STORE.rows if r.get("state") == "denied"]
check("broker denial wrote an audit row", any(
    r.get("target_host") == "evil.com" for r in denied_rows))

# valid token, allowlisted host -> forwarded (FakeConn) -> 200 + audit row
before = len(STORE.rows)
status, body = post_json(B + "/v1/egress", req_body,
                         {"Authorization": "Bearer agent-test-token"})
check("broker allowlisted host succeeds (200)", status == 200 and b"ok" in body)
check("broker allowed request wrote an audit row", len(STORE.rows) > before)
check("broker allowed request audit row is completed", any(
    r.get("state") == "completed" and r.get("target_host") == "example.com"
    for r in STORE.rows))

# --------------------------------------------------------------------------- #
# egress (:8081 contract) — tokenless direct calls rejected
# --------------------------------------------------------------------------- #

EPORT = 18081
start("egress", EPORT)
E = f"http://127.0.0.1:{EPORT}"

status, _ = post_json(E + "/internal/egress", req_body)
check("egress-out tokenless direct call rejected (401)", status == 401)

status, _ = post_json(E + "/internal/egress", req_body,
                      {"X-Egress-Egress-Token": "egress-test-token"})
# A valid token passes the auth gate; the real upstream fetch is a runtime
# concern (needs edge egress + a live host). We only assert the gate opened:
# the response is NOT 401. (It may be 400/502 depending on the fake host.)
check("egress-out valid token passes the auth gate (not 401)", status != 401)

# --------------------------------------------------------------------------- #
# audit-api (:8082 contract) — token-gated, returns the rows
# --------------------------------------------------------------------------- #

APORT = 18082
start("audit-api", APORT)
A = f"http://127.0.0.1:{APORT}"

status, _ = request("GET", A + "/v1/audit")
check("audit-api missing token rejected (401)", status == 401)

status, _ = request("GET", A + "/v1/audit",
                    {"X-Egress-Audit-Token": "audit-test-token"})
check("audit-api valid token returns 200", status == 200)

status, body = request("GET", A + "/v1/audit?limit=100",
                       {"X-Egress-Audit-Token": "audit-test-token"})
events = json.loads(body).get("events") if status == 200 else []
check("audit-api returns recorded audit rows", isinstance(events, list) and len(events) > 0)

# --------------------------------------------------------------------------- #

print()
sys.exit(1 if failures else 0)
