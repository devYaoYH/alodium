#!/usr/bin/env python3
"""Smoke: one check per surface the floor ships. Stdlib only.

Run:  python3 app.py &  APP_URL=http://localhost:8080 python3 tests/smoke.py
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("APP_URL", "http://floor:8080").rstrip("/")
failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL") + f"  {name}")
    if not cond:
        failures.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read()


status, body = get("/healthz")
check("healthz 200 ok", status == 200 and b"ok" in body)

status, body = get("/v1/floor")
floor = json.loads(body)
check("floor 200 json", status == 200)
check("floor has rooms list", isinstance(floor.get("rooms"), list))
check("floor has edges list", isinstance(floor.get("edges"), list))
check("floor reports sources honestly", isinstance(floor.get("sources"), dict))

status, body = get("/v1/activity?since=0")
act = json.loads(body)
check("activity 200 json", status == 200)
check("activity has events + cursor",
      isinstance(act.get("events"), list) and "latest_id" in act)

status, body = get("/")
check("site index served", status == 200 and b"<canvas" in body)

status, body = get("/assets/decor/workshop.svg")
check("fallback sprite served", status == 200 and b"<svg" in body)

status, _ = get("/iso.js")
check("renderer served", status == 200)

# path traversal must not escape the site dir
try:
    status, _ = get("/../app.py")
    check("traversal blocked", status == 404)
except urllib.error.HTTPError as e:
    check("traversal blocked", e.code == 404)

sys.exit(1 if failures else 0)
