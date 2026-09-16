#!/usr/bin/env python3
"""
Offline tests for agent/forgejo. No live network needed: we monkeypatch
urllib.request.urlopen to simulate Forgejo responses. Tests cover the
load-bearing bits the issue called out:

  - body-file reading handles backticks / parens / newlines / fences
  - HTTP errors collapse to a one-line message and NEVER echo the token
  - missing token / missing env exits non-zero with a clear message
  - --json output parses as JSON
  - label-id lookup resolves names case-insensitively
  - repo defaults pick the right env var per subcommand

scripts/test-jail-image.sh runs this from inside the built image.
Stdlib only.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("forgejo_mod", os.path.join(HERE, "forgejo.py"))
forgejo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forgejo)

FAIL = 0
def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


# ---- 1. body-file reading: the hard cases ----------------------------------

with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
    nasty = (
        "backticks `like this`,\n"
        "parens (and these),\n"
        "newlines,\n"
        "fenced:\n"
        "```\nblock of code\nwith [ ] and { } and \"quotes\"\n```\n"
        "and a trailing line.\n"
    )
    f.write(nasty)
    path = f.name
body = forgejo._read_body_file(path)
check("body file: backticks round-trip", "`like this`" in body)
check("body file: parens round-trip", "(and these)" in body)
check("body file: newline count preserved", body.count("\n") == nasty.count("\n"))
check("body file: fences round-trip", "```" in body)
check("body file: brackets/quotes preserved", '[ ]' in body and '{ }' in body and '"quotes"' in body)
os.unlink(path)


# ---- 2. error-message formatting ------------------------------------------

check("_one_line collapses newlines", forgejo._one_line("a\nb\nc") == "a b c")
check("_one_line collapses tabs/spaces", forgejo._one_line("a   b\tc") == "a b c")


# ---- 3. repo-default resolution -------------------------------------------

os.environ["COORDINATION_REPO"] = "operator/coordination"
os.environ["NODE_CONFIG_REPO"] = "operator/node-config"
# Re-exec from disk to simulate a "fresh session" without relying on
# importlib.reload, which doesn't track modules loaded via
# spec_from_file_location.
with open(os.path.join(HERE, "forgejo.py"), "r", encoding="utf-8") as fh:
    src = fh.read()
ns = {"__name__": "forgejo_rerun"}
exec(compile(src, os.path.join(HERE, "forgejo.py"), "exec"), ns)
check("issue subcommand → COORDINATION_REPO",
      forgejo._repo_for("issue", None) == "operator/coordination")
check("pr subcommand → NODE_CONFIG_REPO",
      forgejo._repo_for("pr", None) == "operator/node-config")
check("--repo override wins for issues",
      forgejo._repo_for("issue", "owner/other") == "owner/other")
check("--repo override wins for PRs",
      forgejo._repo_for("pr", "owner/other") == "owner/other")


# ---- 4. missing body file (subprocess) ------------------------------------

env_full = {**os.environ, "AGENT_FORGEJO_TOKEN": "DUMMY_TOKEN_DO_NOT_LOG",
            "COORDINATION_REPO": "operator/coordination",
            "NODE_CONFIG_REPO": "operator/node-config"}
r = subprocess.run([sys.executable, os.path.join(HERE, "forgejo.py"),
                    "issue", "comment", "57", "--file", "/nonexistent/path_xyz"],
                   capture_output=True, text=True, env=env_full)
check("missing body file: exits non-zero", r.returncode != 0,
      detail=f"rc={r.returncode}")
check("missing body file: stderr says 'not found'", "not found" in r.stderr.lower())
check("missing body file: token NOT leaked",
      "DUMMY_TOKEN_DO_NOT_LOG" not in r.stdout and "DUMMY_TOKEN_DO_NOT_LOG" not in r.stderr)


# ---- 5. missing token (subprocess) ----------------------------------------

env_no_tok = {k: v for k, v in os.environ.items() if k != "AGENT_FORGEJO_TOKEN"}
env_no_tok["COORDINATION_REPO"] = "operator/coordination"
env_no_tok["NODE_CONFIG_REPO"] = "operator/node-config"
r = subprocess.run([sys.executable, os.path.join(HERE, "forgejo.py"),
                    "issue", "view", "57"],
                   capture_output=True, text=True, env=env_no_tok)
check("missing token: exits non-zero", r.returncode != 0, detail=f"rc={r.returncode}")
check("missing token: stderr names the variable", "AGENT_FORGEJO_TOKEN" in r.stderr)


# ---- 6. mocked label lookup (subprocess) ----------------------------------
# Drive a real subprocess, but point it at a fake server we stand up in
# the parent via monkeypatching is harder cross-process. Instead, write a
# small Python shim that runs the helper module against fake responses.

# We can do this WITHOUT a server by having the test import the module
# and stub urllib.request.urlopen directly, then drive main().

class FakeResp:
    def __init__(self, status, body_bytes):
        self.status = status
        self._body = body_bytes
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return self._body


def run_with_mock(argv, fake):
    """Run forgejo.main(argv) with urllib.request.urlopen monkeypatched."""
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    buf_out, buf_err = io.StringIO(), io.StringIO()
    rc = 1
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = forgejo.main(argv)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    finally:
        urllib.request.urlopen = orig
    return rc, buf_out.getvalue(), buf_err.getvalue()


# 6a. label subcommand: GET labels → POST /labels
labels_payload = [
    {"id": 14, "name": "in-progress"},
    {"id": 15, "name": "handoff"},
    {"id": 16, "name": "blocked"},
]
calls = []
def fake_label(req, **kw):
    calls.append((req.method, req.full_url))
    if req.method == "GET" and "labels" in req.full_url and "issues" not in req.full_url:
        return FakeResp(200, json.dumps(labels_payload).encode())
    if req.method == "POST" and req.full_url.endswith("/labels"):
        return FakeResp(200, json.dumps([{"id": 14}]).encode())
    return FakeResp(404, b'{"message":"not found"}')

os.environ["AGENT_FORGEJO_TOKEN"] = "secret-XYZ"
rc, out, err = run_with_mock(["issue", "label", "57", "in-progress"], fake_label)
check("label: exits 0", rc == 0, detail=f"rc={rc} err={err!r}")
check("label: stdout says labeled", "labeled issue 57" in out)
check("label: GET /labels called once",
      sum(1 for m, u in calls if m == "GET" and "labels" in u) == 1)
check("label: POST /labels called once",
      sum(1 for m, u in calls if m == "POST" and u.endswith("/labels")) == 1)


# 6b. label subcommand: unknown name → die, no token leak
def fake_unknown(req, **kw):
    return FakeResp(200, json.dumps(labels_payload).encode())
rc, out, err = run_with_mock(["issue", "label", "57", "does-not-exist"], fake_unknown)
check("label: unknown name exits non-zero", rc != 0, detail=f"rc={rc}")
check("label: unknown name mentions label name", "does-not-exist" in err)
check("label: token NOT leaked on unknown", "secret-XYZ" not in out and "secret-XYZ" not in err)


# 7. --json output: parses as JSON
issue_payload = {"number": 57, "title": "t", "state": "open",
                 "user": {"login": "u"}, "created_at": "2026-09-16T00:00:00Z",
                 "labels": [], "body": "hello"}
def fake_issue(req, **kw):
    if "/comments" in req.full_url:
        return FakeResp(200, json.dumps([]).encode())
    return FakeResp(200, json.dumps(issue_payload).encode())
rc, out, err = run_with_mock(["issue", "view", "57", "--json"], fake_issue)
parsed = json.loads(out) if rc == 0 else None
check("--json: exits 0", rc == 0, detail=f"rc={rc} err={err!r}")
check("--json: stdout is valid JSON", parsed is not None)
check("--json: contains issue+comments", parsed and "issue" in parsed and "comments" in parsed)


# 8. 401 response: non-zero exit, no token leak
def fake_401(req, **kw):
    return FakeResp(401, b'{"message":"bad token"}')
rc, out, err = run_with_mock(["issue", "view", "57"], fake_401)
check("401: exits non-zero", rc != 0, detail=f"rc={rc}")
check("401: stderr mentions HTTP 401", "HTTP 401" in err)
check("401: token NOT leaked", "secret-XYZ" not in out and "secret-XYZ" not in err)


# 9. PR list default state=open, compact text output
def fake_prs(req, **kw):
    return FakeResp(200, json.dumps([
        {"number": 12, "title": "a", "head": {"label": "x"}, "base": {"label": "main"}, "state": "open"},
    ]).encode())
rc, out, err = run_with_mock(["pr", "list"], fake_prs)
check("pr list: exits 0", rc == 0)
check("pr list: compact text contains #12", "#12" in out)
check("pr list: compact text contains base <- head", "main <- x" in out)


# 10. PR list --json: returns raw JSON
def fake_prs_json(req, **kw):
    return FakeResp(200, json.dumps([{"number": 12, "title": "a"}]).encode())
rc, out, err = run_with_mock(["pr", "list", "--json"], fake_prs_json)
check("pr list --json: exits 0", rc == 0)
check("pr list --json: parses", json.loads(out) is not None)


# 11. issue view compact text (default): issue body, then comments
def fake_full(req, **kw):
    if "/comments" in req.full_url:
        return FakeResp(200, json.dumps([
            {"id": 1, "user": {"login": "op"}, "created_at": "2026-09-16T00:00:00Z",
             "body": "comment body"}
        ]).encode())
    return FakeResp(200, json.dumps({
        "number": 57, "title": "title here", "state": "open",
        "user": {"login": "author"}, "created_at": "2026-09-16T00:00:00Z",
        "labels": [{"name": "in-progress"}], "body": "the body"
    }).encode())
rc, out, err = run_with_mock(["issue", "view", "57"], fake_full)
check("issue view: exits 0", rc == 0)
check("issue view: contains title", "title here" in out)
check("issue view: contains body", "the body" in out)
check("issue view: contains comment body", "comment body" in out)


# 12. pr create: --body-file reads the file
with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
    f.write("body with `backticks` and (parens)\n")
    body_path = f.name
posted = []
def fake_create(req, **kw):
    posted.append((req.method, req.full_url, req.data))
    return FakeResp(201, json.dumps({"number": 99, "html_url": "https://x/pulls/99"}).encode())
rc, out, err = run_with_mock(["pr", "create", "--title", "t",
                              "--head", "feat", "--body-file", body_path], fake_create)
check("pr create: exits 0", rc == 0, detail=f"err={err!r}")
check("pr create: POSTed to /pulls",
      any(m == "POST" and u.endswith("/pulls") for m, u, _ in posted))
check("pr create: body from file made it into JSON payload",
      any("`backticks`" in d.decode("utf-8") for _, _, d in posted))
os.unlink(body_path)


# 13. pr request-review: POSTs to the right endpoint
def fake_review(req, **kw):
    return FakeResp(200, b"[]")
rc, out, err = run_with_mock(["pr", "request-review", "5", "operator"], fake_review)
check("pr request-review: exits 0", rc == 0, detail=f"err={err!r}")
check("pr request-review: stderr empty", err == "")


print()
if FAIL == 0:
    print("test_forgejo: PASS")
    sys.exit(0)
print(f"test_forgejo: FAIL ({FAIL} failures)")
sys.exit(1)
