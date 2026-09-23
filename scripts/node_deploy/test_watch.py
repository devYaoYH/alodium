#!/usr/bin/env python3
"""
EQUIVALENCE for scripts/deploy_watch.py against the bash deploy-watch.sh.

deploy-watch decides, every two minutes and with nobody watching, whether the
node deploys. It was recorded, not reasoned about: each scenario in
testdata/watch_scenarios.json was built as a REAL git world (a bare `forgejo`
remote at C3, the checkout on main at C2) by node_host/testkit.py, the bash was
executed in it with a fake deploy.sh and a fake Forgejo, and what it did went
into testdata/watch_bash_baseline.json: exit status, every stdout line, every
API call with its body, whether deploy ran, which stamps exist afterwards,
where HEAD is, and whether the lock was left behind.

This replays the same worlds through deploy_watch.main — with real git, since
git's own answers (rev-parse, diff-index, merge-base, log) are half of the
behavior — and asserts the same outcome. Commit hashes are compared by name.

The invariants worth naming, each covered by a scenario:
  - the watcher NEVER moves the checkout: head_after is C2 (or L1) in every
    scenario, including a successful deploy (the fake deploy does not pull;
    the real deploy.py does its own fast-forward, and must be the one to)
  - a failed tip is reported once: stamped, and the next pass stays quiet
  - a divergence deploys nothing and exits 1, dry-run or not
  - PRESERVED DEFECT: a deploy that fails while Forgejo is down is stamped and
    never reported (deploy_fails_forgejo_down)

Also: node_deploy.info.watcher_deployed against the bash's inline reader.

Run:  python3 scripts/node_deploy/test_watch.py   (from the repo root)
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from node_deploy import info                                   # noqa: E402
from node_host import testkit                                  # noqa: E402

_spec = importlib.util.spec_from_file_location("deploy_watch", SCRIPTS / "deploy_watch.py")
deploy_watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_watch)

SCENARIOS = json.loads((HERE / "testdata" / "watch_scenarios.json").read_text())["scenarios"]
BASH = json.loads((HERE / "testdata" / "watch_bash_baseline.json").read_text())

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def replay(spec):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root, names = testkit.build_watch_world(tmp, spec)
        genv = testkit.git_env(tmp)

        def run(argv, **kw):                 # real git, isolated from ~/.gitconfig
            if not kw.get("capture_output"):
                kw.setdefault("stdout", subprocess.DEVNULL)
                kw.setdefault("stderr", subprocess.DEVNULL)
            return subprocess.run(argv, env=genv, **kw)

        host = testkit.FakeHost(spec, root)
        transport = testkit.FakeTransport(testkit.Routes(spec))
        stdout = io.StringIO()
        cwd = os.getcwd()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = deploy_watch.main(["x", *spec.get("argv", [])], repo_root=root,
                                         base_env=dict(genv), host=host,
                                         transport=transport, run=run)
        finally:
            os.chdir(cwd)
        out = {"exit": code,
               "stdout": [testkit.unhash(line, names)
                          for line in testkit.normalize_stdout(stdout.getvalue())],
               "calls": testkit.unhash_obj(transport.calls, names),
               "deploys": host.deploys}
        out.update(testkit.watch_state(root, names, genv))
        return out


print("deploy-watch: every recorded scenario")
for name, spec in SCENARIOS.items():
    got, want = replay(spec), BASH["scenarios"][name]
    bad = [k for k in want if not k.startswith("_") and got.get(k) != want[k]]
    check(f"watch/{name}", not bad,
          "".join(f"\n      {k}: got {got.get(k)!r}\n      {' ' * len(k)}  bash {want[k]!r}"
                  for k in bad))

print("invariants the recording shows")
heads = {n: o["head_after"] for n, o in BASH["scenarios"].items()}
check("the watcher never moved the checkout",
      all(h in ("{C2}", "{L1}") for h in heads.values()), heads)
check("a failed deploy was stamped",
      "deploy-fail-{C3}" in BASH["scenarios"]["deploy_fails"]["stamps_after"])
check("a successful deploy cleared old stamps",
      not any(s.startswith("deploy-fail-")
              for s in BASH["scenarios"]["deploy_ok"]["stamps_after"]))
check("divergence exits 1 even in dry-run",
      BASH["scenarios"]["diverged_dry"]["exit"] == 1
      and BASH["scenarios"]["diverged_dry"]["deploys"] == 0)
check("no pass leaves its lock behind (except one it did not take)",
      all(not o["lock_left"] for n, o in BASH["scenarios"].items() if n != "lock_held"))

print("info.watcher_deployed: the bash's inline reader")
for case in BASH["pipelines"]["watcher_deployed"]:
    got = info.watcher_deployed(case["text"])
    check(f"deploy-info {case['text']!r} -> {case['out']!r}", got == case["out"], f"got {got!r}")

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
