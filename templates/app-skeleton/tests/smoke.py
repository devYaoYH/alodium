#!/usr/bin/env python3
"""Smoke tests the change pipeline runs against a STAGING instance
(app.toml [tests]). $APP_URL is injected by the harness; defaults suit
local runs. Stdlib only. Extend with every endpoint you add — the
pipeline refuses promotion on red.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("APP_URL", "http://localhost:8080").rstrip("/")


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return r.status, json.load(r)


def post(path, body):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.load(r)


def main():
    status, body = get("/healthz")
    assert status == 200 and body.get("status") == "ok", f"healthz: {status} {body}"

    status, body = post("/v1/echo", {"message": "ping"})
    assert status == 200 and body.get("echo", {}).get("message") == "ping", (
        f"echo: {status} {body}"
    )

    print("smoke: all passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"smoke: FAILED — {e}", file=sys.stderr)
        sys.exit(1)
