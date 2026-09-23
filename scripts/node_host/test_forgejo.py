#!/usr/bin/env python3
"""
Offline tests for node_host.forgejo — the curl `A()` wrapper, in Python.

  - request shaping: URL under /api/v1/repos/<repo>/, token + JSON headers,
    the exact payloads of comment / close / claim / release
  - curl -s semantics: an HTTP error still returns its body; an unreachable
    server returns "" for reads and a curl exit status for writes
  - label_id: found / absent / unreadable (raises, as the bash's python did)
  - the pinned transport, against real sockets on 127.0.0.1 and no network:
      * it connects to the PIN address, whatever the URL's host resolves to,
        and presents the URL's host as TLS SNI (read out of the ClientHello,
        which is plaintext) — that is what `--resolve host:443:127.0.0.1` did
      * nothing listening -> Unreachable, curl code 7
      * a peer that is not TLS -> Unreachable, curl code 35

Run:  python3 scripts/node_host/test_forgejo.py   (from the repo root)
"""

import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_host import forgejo                                   # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


class Recorder:
    def __init__(self, answer=(200, b"[]"), down=False):
        self.calls, self.answer, self.down = [], answer, down

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        if self.down:
            raise forgejo.Unreachable(7, "down")
        return self.answer


print("request shaping")
rec = Recorder()
api = forgejo.Forgejo("node.example", "s3cret", "operator/coordination", rec)
api.get_text("issues?state=open&type=issues&limit=50")
m, url, headers, body = rec.calls[-1]
check("GET under the repo API", m == "GET" and url ==
      "https://git.node.example/api/v1/repos/operator/coordination/issues?state=open&type=issues&limit=50")
check("token header", headers["Authorization"] == "token s3cret")
check("JSON content type (curl sent it on every call)", headers["Content-Type"] == "application/json")
api.comment(12, 'a "quoted" body\n')
check("comment payload", rec.calls[-1][:2] == ("POST", api.url("issues/12/comments"))
      and rec.calls[-1][3] == b'{"body": "a \\"quoted\\" body\\n"}')
api.close(12)
check("close payload", rec.calls[-1][0] == "PATCH" and rec.calls[-1][3] == b'{"state": "closed"}')
api.add_label(12, "7")
check("claim payload is an integer id", rec.calls[-1][3] == b'{"labels": [7]}')
api.remove_label(12, "7")
check("release is a DELETE with no body", rec.calls[-1][0] == "DELETE"
      and rec.calls[-1][1].endswith("/issues/12/labels/7") and rec.calls[-1][3] is None)

print("curl -s semantics")
err = forgejo.Forgejo("d", "t", "o/r", Recorder(answer=(404, b'{"message":"nope"}')))
check("an HTTP error still yields its body", err.get_text("x") == '{"message":"nope"}')
check("an HTTP error on a write is status 0 (nobody looked)", err.send("POST", "x", {}) == 0)
down = forgejo.Forgejo("d", "t", "o/r", Recorder(down=True))
check("unreachable read -> ''", down.get_text("x") == "")
check("unreachable write -> curl status 7", down.comment(1, "b") == 7)
check("fetch reports reachability", forgejo.fetch(Recorder(down=True), "u", {}) == ("", False)
      and forgejo.fetch(Recorder(), "u", {}) == ("[]", True))

print("label_id")
labels = '[{"id": 3, "name": "task-request"}, {"id": 7, "name": "in-progress"}, {"id": 8, "name": "in-progress"}]'
check("found -> first id, as a string", forgejo.label_id(labels, "in-progress") == "7")
check("absent -> ''", forgejo.label_id(labels, "blocked") == "")
for bad in ("", '{"message": "x"}', "[1]"):
    try:
        forgejo.label_id(bad, "x")
        check(f"unreadable {bad!r} raises", False)
    except (ValueError, TypeError, KeyError):
        check(f"unreadable {bad!r} raises", True)

print("pinned transport (loopback sockets only)")


def serve_once(reply=b""):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def run():
        conn, _ = srv.accept()
        conn.settimeout(5)
        try:
            got["hello"] = conn.recv(4096)
            if reply:
                conn.sendall(reply)
        finally:
            conn.close()
            srv.close()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return srv.getsockname()[1], got, th


port, got, th = serve_once(reply=b"HTTP/1.1 200 OK\r\n\r\nnot tls")
try:
    forgejo.pinned_transport("GET", f"https://git.pinned.invalid:{port}/api/v1/version",
                             {}, None, 5)
    check("a non-TLS peer is an error", False)
except forgejo.Unreachable as exc:
    check("a non-TLS peer -> curl code 35", exc.code == forgejo.CURL_SSL, exc.code)
th.join(5)
check("connected to the pin (127.0.0.1) although the host does not resolve",
      "hello" in got)
check("presented the URL's host as SNI", b"git.pinned.invalid" in got.get("hello", b""))

closed = socket.socket()
closed.bind(("127.0.0.1", 0))
free_port = closed.getsockname()[1]
closed.close()
try:
    forgejo.pinned_transport("GET", f"https://git.x.invalid:{free_port}/", {}, None, 5)
    check("nothing listening is an error", False)
except forgejo.Unreachable as exc:
    check("nothing listening -> curl code 7", exc.code == forgejo.CURL_COULDNT_CONNECT, exc.code)

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
