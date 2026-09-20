#!/usr/bin/env python3
"""
Offline tests for scripts/deploy.py — the whole sequence, with a fake shell.

Every git/docker/script call is intercepted, recorded and answered from a
table, so these tests drive a complete deploy in milliseconds without a daemon,
without a checkout and without touching a container. What they assert is not
"it ran" but WHICH commands ran and in what order, because on this node the
difference between a correct deploy and a disastrous one is exactly that list.

The branches here are the ones a live run will not reach, and each is a failure
that has actually happened on this node or that the bash was written to catch:

  - nothing changed (PRESERVED DEFECT 1)     - compose config invalid (#93)
  - only config/ changed                     - caddy fails validation
  - git diverged                             - sso-setup fails
  - `compose up` fails, with its exit code   - an image build fails
  - a new profile-gated service is down (PRESERVED DEFECT 3: #123, #128)
  - agent/ changed (PRESERVED DEFECT 2)      - a corrupt previous deploy-info

and the invariant under all of them: a run that aborts writes status=failed and
does NOT advance deployed_commit, so scripts/deploy-watch.sh retries rather
than believing a broken tip is live.

Run:  python3 scripts/node_deploy/test_sequence.py   (from the repo root)
Stdlib only.
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("deploy_entry", SCRIPTS / "deploy.py")
deploy_entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_entry)

HEAD = "1111111111111111111111111111111111111111"
PREV = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


COMPOSE_JSON = {
    "services": {
        "caddy": {"image": "caddy:2", "restart": "unless-stopped"},
        "litellm": {"image": "litellm", "restart": "unless-stopped"},
        "homepage": {"image": "homepage", "restart": "unless-stopped"},
        "memos": {"profiles": ["apps"], "restart": "unless-stopped",
                  "build": {"context": "./apps/memos"},
                  "image": "sovereign-node/memos:local"},
        "memos-db": {"profiles": ["apps"], "restart": "unless-stopped",
                     "image": "postgres:16"},
        "snake": {"profiles": ["on-demand"], "restart": "no",
                  "build": {"context": "./apps/snake"},
                  "image": "sovereign-node/snake:local"},
    }
}
SERVICES = list(COMPOSE_JSON["services"])
# `memosaurus` is a decoy: it starts with "memos" but is a different app.
# If the `^<app>(-|$)` boundary is ever widened to a prefix match, the
# restart assertions below catch it here and not on the node.
RUNNING = ["caddy", "litellm", "homepage", "memos", "memos-db", "memosaurus"]


class Shell:
    """Routes an argv to a canned answer and records every call it saw."""

    def __init__(self, changed=(), compose_diff="", running=RUNNING,
                 overrides=None, images=("sovereign-node/memos:local",
                                         "sovereign-node/snake:local")):
        self.calls = []
        self.changed = list(changed)
        self.compose_diff = compose_diff
        self.running = list(running)
        self.images = set(images)
        self.overrides = overrides or {}

    def key(self, argv):
        text = " ".join(argv)
        for prefix in self.overrides:
            if text.startswith(prefix):
                return prefix
        return None

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        text = " ".join(argv)

        override = self.key(argv)
        if override is not None:
            rc, out, err = self.overrides[override]
            return Proc(rc, out, err)

        if text == "git rev-parse HEAD":
            return Proc(0, HEAD + "\n")
        if text == "git rev-parse --short HEAD":
            return Proc(0, HEAD[:7] + "\n")
        if argv[:3] == ["git", "diff", "--name-only"]:
            return Proc(0, "".join(f"{p}\n" for p in self.changed))
        if argv[:2] == ["git", "diff"]:
            return Proc(0, self.compose_diff)
        if argv[:3] == ["git", "remote", "get-url"]:
            return Proc(1)                      # no origin, the node's real state
        if argv[:4] == ["docker", "compose", "config", "--services"]:
            return Proc(0, "\n".join(SERVICES) + "\n")
        if argv[:4] == ["docker", "compose", "config", "--format"]:
            return Proc(0, json.dumps(COMPOSE_JSON))
        if argv[:4] == ["docker", "compose", "ps", "--services"]:
            return Proc(0, "\n".join(self.running) + "\n")
        if argv[:3] == ["docker", "image", "inspect"]:
            return Proc(0 if argv[3] in self.images else 1)
        if argv[:3] == ["docker", "build", "-t"]:
            log = kw.get("stdout")
            if log is not None and hasattr(log, "write"):
                log.write(b"build output line\n")
            return Proc(0)
        return Proc(0)

    # --- assertions helpers ---
    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def text_calls(self):
        return [" ".join(c) for c in self.calls]


def run_deploy(shell, previous=None):
    """Drive one whole deploy in a throwaway tree. Returns (exit code, info)."""
    root = Path(tempfile.mkdtemp(prefix="deploy-seq."))
    (root / "docker-compose.yml").write_text(
        "services:\n  memos:\n    profiles: [apps]\n  snake:\n"
        "    profiles: [on-demand]\n")
    target = root / deploy_entry.DEPLOY_INFO
    target.parent.mkdir(parents=True, exist_ok=True)
    if previous is not None:
        target.write_text(previous if isinstance(previous, str)
                          else json.dumps(previous))
    dep = deploy_entry.Deploy(repo_root=root, run=shell)
    try:
        code = dep.run()
    except deploy_entry.StepFailed as exc:
        dep.record("ERROR", str(exc))
        dep.write_info(deploy_entry.info.FAILED)
        code = exc.code
    info_doc = json.loads(target.read_text()) if target.exists() else None
    return code, info_doc, dep


PREV_OK = {"timestamp": "t", "commit": PREV, "short_hash": PREV[:7], "url": "u",
           "status": "ok", "deployed_commit": PREV, "messages": []}


# ---- 1. NOTHING CHANGED — PRESERVED DEFECT (1) -----------------------------
#
# The operator ran `git pull` before deploying, so OLD_HEAD == HEAD and the
# diff is empty. The deploy walks its whole sequence, builds nothing, restarts
# nothing, and reports ok. That is the bug, pinned: a fix has to change these
# assertions deliberately.

print("sequence: nothing changed")
sh = Shell(changed=[])
code, doc, dep = run_deploy(sh, PREV_OK)
check("nothing changed: exits 0", code == 0)
check("nothing changed: NOT ONE image is built",
      sh.ran("docker", "compose", "build") == [] and sh.ran("docker", "build") == [])
check("nothing changed: NOT ONE container is restarted",
      sh.ran("docker", "compose", "restart") == [])
check("nothing changed: sso-setup is not run",
      sh.ran("./scripts/sso-setup.sh") == [])
check("nothing changed: it STILL reports ok and advances deployed_commit — "
      "this is PRESERVED DEFECT (1), reproduced on purpose",
      doc["status"] == "ok" and doc["deployed_commit"] == HEAD)
check("nothing changed: compose up still runs, so a drifted container spec is "
      "still reconciled",
      len(sh.ran("docker", "compose", "up")) >= 1)


# ---- 2. only config/ changed ----------------------------------------------

print("sequence: only config/ changed")
sh = Shell(changed=["config/litellm.yaml", "config/homepage/services.yaml"])
code, doc, dep = run_deploy(sh, PREV_OK)
restarted = [c[-1] for c in sh.ran("docker", "compose", "restart")]
check("config only: litellm and homepage restart, in that order",
      restarted == ["litellm", "homepage"], detail=str(restarted))
check("config only: no image is built", sh.ran("docker", "compose", "build") == [])
check("config only: status ok", code == 0 and doc["status"] == "ok")


# ---- 3. an app's build inputs changed --------------------------------------

print("sequence: an app's build inputs changed")
sh = Shell(changed=["apps/memos/Dockerfile", "apps/memos/compose.yaml"])
code, doc, dep = run_deploy(sh, PREV_OK)
check("app change: the image is rebuilt",
      [c[-1] for c in sh.ran("docker", "compose", "build")] == ["memos"])
restarted = [c[-1] for c in sh.ran("docker", "compose", "restart")]
check("app change: its running services are restarted, and ONLY those — "
      "memosaurus is a different app that merely shares a prefix",
      restarted == ["memos", "memos-db"], detail=str(restarted))
check("app change: it is also re-upped for the on-demand case",
      any(c[:4] == ["docker", "compose", "up", "-d"] and "memos" in c
          for c in sh.calls))
check("app change: SSO refreshes, because apps/memos/compose.yaml moved",
      len(sh.ran("./scripts/sso-setup.sh")) == 1)
check("app change: status ok", code == 0 and doc["status"] == "ok")


# ---- 4. metadata-only change must NOT rebuild or restart -------------------

print("sequence: metadata-only app change")
sh = Shell(changed=["apps/memos/compose.yaml", "apps/memos/route.caddy",
                    "apps/memos/env.example"])
code, doc, dep = run_deploy(sh, PREV_OK)
check("metadata only: nothing is built", sh.ran("docker", "compose", "build") == [])
check("metadata only: nothing is restarted",
      sh.ran("docker", "compose", "restart") == [])
check("metadata only: compose up still applies the new spec",
      len(sh.ran("docker", "compose", "up")) >= 1)


# ---- 5. git diverged -------------------------------------------------------

print("sequence: git diverged")
sh = Shell(changed=[], overrides={"git merge --ff-only": (1, "", "diverged")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("diverged: exits 1", code == 1)
check("diverged: NOTHING docker was touched — not one container, not one image",
      not any(c[0] == "docker" for c in sh.calls), detail=str(sh.text_calls()))
check("diverged: no sibling script ran either",
      not any(c[0].startswith("./scripts/") for c in sh.calls))
check("diverged: status failed and deployed_commit does NOT advance",
      doc["status"] == "failed" and doc["deployed_commit"] == PREV)
check("diverged: the message tells the operator it is theirs to reconcile",
      "reconcile by hand" in doc["messages"][0]["text"])


# ---- 6. compose config invalid (#93) ---------------------------------------

print("sequence: compose config invalid")
sh = Shell(changed=["docker-compose.yml"],
           overrides={"docker compose config --services":
                      (1, "", "\n\nservices.memos: duplicate key 'image'\n")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("bad compose: exits 1", code == 1)
check("bad compose: NO container was changed — no up, no restart, no build",
      not sh.ran("docker", "compose", "up") and not sh.ran("docker", "compose", "restart")
      and not sh.ran("docker", "compose", "build"))
check("bad compose: compose's OWN last non-blank line reaches the badge, not a "
      "bare 'step failed' — that is what #93 hid behind a 2>/dev/null",
      "duplicate key 'image'" in doc["messages"][0]["text"],
      detail=doc["messages"][0]["text"])
check("bad compose: status failed, deployed_commit held back",
      doc["status"] == "failed" and doc["deployed_commit"] == PREV)


# ---- 7. `compose up` fails — the exit code must survive --------------------

print("sequence: compose up fails")
sh = Shell(changed=[], overrides={"docker compose up -d --remove-orphans":
                                  (17, "", "unhealthy dependency")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("up fails: the deploy exits with the FAILING command's code, not 1 — "
      "deploy-watch.sh reports that number in the blocked issue it files",
      code == 17, detail=str(code))
check("up fails: the failing command is named in the badge",
      "docker compose up -d --remove-orphans" in doc["messages"][0]["text"])
check("up fails: status failed, deployed_commit held back so the watcher retries",
      doc["status"] == "failed" and doc["deployed_commit"] == PREV)
check("up fails: it stopped there — no restart pass ran",
      sh.ran("docker", "compose", "restart") == [])


# ---- 8. the caddy health gate ----------------------------------------------

print("sequence: caddy fails validation")
sh = Shell(changed=[], overrides={"docker compose exec -T caddy caddy validate":
                                  (1, "", "bad route")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("caddy invalid: the reload is NOT attempted",
      not any("reload" in " ".join(c) for c in sh.calls))
check("caddy invalid: it is a WARN, so the deploy finishes",
      code == 0 and doc["status"] == "warning")
check("caddy invalid: deployed_commit advances — a warning run IS deployed",
      doc["deployed_commit"] == HEAD)
check("caddy invalid: the badge says which route to fix",
      "NOT reloading" in doc["messages"][0]["text"])

print("sequence: caddy reload fails after validating")
sh = Shell(changed=[], overrides={"docker compose exec -T caddy caddy reload":
                                  (9, "", "")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("caddy reload fails: FATAL, not a warning — Caddy is not serving the "
      "merged routes and cannot say why",
      code == 9 and doc["status"] == "failed")


# ---- 9. sso-setup fails ----------------------------------------------------

print("sequence: sso-setup fails")
sh = Shell(changed=["caddy/Caddyfile"],
           overrides={"./scripts/sso-setup.sh": (1, "", "POCKET_ID_API_KEY expired\n")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("sso fails: exits 1", code == 1)
check("sso fails: the reason reaches the badge, not just the log",
      "POCKET_ID_API_KEY expired" in doc["messages"][0]["text"])
check("sso fails: status failed, deployed_commit held back",
      doc["status"] == "failed" and doc["deployed_commit"] == PREV)
check("sso fails: it aborted BEFORE the caddy reload and the restart pass",
      not any("caddy reload" in " ".join(c) for c in sh.calls)
      and sh.ran("docker", "compose", "restart") == [])


# ---- 10. an image build fails ----------------------------------------------

print("sequence: an image build fails")
sh = Shell(changed=["apps/memos/Dockerfile"],
           overrides={"docker compose build memos": (2, "", "")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("build fails: the deploy CONTINUES — step 5 runs the existing image",
      code == 0 and len(sh.ran("docker", "compose", "up")) >= 1)
check("build fails: recorded as a warning, so the badge shows",
      doc["status"] == "warning"
      and "build failed for memos" in doc["messages"][0]["text"])


# ---- 11. a new profile-gated service — PRESERVED DEFECT (3) ---------------

print("sequence: a new profile-gated service is not running")
DIFF = "diff --git a/docker-compose.yml b/docker-compose.yml\n+  memos-db:\n"
sh = Shell(changed=["docker-compose.yml"], compose_diff=DIFF,
           running=["caddy", "litellm", "homepage", "memos"])
code, doc, dep = run_deploy(sh, PREV_OK)
warn = [m["text"] for m in doc["messages"]]
check("new service: WARNed with a runnable command",
      warn == ["new service(s) not started: memos-db — run: docker compose "
               "--profile apps up -d memos-db"], detail=str(warn))
check("new service: it is NOT started — PRESERVED DEFECT (3). Starting a "
      "profile stays the operator's call; the deploy only makes it loud",
      not any(c[:4] == ["docker", "compose", "up", "-d"] and "memos-db" in c
              for c in sh.calls))
check("new service: status warning, and the run still 'succeeds'",
      code == 0 and doc["status"] == "warning")

print("sequence: a new one-shot service is NOT warned about")
sh = Shell(changed=["docker-compose.yml"],
           compose_diff="+  snake:\n", running=RUNNING)
code, doc, dep = run_deploy(sh, PREV_OK)
check("one-shot: restart:'no' is never expected up, so no warning",
      doc["status"] == "ok" and doc["messages"] == [])


# ---- 12. agent/ changed — PRESERVED DEFECT (2) ----------------------------

print("sequence: agent/ changed")
sh = Shell(changed=["agent/AGENTS.md"])
code, doc, dep = run_deploy(sh, PREV_OK)
check("agent: a candidate image is built from ./agent",
      any(c[:5] == ["docker", "build", "-t", deploy_entry.AGENT_CANDIDATE,
                    "./agent"] for c in sh.calls))
check("agent: the jail smoke test gates the promotion",
      any(c[0] == "./scripts/test-jail-image.sh" for c in sh.calls))
check("agent: only then is :local moved",
      any(c[:2] == ["docker", "tag"] and c[-1] == deploy_entry.AGENT_LOCAL
          for c in sh.calls))
check("agent: the candidate tag is dropped afterwards",
      any(c[:3] == ["docker", "image", "rm"] for c in sh.calls))
check("agent: `docker compose build agent` is NEVER run — PRESERVED DEFECT (2)",
      not any(c[:4] == ["docker", "compose", "build", "agent"] for c in sh.calls))
check("agent: status ok", code == 0 and doc["status"] == "ok")

print("sequence: the agent smoke test fails")
sh = Shell(changed=["agent/AGENTS.md"],
           overrides={"./scripts/test-jail-image.sh": (1, "", "")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("agent smoke fails: :local is NOT moved — the stale jail keeps serving",
      not any(c[:2] == ["docker", "tag"] for c in sh.calls))
check("agent smoke fails: WARN only, deploy finishes",
      code == 0 and doc["status"] == "warning"
      and "smoke test failed" in doc["messages"][0]["text"])


# ---- 13. a corrupt previous deploy-info ------------------------------------

print("sequence: a corrupt previous deploy-info")
sh = Shell(changed=[], overrides={"docker compose up -d --remove-orphans": (3, "", "")})
code, doc, dep = run_deploy(sh, "{not json at all")
check("corrupt previous: the deploy does not crash on it", code == 3)
check("corrupt previous: deployed_commit becomes empty, which the watcher "
      "reads as 'nothing deployed yet' and retries — the safe direction",
      doc["status"] == "failed" and doc["deployed_commit"] == "")


# ---- 14. the origin mirror -------------------------------------------------

print("sequence: the origin mirror")
sh = Shell(changed=[], overrides={"git remote get-url origin": (0, "git@...", ""),
                                  "git push origin main": (1, "", "no ssh agent")})
code, doc, dep = run_deploy(sh, PREV_OK)
check("origin push fails: a WARN, never fatal — the node is already at merged "
      "main and sync-node-config.sh reconciles",
      code == 0 and doc["status"] == "warning"
      and "could not push origin" in doc["messages"][0]["text"])

print(f"\n{'FAILED' if FAIL else 'PASS'}: deploy.py sequence ({FAIL} failure(s))")
sys.exit(1 if FAIL else 0)
