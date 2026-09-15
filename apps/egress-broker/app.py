#!/usr/bin/env python3
"""Capability-gated brokered egress for the agent network.

Three roles share this single image (EGRESS_ROLE):

  broker    (:8080, on `agents` + `egress-private` + `egress-data`)
            Agents call POST /v1/egress with AGENT_EGRESS_TOKEN (Bearer). The
            broker enforces the profile allowlist, forwards approved requests to
            egress-out with EGRESS_EGRESS_TOKEN, and writes one audit row per
            request via the writer DSN. /healthz.

  egress    (:8081, the only egress service on `edge` + `egress-private`)
            Authenticates the broker via EGRESS_EGRESS_TOKEN, enforces EGRESS_ALLOW
            (defense-in-depth — the broker already enforces per-profile), and
            performs the actual outbound fetch. /healthz.

  audit-api (:8082, Ring 0 on `egress-admin` + `egress-data`)
            Read-only; authenticates via EGRESS_AUDIT_TOKEN (X-Egress-Audit-Token,
            injected by Caddy server-side) and reads via the reader DSN. /healthz.

Stdlib-only except psycopg for the audit DB. Upstream responses are untrusted
input; the broker bounds what it retains (capped bytes) and returns. This is a
deterministic brokered capability, not a transparent HTTP proxy.
"""
import base64
import hmac
import json
import os
import sys
import time
import tomllib
import uuid
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from audit_store import AuditStore

PORT = int(os.environ.get("PORT", "8080"))
ROLE = os.environ.get("EGRESS_ROLE", "broker")
MAX_BODY_BYTES = 16_384
MAX_RESPONSE_BYTES = 262_144           # 256 KiB cap on any single upstream reply
MAX_AUDIT_EVENTS = 200
MAX_HEADERS = 20
MAX_HEADER_NAME = 200
MAX_HEADER_VALUE = 4_096
MAX_HOST_LEN = 253
MAX_PATH_LEN = 2_048
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
PROFILE_DIR = Path(os.environ.get("EGRESS_PROFILE_DIR", "/etc/egress-profiles"))
AUDIT_STORE = None
_PROFILE_HOSTS = None


# Sentinel returned by _audit_authorised() when the configuration error path
# already wrote its own 503 response. Callers must check this BEFORE the
# "is not True" branch so we send exactly one response on the wire (writing
# 401 after 503 is a write-after-headers error on the same connection).
_ALREADY_RESPONDED = object()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def configured(name):
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def safe_equal(received, expected):
    """Constant-time compare; an empty received value is never equal."""
    return bool(received) and hmac.compare_digest(received, expected)


def audit_store():
    global AUDIT_STORE
    if AUDIT_STORE is None:
        AUDIT_STORE = AuditStore(os.environ.get("EGRESS_AUDIT_DATABASE_URL", ""))
    return AUDIT_STORE


def profile_hosts():
    """Load the broker's a-priori allowlist from the named egress profile.

    Profiles are reviewed, version-controlled files under manifest/egress-profiles
    (mounted read-only at PROFILE_DIR). An unknown profile name is fatal — the
    container refuses to serve rather than fall back to an open default.
    """
    global _PROFILE_HOSTS
    if _PROFILE_HOSTS is None:
        name = os.environ.get("EGRESS_PROFILE", "default")
        path = PROFILE_DIR / f"{name}.toml"
        if not path.is_file():
            raise RuntimeError(
                f"egress profile '{name}' not found at {path} — refusing to serve"
            )
        data = tomllib.loads(path.read_text())
        hosts = data.get("default_egress_allowlist", {}).get("hosts", []) or []
        _PROFILE_HOSTS = {str(h).strip().lower() for h in hosts if str(h).strip()}
    return _PROFILE_HOSTS


def egress_allow_hosts():
    """egress-out's defense-in-depth allowlist (EGRESS_ALLOW, comma-separated)."""
    raw = os.environ.get("EGRESS_ALLOW", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def parse_host(host):
    if not isinstance(host, str) or not host:
        raise ValueError("host must be a non-empty string")
    parsed = urlsplit(host)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("host must be an http(s) URL")
    hostname = parsed.hostname
    if not hostname or len(hostname) > MAX_HOST_LEN:
        raise ValueError("host has no valid hostname")
    return parsed.scheme, hostname.lower(), parsed.port


def host_allowed(hostname, allowlist):
    """Exact or suffix match against the allowlist. Empty allowlist denies all."""
    if not allowlist:
        return False
    return any(hostname == entry or hostname.endswith("." + entry) for entry in allowlist)


def normalise_egress(payload):
    """Validate + bound the broker request schema. Raises ValueError on misuse."""
    if not isinstance(payload, dict):
        raise ValueError("request must be an object")
    scheme, hostname, port = parse_host(payload.get("host"))
    host = payload.get("host")
    path = payload.get("path", "/")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_PATH_LEN:
        raise ValueError("path must start with '/' and be 1-2048 chars")
    method = str(payload.get("method", "GET")).upper()
    if method not in ALLOWED_METHODS:
        raise ValueError(f"method must be one of {sorted(ALLOWED_METHODS)}")
    headers = payload.get("headers", {}) or {}
    if not isinstance(headers, dict) or len(headers) > MAX_HEADERS:
        raise ValueError(f"headers must be an object of at most {MAX_HEADERS} entries")
    clean_headers = {}
    for name, value in headers.items():
        if (not isinstance(name, str) or not name or len(name) > MAX_HEADER_NAME
                or not isinstance(value, str) or len(value) > MAX_HEADER_VALUE):
            raise ValueError("header names/values must be bounded strings")
        clean_headers[name] = value
    body = payload.get("body")
    if body is not None:
        if not isinstance(body, str) or len(body) > MAX_BODY_BYTES:
            raise ValueError("body must be a string of at most 16384 chars")
    return {
        "host": host, "scheme": scheme, "hostname": hostname, "port": port,
        "path": path, "method": method, "headers": clean_headers, "body": body,
    }


def audit_limit(path):
    raw = parse_qs(urlsplit(path).query).get("limit", ["100"])[0]
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= MAX_AUDIT_EVENTS:
        raise ValueError(f"limit must be from 1 to {MAX_AUDIT_EVENTS}")
    return limit


def egress_events(limit):
    return audit_store().events(limit)


def request_json(connection, method, path, payload, headers):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    connection.request(method, path, body=body, headers=headers)
    reply = connection.getresponse()
    raw = reply.read(MAX_BODY_BYTES * 32 + 1)
    if len(raw) > MAX_BODY_BYTES * 32:
        raise ValueError("egress-out response exceeded broker limit")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = {"error": "egress-out returned non-JSON"}
    return reply.status, decoded, len(raw)


class Handler(BaseHTTPRequestHandler):
    server_version = "egress-broker/0.1"

    def _send(self, code, body):
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, code, content_type, data):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length")
        if length < 1 or length > MAX_BODY_BYTES:
            raise ValueError(f"request body must be 1-{MAX_BODY_BYTES} bytes")
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON") from exc

    def _bearer(self):
        received = self.headers.get("Authorization", "")
        if received.startswith("Bearer "):
            received = received[7:]
        return received

    def _header_token(self, header):
        return self.headers.get(header, "")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            try:
                if ROLE in ("broker", "audit-api"):
                    audit_store().health()
                self._send(200, {"status": "ok", "role": ROLE})
            except Exception:
                self._send(503, {"status": "unavailable", "role": ROLE})
            return
        if ROLE == "audit-api" and path in ("/", "/index.html"):
            self._audit_dashboard()
            return
        if ROLE == "audit-api" and path == "/v1/audit":
            self._audit_events()
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if ROLE == "broker" and path == "/v1/egress":
            self._broker_egress()
        elif ROLE == "egress" and path == "/internal/egress":
            self._egress_fetch()
        else:
            self._send(404, {"error": "not found"})

    def _broker_egress(self):
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        tenant_id = os.environ.get("EGRESS_CAPABILITY_ID", "agent-dev")
        key_id = "agent-egress-token"
        try:
            if not safe_equal(self._bearer(), configured("AGENT_EGRESS_TOKEN")):
                self._deny(request_id, tenant_id, key_id, "", "unauthorized", "POST", "/v1/egress")
                self._send(403, {"error": "unauthorized"})
                return
            clean = normalise_egress(self._read_json())
            if not host_allowed(clean["hostname"], profile_hosts()):
                self._deny(request_id, tenant_id, key_id, clean["hostname"],
                           "host_not_allowlisted", clean["method"], clean["path"])
                self._send(403, {"error": "host not allowlisted"})
                return
            audit_store().begin_egress(
                tenant_id, request_id, key_id, clean["host"],
                os.environ.get("EGRESS_PROFILE", "default"),
                clean["method"], clean["path"], clean["headers"],
            )
            parsed = urlsplit(os.environ.get("EGRESS_EGRESS_URL", "http://egress-out:8081"))
            if parsed.scheme != "http" or parsed.hostname != "egress-out" \
                    or parsed.port not in (None, 8081):
                raise RuntimeError("EGRESS_EGRESS_URL must be http://egress-out:8081")
            connection = HTTPConnection(parsed.hostname, parsed.port or 8081, timeout=30)
            forward = {
                "host": clean["host"], "path": clean["path"], "method": clean["method"],
                "headers": clean["headers"], "body": clean["body"],
            }
            status, body, _n = request_json(
                connection, "POST", "/internal/egress", forward,
                {"Content-Type": "application/json",
                 "X-Egress-Egress-Token": configured("EGRESS_EGRESS_TOKEN")},
            )
            duration = round((time.monotonic() - started) * 1000)
            if status == 403:
                audit_store().fail_egress(request_id, "denied_by_egress", duration)
                self._send(403, body if isinstance(body, dict) else {"error": "denied"})
                return
            if status >= 400:
                audit_store().fail_egress(request_id, f"upstream_status_{status}", duration)
                self._send(502, {"error": "egress upstream error", "upstream_status": status})
                return
            upstream_status = body.get("status", status)
            content_type = body.get("content_type", "application/octet-stream")
            try:
                raw = base64.b64decode(body.get("body_b64", ""))
            except Exception:
                raw = b""
            audit_store().complete_egress(request_id, upstream_status, len(raw), duration)
            self._send_bytes(upstream_status, content_type, raw)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except OSError:
            try:
                audit_store().fail_egress(
                    request_id, "upstream_unavailable",
                    round((time.monotonic() - started) * 1000))
            except Exception:
                pass
            self._send(502, {"error": "egress upstream unavailable"})
        except RuntimeError as exc:
            print(f"[egress-{ROLE}] configuration error: {exc}", file=sys.stderr, flush=True)
            self._send(503, {"error": "egress capability unavailable"})
        except Exception as exc:
            print(f"[egress-{ROLE}] audit persistence error: {exc}", file=sys.stderr, flush=True)
            self._send(503, {"error": "egress audit unavailable"})

    def _deny(self, request_id, tenant_id, key_id, target_host, reason, method, path):
        try:
            audit_store().deny_egress(tenant_id, request_id, key_id, target_host, reason, method, path)
        except Exception as exc:
            print(f"[egress-{ROLE}] deny audit write failed: {exc}", file=sys.stderr, flush=True)

    def _egress_fetch(self):
        try:
            if not safe_equal(self._header_token("X-Egress-Egress-Token"),
                              configured("EGRESS_EGRESS_TOKEN")):
                self._send(401, {"error": "unauthorized"})
                return
            clean = normalise_egress(self._read_json())
            if not host_allowed(clean["hostname"], egress_allow_hosts()):
                self._send(403, {"error": "host not allowlisted"})
                return
            scheme = clean["scheme"]
            port = clean["port"] or (443 if scheme == "https" else 80)
            connection = HTTPSConnection(clean["hostname"], port, timeout=30) \
                if scheme == "https" else HTTPConnection(clean["hostname"], port, timeout=30)
            headers = dict(clean["headers"])
            headers.setdefault("Host", clean["hostname"])
            body_bytes = clean["body"].encode("utf-8") if clean["body"] is not None else None
            connection.request(clean["method"], clean["path"], body=body_bytes, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raw = raw[:MAX_RESPONSE_BYTES]
            print(json.dumps({
                "event": "egress_fetch", "at": now(), "destination": clean["hostname"],
                "path": clean["path"], "method": clean["method"], "status": response.status,
                "response_bytes": len(raw),
            }, separators=(",", ":")), flush=True)
            self._send(200, {
                "status": response.status,
                "content_type": response.getheader("Content-Type", "application/octet-stream"),
                "body_b64": base64.b64encode(raw).decode("ascii"),
            })
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except OSError:
            self._send(502, {"error": "upstream unreachable"})
        except RuntimeError as exc:
            print(f"[egress-out] configuration error: {exc}", file=sys.stderr, flush=True)
            self._send(503, {"error": "egress unavailable"})

    def _audit_authorised(self):
        try:
            return safe_equal(self._header_token("X-Egress-Audit-Token"),
                              configured("EGRESS_AUDIT_TOKEN"))
        except RuntimeError as exc:
            print(f"[egress-audit-api] configuration error: {exc}", file=sys.stderr, flush=True)
            self._send(503, {"error": "audit dashboard unavailable"})
            # Signal to the caller that we already responded — returning None
            # here used to let the caller try to send 401 on top of the 503,
            # which raises write-after-headers on the same connection.
            return _ALREADY_RESPONDED

    def _audit_dashboard(self):
        result = self._audit_authorised()
        if result is _ALREADY_RESPONDED:
            return
        if result is not True:
            self._send(401, {"error": "unauthorized"})
            return
        try:
            events = audit_store().events(50)
        except Exception:
            self._send(503, {"error": "audit log unavailable"})
            return
        rows = "".join(
            "<tr><td>{created_at}</td><td>{state}</td><td>{tenant_id}</td>"
            "<td><code>{target_host}</code></td><td>{request_method} {request_path}</td>"
            "<td>{upstream_status}</td><td>{response_bytes}</td><td>{duration_ms}</td></tr>".format(
                created_at=e.get("created_at", ""), state=e.get("state", ""),
                tenant_id=e.get("tenant_id", ""), target_host=e.get("target_host", ""),
                request_method=e.get("request_method", ""), request_path=e.get("request_path", ""),
                upstream_status=e.get("upstream_status", ""), response_bytes=e.get("response_bytes", ""),
                duration_ms=e.get("duration_ms", ""))
            for e in events
        )
        html = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<title>Egress audit &middot; sovereign-node</title></head><body>"
            "<h1>Agent egress audit</h1>"
            "<p>Every brokered egress request, newest first. Denied rows record why.</p>"
            "<table border='1' cellpadding='6'><thead><tr>"
            "<th>Time</th><th>Outcome</th><th>Tenant</th><th>Target host</th>"
            "<th>Request</th><th>Upstream</th><th>Bytes</th><th>Duration ms</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table></body></html>"
        )
        self._send_html(200, html)

    def _audit_events(self):
        result = self._audit_authorised()
        if result is _ALREADY_RESPONDED:
            return
        if result is not True:
            self._send(401, {"error": "unauthorized"})
            return
        try:
            self._send(200, {"events": egress_events(audit_limit(self.path))})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except OSError:
            self._send(503, {"error": "audit log unavailable"})

    def log_message(self, fmt, *args):
        print(f"[egress-{ROLE}] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    if ROLE not in ("broker", "egress", "audit-api"):
        raise SystemExit("EGRESS_ROLE must be broker, egress, or audit-api")
    print(f"[egress-{ROLE}] listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
