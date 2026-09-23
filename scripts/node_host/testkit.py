"""
testkit — the scenario worlds the host-job equivalence tests replay.

The bash dispatcher, dispatch-run and deploy-watch could not be A/B-tested
against the live node (that would mean filing issues at it and deploying), so
their behavior was RECORDED instead: each scenario in
node_dispatch/testdata/scenarios.json and node_deploy/testdata/watch_scenarios.json
was built into a throwaway checkout by THIS module, the bash was executed in it
against a fake Forgejo / docker / run-task, and what it did was written to the
bash_baseline files. The Python replays the same worlds, through the same
route semantics, and must do the same things.

Keeping world-building and the fake Forgejo in one module is the point: the
recorder and the replay cannot drift apart on what a scenario means.

Not a test itself (no test_ prefix); imported by the test_*.py files.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

STATE = ".task-dispatch"
_TS_LOCAL = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}")
_TS_UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


# --- the fake Forgejo (and LiteLLM) ------------------------------------------

class Routes:
    """Answers a request from a scenario's route table.

    Route keys are 'METHOD path' with path relative to
    /api/v1/repos/<COORDINATION_REPO>/ on git.<domain>, or 'GET llm:/v1/models'
    on llm.<domain>. A value is a JSON body (200), {status, body}, or "down".
    """

    def __init__(self, spec):
        self.routes = spec.get("routes", {})
        self.down = bool(spec.get("down"))
        env = spec["env"]
        self.git_base = f"https://git.{env['NODE_DOMAIN']}/api/v1/repos/{env['COORDINATION_REPO']}/"
        self.llm_base = f"https://llm.{env['NODE_DOMAIN']}"

    def key(self, method, url):
        if url.startswith(self.git_base):
            return f"{method} {url[len(self.git_base):]}", "git"
        if url.startswith(self.llm_base):
            return f"{method} llm:{url[len(self.llm_base):]}", "llm"
        return f"{method} {url}", "other"

    def answer(self, method, url):
        """(status, body bytes), or None when there is no response at all."""
        key, host = self.key(method, url)
        if host == "git" and self.down:
            return None
        value = self.routes.get(key)
        if value == "down":
            return None
        if value is None:
            if method == "GET":
                return 404, b'{"message":"not found"}'
            return 201, b"{}"
        status, body = 200, value
        if isinstance(value, dict) and set(value) == {"status", "body"}:
            status, body = value["status"], value["body"]
        if not isinstance(body, str):
            body = json.dumps(body)
        return status, body.encode()


def call_record(method, key, body):
    """How a request is written into an outcome: [method, key, parsed body]."""
    if body in (None, b"", ""):
        parsed = None
    else:
        text = body.decode() if isinstance(body, bytes) else body
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = text
    return [method, key, parsed]


class FakeTransport:
    """node_host.forgejo's transport, answering from Routes and recording."""

    def __init__(self, routes: Routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        from node_host.forgejo import Unreachable, CURL_COULDNT_CONNECT
        key, _ = self.routes.key(method, url)
        self.calls.append(call_record(method, key, body))
        answer = self.routes.answer(method, url)
        if answer is None:
            raise Unreachable(CURL_COULDNT_CONNECT, "fake: down")
        return answer


# --- the fake host (for the Python side) ---------------------------------------

class FakeHost:
    """node_host.host.Host with every side effect recorded, none performed."""

    def __init__(self, spec, repo_root):
        self.spec = spec
        self.repo_root = str(repo_root)
        self.run_task = []
        self.spawns = []
        self.trace_render = []
        self.deploys = 0
        self.sleeps = 0

    def script(self, name):
        return [f"<{name}>"]

    def run_combined(self, argv):
        name = argv[0]
        if name == "<run-task.sh>":
            self.run_task.append(list(argv[1:]))
            rt = self.spec.get("run_task", {"rc": 0, "output": ""})
            return rt["rc"], rt["output"]
        if name == "<deploy.py>":
            self.deploys += 1
            d = self.spec.get("deploy", {"rc": 0, "output": ""})
            return d["rc"], d["output"]
        raise AssertionError(f"unexpected command {argv}")

    def run_quiet(self, argv):
        assert argv[0] == "<trace-render.py>", argv
        self.trace_render.append(list(argv[1:]))
        return self.spec.get("trace_render_rc", 0)

    def spawn_detached(self, argv, log_path):
        assert argv[0] == "<dispatch_run.py>", argv
        if self.spec.get("spawn_fails"):
            raise OSError("fake: spawn failed")
        self.spawns.append(list(argv[1:]))

    def sleep(self, seconds):
        self.sleeps += 1

    def now(self):
        return time.time()

    def running_tenants(self):
        return int(self.spec.get("ps", 0))

    def image_id(self, image):
        return self.spec.get("image_id") or ""


# --- building a dispatcher / dispatch-run world ---------------------------------

def _age(path: Path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def write_env(path: Path, env: dict):
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))


def build_dispatch_world(tmp: Path, spec: dict, briefs: dict) -> Path:
    """A checkout the dispatcher / dispatch-run can run in. Returns its root."""
    root = tmp / "repo"
    (root / "tasks").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "config").mkdir()
    for name, body in briefs.items():
        (root / "tasks" / f"{name}.md").write_text(body)
    for skill in spec.get("skills", []):
        (root / "skills" / skill).mkdir(parents=True)
        (root / "skills" / skill / "SKILL.md").write_text("x\n")
    if spec.get("tiers"):
        (root / "config" / "dispatch-tiers.yaml").write_text(spec["tiers"])
    for rel, body in spec.get("files", {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    env = dict(spec["env"])
    state = root / STATE
    state.mkdir()
    for name, age in spec.get("stamps", {}).items():
        (state / name).write_text("")
        _age(state / name, age)
    if "lock_age" in spec:
        (state / "pass.lock").mkdir()
        _age(state / "pass.lock", spec["lock_age"])
    if spec.get("spool") is not None:
        spool = tmp / "spool"
        spool.mkdir()
        for name in spec["spool"]:
            (spool / name).write_text("")
        env["DISPATCH_SPOOL"] = str(spool)
    write_env(root / ".env", env)
    return root


def normalize_stdout(text: str) -> list:
    return [_TS_LOCAL.sub("<ts>", line) for line in text.splitlines()]


def dispatch_state(tmp: Path, root: Path) -> dict:
    """What a run left behind: fresh stamps, the audit log, spool, lock."""
    state = root / STATE
    now = time.time()
    touched = sorted(p.name for p in state.iterdir()
                     if p.is_file() and p.name not in ("dispatch-audit.log", "dispatch-run.log")
                     and now - p.stat().st_mtime < 30)
    audit_path = state / "dispatch-audit.log"
    audit = ([_TS_UTC.sub("<ts>", line) for line in audit_path.read_text().splitlines()]
             if audit_path.exists() else [])
    spool = tmp / "spool"
    return {"touched": touched, "audit": audit,
            "spool_left": sorted(os.listdir(spool)) if spool.is_dir() else None,
            "lock_left": (state / "pass.lock").exists()}


# --- building a deploy-watch world ------------------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def git_env(tmp: Path) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp / "home"),
           "LANG": "C", "LC_ALL": "C"}
    env.update(GIT_ENV)
    return env


def _git(root, env, *args, date=None):
    e = dict(env)
    if date:
        e["GIT_AUTHOR_DATE"] = e["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", *args], cwd=root, env=e, check=True,
                          capture_output=True, text=True).stdout.strip()


def build_watch_world(tmp: Path, spec: dict):
    """(root, {name: hash}) — a bare 'forgejo' remote at C3, a checkout at C2."""
    (tmp / "home").mkdir(exist_ok=True)
    env = git_env(tmp)
    remote = tmp / "remote.git"
    root = tmp / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)],
                   env=env, check=True)
    root.mkdir()
    _git(root, env, "init", "-q", "-b", "main")
    names = {}
    for i in (1, 2, 3):
        (root / "README").write_text(f"version {i}\n")
        _git(root, env, "add", "README")
        _git(root, env, "commit", "-q", "-m", f"C{i}", date=f"2026-09-0{i}T00:00:00Z")
        names[f"C{i}"] = _git(root, env, "rev-parse", "HEAD")
    _git(root, env, "remote", "add", "forgejo", str(remote))
    _git(root, env, "push", "-q", "forgejo", "main")
    _git(root, env, "reset", "-q", "--hard", names["C2"])
    if spec.get("local_extra"):
        (root / "LOCAL").write_text("local only\n")
        _git(root, env, "add", "LOCAL")
        _git(root, env, "commit", "-q", "-m", "L1", date="2026-09-05T00:00:00Z")
        names["L1"] = _git(root, env, "rev-parse", "HEAD")
    if spec.get("branch", "main") != "main":
        _git(root, env, "checkout", "-q", "-b", spec["branch"])
    if spec.get("dirty"):
        (root / "README").write_text("edited\n")
    if spec.get("broken_remote"):
        _git(root, env, "remote", "set-url", "forgejo", str(tmp / "nowhere.git"))

    def sub(value):
        if isinstance(value, str):
            for name, h in names.items():
                value = value.replace("{" + name + "}", h)
        return value

    if "deploy_info" in spec:
        info = {k: sub(v) for k, v in spec["deploy_info"].items()}
        path = root / "config/homepage/static/deploy-info.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(info, indent=2) + "\n")
    if "deploy_info_raw" in spec:
        path = root / "config/homepage/static/deploy-info.json"
        path.parent.mkdir(parents=True)
        path.write_text(spec["deploy_info_raw"])
    state = root / STATE
    state.mkdir()
    for name in spec.get("stamps", []):
        (state / sub(name)).write_text("")
    if "lock_age" in spec:
        (state / "deploy-watch.lock").mkdir()
        _age(state / "deploy-watch.lock", spec["lock_age"])
    write_env(root / ".env", spec["env"])
    return root, names


def unhash(text: str, names: dict) -> str:
    """Replace commit hashes (full, 12-char, or git's abbreviation) by name."""
    for name, h in sorted(names.items()):
        text = text.replace(h, "{" + name + "}")
        text = text.replace(h[:12], "{" + name + ".12}")
        text = re.sub(r"\b" + h[:7] + r"[0-9a-f]*\b", "{" + name + ".abbrev}", text)
    return text


def unhash_obj(value, names):
    if isinstance(value, str):
        return unhash(value, names)
    if isinstance(value, list):
        return [unhash_obj(v, names) for v in value]
    if isinstance(value, dict):
        return {k: unhash_obj(v, names) for k, v in value.items()}
    return value


def watch_state(root: Path, names: dict, env: dict) -> dict:
    state = root / STATE
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env,
                          capture_output=True, text=True).stdout.strip()
    return {"stamps_after": sorted(unhash(p.name, names) for p in state.iterdir()
                                   if p.is_file()),
            "head_after": unhash(head, names),
            "lock_left": (state / "deploy-watch.lock").exists()}
