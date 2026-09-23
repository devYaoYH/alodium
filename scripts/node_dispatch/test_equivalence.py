#!/usr/bin/env python3
"""
EQUIVALENCE with the bash dispatcher and dispatch-run this package replaces.
The load-bearing test.

A live A/B is not available and must not be manufactured: proving a dispatcher
port by filing issues at the real node would launch real tenants. So the bash
was EXECUTED instead, in throwaway checkouts (node_host/testkit.py), against a
fake Forgejo, LiteLLM, docker, run-task and trace-render:

  testdata/scenarios.json      the inputs: issues, timelines, labels, briefs,
                               stamps, the tier table, what run-task prints
  testdata/bash_baseline.json  what /bin/bash scripts/task-dispatcher.sh and
                               scripts/dispatch-run.sh DID with each: every
                               API call in order with its body, every stdout
                               line, which run-task / dispatch-run / trace-
                               render invocations happened, which stamps were
                               touched, the audit log, the spool, the lock

This file replays each scenario through scripts/task_dispatcher.py and
scripts/dispatch_run.py with the same fakes and asserts the SAME outcome. The
scenarios cover the security gates, not just the happy path:

  requests_mix        missing / not-auto / traversal / duplicate-key briefs,
                      a body-only `dispatch: auto`, and the in-pass cooldown
  assigned_gates      operator vs self-assignment, removed events, created_at
                      ordering, an in-progress claim, a cooling-down issue
  operator_override   OPERATOR_LOGIN from the host env redefines who may assign
  *_down / *_unreadable  what an unreachable or garbled Forgejo does (#62)
  hard_by_agent / relabelled_by_agent  an agent cannot pick its own tier
  litellm_*           outage aborts; a 401 falls back (recorded, kept)

One deliberate difference is normalized below: the bash spawned
./scripts/dispatch-run.sh, the port spawns scripts/dispatch_run.py under the
same interpreter. Both are recorded as the issue number they were given.

Run:  python3 scripts/node_dispatch/test_equivalence.py   (from the repo root)
      ./scripts/verify-config.sh                          (runs it with the rest)
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from node_host import testkit                                  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = load("task_dispatcher")
dispatch_run = load("dispatch_run")

SCENARIOS = json.loads((HERE / "testdata" / "scenarios.json").read_text())
BASH = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def base_env(tmp):
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp / "home")}


def replay(module, spec, extra_keys):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (tmp / "home").mkdir()
        root = testkit.build_dispatch_world(tmp, spec, SCENARIOS["briefs"])
        host = testkit.FakeHost(spec, root)
        transport = testkit.FakeTransport(testkit.Routes(spec))
        stdout = io.StringIO()
        cwd = os.getcwd()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = module.main(["x", *spec.get("argv", [])], repo_root=root,
                                   base_env=base_env(tmp), host=host, transport=transport)
        finally:
            os.chdir(cwd)                   # main() cds into the checkout, as the bash did
        out = {"exit": code, "stdout": testkit.normalize_stdout(stdout.getvalue()),
               "calls": transport.calls, "run_task": host.run_task}
        state = testkit.dispatch_state(tmp, root)
        if "spawns" in extra_keys:
            out.update(spawns=host.spawns, sleeps=host.sleeps, **state)
        else:
            out.update(trace_render=host.trace_render, touched=state["touched"],
                       audit=state["audit"])
        return out


def compare(label, got, want):
    for key in want:
        if key.startswith("_"):
            continue
        if got.get(key) != want[key]:
            g, w = got.get(key), want[key]
            if isinstance(g, list) and isinstance(w, list):
                for i, (a, b) in enumerate(zip(g, w)):
                    if a != b:
                        detail = f"first difference at [{i}]:\n      got  {a!r}\n      bash {b!r}"
                        break
                else:
                    detail = f"length {len(g)} vs bash {len(w)}"
            else:
                detail = f"got {g!r}, bash {w!r}"
            check(f"{label}: {key}", False, detail)
            return
    check(label, True)


print("task-dispatcher: every recorded scenario")
for name, spec in SCENARIOS["dispatcher"].items():
    compare(f"dispatcher/{name}", replay(dispatcher, spec, ("spawns",)),
            BASH["dispatcher"][name])

print("dispatch-run: every recorded scenario")
for name, spec in SCENARIOS["dispatch_run"].items():
    compare(f"dispatch_run/{name}", replay(dispatch_run, spec, ("trace_render",)),
            BASH["dispatch_run"][name])

print("coverage: the recording exercised the paths that matter")
calls = [c for o in BASH["dispatcher"].values() for c in o["calls"]]
check("a rejection closed an issue", any(c[0] == "PATCH" for c in calls))
check("a claim was added", any(c[0] == "POST" and c[1].endswith("/labels") for c in calls))
check("a claim was released", any(c[0] == "DELETE" for c in calls))
check("a tenant was spawned", any(o["spawns"] for o in BASH["dispatcher"].values()))
check("run-task ran from a task request",
      any(o["run_task"] for o in BASH["dispatcher"].values()))
check("dispatch-run aborted on a LiteLLM outage",
      BASH["dispatch_run"]["litellm_down"]["exit"] == 1)
check("a trace was rendered", any(o["trace_render"] for o in BASH["dispatch_run"].values()))

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
