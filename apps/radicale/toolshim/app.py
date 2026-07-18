#!/usr/bin/env python3
"""radicale-toolshim: the chat-facing tool surface for Radicale (CalDAV).

Open WebUI registers this as an OpenAPI tool server (wired declaratively by
scripts/chat-tools-setup.sh from manifest/radicale.toml [expose.chat]); models
then call the operations below mid-conversation.

Upstream credential: a Radicale Basic-auth user (minted by
chat-tools-setup.sh into secrets/radicale.env as RADICALE_TOOL_USER/PASSWORD),
scoped by Radicale's rights file to the operator's calendar. stdlib only — no
third-party deps.
"""
import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid

RADICALE_URL = os.environ.get("RADICALE_URL", "http://radicale:5232").rstrip("/")
RADICALE_USER = os.environ.get("RADICALE_TOOL_USER", "")
RADICALE_PASS = os.environ.get("RADICALE_TOOL_PASSWORD", "")
# Calendar collection path (CalDAV collection URL, no trailing slash)
CAL_PATH = os.environ.get(
    "RADICALE_TOOL_CAL_PATH",
    f"/{RADICALE_USER or 'operator'}/calendar"
).rstrip("/")
PORT = int(os.environ.get("PORT", "8100"))

OPENAPI = {
    "openapi": "3.1.0",
    "info": {
        "title": "radicale-tools",
        "version": "1.0.0",
        "description": "Read+create events on the operator's calendar (Radicale)",
    },
    "paths": {
        "/events/today": {
            "get": {
                "operationId": "today",
                "summary": "List today's events (local time)",
                "parameters": [
                    {"name": "limit", "in": "query", "required": False,
                     "schema": {"type": "integer", "default": 50, "maximum": 200},
                     "description": "Maximum events to return"},
                ],
                "responses": {"200": {"description": "Events",
                    "content": {"application/json": {"schema": {"type": "object"}}}}},
            }
        },
        "/events/list": {
            "get": {
                "operationId": "list_events",
                "summary": "List events in a time range",
                "parameters": [
                    {"name": "start", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "description": "ISO8601 start (default: now)"},
                    {"name": "end", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "description": "ISO8601 end (default: +7 days)"},
                    {"name": "limit", "in": "query", "required": False,
                     "schema": {"type": "integer", "default": 200, "maximum": 500},
                     "description": "Max events to return"},
                ],
                "responses": {"200": {"description": "Events",
                    "content": {"application/json": {"schema": {"type": "object"}}}}},
            }
        },
        "/events/create": {
            "post": {
                "operationId": "create_event",
                "summary": "Create a calendar event",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["summary", "start", "end"],
                            "properties": {
                                "summary": {"type": "string"},
                                "start": {"type": "string", "description": "ISO8601"},
                                "end": {"type": "string", "description": "ISO8601"},
                                "description": {"type": "string"},
                            },
                        }
                    }}
                },
                "responses": {"200": {"description": "Created",
                    "content": {"application/json": {"schema": {"type": "object"}}}}},
            }
        },
    },
}


def _auth_header():
    if RADICALE_USER and RADICALE_PASS:
        token = base64.b64encode(f"{RADICALE_USER}:{RADICALE_PASS}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def _http(method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    headers = {"User-Agent": "radicale-toolshim/1.0", **(_auth_header()), **(headers or {})}
    url = f"{RADICALE_URL}{path}"
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.getcode(), resp.read(), dict(resp.headers)


# ---- iCalendar parsing (minimal, stdlib) ------------------------------------

def _unfold_ical(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    for line in lines:
        if not line:
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if out:
                out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse_props(lines: list[str], start_idx: int) -> tuple[dict, int]:
    props = {}
    i = start_idx
    while i < len(lines):
        line = lines[i]
        if line == "END:VEVENT":
            return props, i
        if ":" in line:
            key_params, val = line.split(":", 1)
            key = key_params.split(";", 1)[0].upper()
            props.setdefault(key, []).append(val)
        i += 1
    return props, i


def _parse_datetime(val: str) -> str:
    # Return ISO8601 Z when possible; accept DATE (YYYYMMDD) as all-day
    val = val.strip()
    if val.endswith("Z") and len(val) in (16, 15):  # 20260718T090000Z
        try:
            t = dt.datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
            return t.isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    if len(val) == 8 and val.isdigit():  # 20260718 (all-day)
        try:
            d = dt.datetime.strptime(val, "%Y%m%d").date()
            return d.isoformat()
        except Exception:
            pass
    # Fallback: return as-is
    return val


def _slim_event(props: dict, href: str | None = None) -> dict:
    dtstart = _parse_datetime((props.get("DTSTART") or [""])[0])
    dtend = _parse_datetime((props.get("DTEND") or [""])[0])
    summary = (props.get("SUMMARY") or [""])[0]
    desc = (props.get("DESCRIPTION") or [""])[0]
    return {"summary": summary, "start": dtstart, "end": dtend, "description": desc, "href": href}


def parse_ics(text: str) -> list[dict]:
    lines = _unfold_ical(text)
    events = []
    i = 0
    while i < len(lines):
        if lines[i] == "BEGIN:VEVENT":
            props, i = _parse_props(lines, i + 1)
            events.append(_slim_event(props))
        else:
            i += 1
    return events


def ics_in_range(text: str, start: dt.datetime, end: dt.datetime, limit: int) -> list[dict]:
    def to_dt(s: str) -> dt.datetime | None:
        if not s:
            return None
        try:
            if len(s) == 10 and s[4] == '-' and s[7] == '-':  # YYYY-MM-DD
                d = dt.datetime.strptime(s, "%Y-%m-%d").date()
                return dt.datetime.combine(d, dt.time.min, tzinfo=dt.timezone.utc)
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    out = []
    for ev in parse_ics(text):
        s = to_dt(ev.get("start", ""))
        e = to_dt(ev.get("end", ""))
        if s is None:
            continue
        # If end missing, treat as instant; if all-day, end may be next day — include if overlaps
        e = e or s
        if not (e <= start or s >= end):
            out.append(ev)
        if len(out) >= limit:
            break
    return out


# ---- CalDAV helpers ----------------------------------------------------------

CAL_QUERY_XML = """<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:D="DAV:">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start}" end="{end}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""


def list_events_range(start_iso: str | None, end_iso: str | None, limit: int) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    start = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00")) if start_iso else now
    end = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else (start + dt.timedelta(days=7))
    # Prefer CalDAV REPORT; fall back to GET of a collection .ics
    try:
        body = CAL_QUERY_XML.format(start=start.strftime("%Y%m%dT%H%M%SZ"), end=end.strftime("%Y%m%dT%H%M%SZ")).encode()
        code, data, hdr = _http("REPORT", CAL_PATH + "/", body=body, headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        if code in (207, 200):
            # Very small XML pull: find all <C:calendar-data>...</C:calendar-data>
            txt = data.decode("utf-8", "ignore")
            events = []
            pos = 0
            while True:
                a = txt.find("<C:calendar-data", pos)
                if a == -1:
                    a = txt.find("<cal:calendar-data", pos)  # some servers use prefix cal
                    if a == -1:
                        break
                a = txt.find(">", a)
                b = txt.find("</", a)
                if a == -1 or b == -1:
                    break
                ics = txt[a+1:b]
                events.extend(parse_ics(ics))
                pos = b + 1
            # Filter by time (server should already do it, but be safe) and cap
            def to_dt(s: str) -> dt.datetime:
                try:
                    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
                except Exception:
                    return now
            events.sort(key=lambda e: to_dt(e.get("start", "9999-12-31T00:00:00Z")))
            return {"range": {"start": start.isoformat().replace("+00:00", "Z"),
                               "end": end.isoformat().replace("+00:00", "Z")},
                    "events": events[:limit]}
    except Exception as e:
        # Fall back to GET of a single-file collection (calendar.ics)
        pass
    try:
        code, data, _ = _http("GET", f"{CAL_PATH}.ics")
        if code == 200:
            text = data.decode("utf-8", "ignore")
            events = ics_in_range(text, start, end, limit)
            return {"range": {"start": start.isoformat().replace("+00:00", "Z"),
                               "end": end.isoformat().replace("+00:00", "Z")},
                    "events": events}
    except Exception as e:
        return {"error": f"radicale upstream failed: {e}"}
    return {"range": {"start": start.isoformat().replace("+00:00", "Z"),
                       "end": end.isoformat().replace("+00:00", "Z")},
            "events": []}


def create_event(summary: str, start_iso: str, end_iso: str, description: str | None) -> dict:
    def to_ical_dt(s: str) -> str:
        try:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
            return t.strftime("%Y%m%dT%H%M%SZ")
        except Exception:
            # Try DATE (all-day)
            try:
                d = dt.datetime.strptime(s, "%Y-%m-%d").date()
                return d.strftime("%Y%m%d")
            except Exception:
                raise ValueError("invalid datetime format; expected ISO8601 or YYYY-MM-DD")

    uid = str(uuid.uuid4())
    dtstamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    DTSTART = to_ical_dt(start_iso)
    DTEND = to_ical_dt(end_iso)
    def fold(s: str) -> str:
        # 75 octets per RFC5545; ASCII inputs here, so 75 chars is fine
        lines = []
        s = s.replace("\r", "").replace("\n", "\\n")
        while s:
            lines.append(s[:75])
            s = s[75:]
        return "\r\n ".join(lines) if lines else ""

    ics = ("BEGIN:VCALENDAR\r\n"
           "VERSION:2.0\r\n"
           "PRODID:-//sovereign-node//radicale-toolshim//EN\r\n"
           "BEGIN:VEVENT\r\n"
           f"UID:{uid}\r\n"
           f"DTSTAMP:{dtstamp}\r\n"
           f"DTSTART:{DTSTART}\r\n"
           f"DTEND:{DTEND}\r\n"
           f"SUMMARY:{fold(summary)[:512]}\r\n"
           + (f"DESCRIPTION:{fold(description)[:2048]}\r\n" if description else "") +
           "END:VEVENT\r\nEND:VCALENDAR\r\n")

    path = f"{CAL_PATH}/{uid}.ics"
    code, data, hdr = _http("PUT", path, body=ics.encode("utf-8"),
                            headers={"Content-Type": "text/calendar; charset=utf-8",
                                     "If-None-Match": "*"})
    if code in (200, 201, 204):
        href = hdr.get("Content-Location", path)
        return {"ok": True, "uid": uid, "href": href}
    raise RuntimeError(f"upstream returned {code}: {data[:200]!r}")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(url.query))
        try:
            if url.path == "/openapi.json":
                self.send_json(OPENAPI)
                return
            if url.path == "/healthz":
                # Probe minimal upstream reachability without creating anything
                ok = True
                try:
                    code, _, _ = _http("PROPFIND", CAL_PATH + "/", headers={"Depth": "0"})
                    ok = code in (207, 200, 401)  # auth may be required
                except Exception:
                    ok = False
                self.send_json({
                    "status": "ok",
                    "upstream": ok,
                    "auth": bool(RADICALE_USER and RADICALE_PASS),
                    "cal_path": CAL_PATH,
                })
                return
            if url.path == "/events/today":
                limit = max(1, min(int(params.get("limit", "50") or 50), 200))
                now = dt.datetime.now().astimezone()
                start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=now.tzinfo).astimezone(dt.timezone.utc)
                end = start + dt.timedelta(days=1)
                res = list_events_range(start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"), limit)
                self.send_json(res)
                return
            if url.path == "/events/list":
                start = params.get("start")
                end = params.get("end")
                limit = max(1, min(int(params.get("limit", "200") or 200), 500))
                res = list_events_range(start, end, limit)
                self.send_json(res)
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            if self.path == "/events/create":
                if not (RADICALE_USER and RADICALE_PASS):
                    self.send_json({"error": "write requires RADICALE_TOOL_USER/PASSWORD"}, 400)
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b"{}"
                data = json.loads(body.decode() or "{}")
                summary = (data.get("summary") or "").strip()
                start = (data.get("start") or "").strip()
                end = (data.get("end") or "").strip()
                description = data.get("description")
                if not (summary and start and end):
                    self.send_json({"error": "summary, start, end are required"}, 400)
                    return
                res = create_event(summary, start, end, description)
                self.send_json(res)
                return
            self.send_json({"error": "not found"}, 404)
        except ValueError as ve:
            self.send_json({"error": str(ve)}, 400)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"radicale-toolshim on :{PORT} -> {RADICALE_URL}{CAL_PATH} "
          f"(auth {'set' if (RADICALE_USER and RADICALE_PASS) else 'MISSING'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
