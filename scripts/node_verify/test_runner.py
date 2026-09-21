#!/usr/bin/env python3
"""
Offline tests for node_verify.{runner, report, discovery} — the edge and the
verdict assembly. No caddy, no shellcheck, no git, no daemon: `run` and
`which` are injected, so this asserts the argv and the verdict mapping on a
machine where none of those tools exist.

  - caddy: SKIPs without the binary; validates the COPY, not the checkout;
    rewrites the /srv/apps/ import so the app routes are actually read (without
    that rewrite a broken route.caddy validates against an empty door); the
    failure keeps the diagnosis and drops caddy's own chatter
  - shellcheck: SKIPs without the binary; runs -S error over the file list;
    a non-zero exit FAILs with the first 20 lines
  - the test-discovery UNION — tracked plus on-disk — which is the bug that
    made this gate print PASS while skipping a brand-new test file
  - shell_files falls back only when git FAILED, not when git returned nothing
  - report: OK/SKIP/FAIL rendering, head/tail truncation, and exit-code
    assembly — any FAIL ⇒ 1, and a SKIP never masks a FAIL
  - guarded: an exception becomes a FAIL with its traceback, never a pass
  - EQUIVALENCE: the file lists bash computed on this tree are reproduced

Run:  python3 scripts/node_verify/test_runner.py     (from the repo root)
      ./scripts/verify-config.sh                     (runs it with the rest)
PyYAML + stdlib only.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_verify import discovery, report, runner                # noqa: E402
from node_verify.report import FAIL as R_FAIL, OK, Result, SKIP, Section  # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def recorder(results):
    """An injected `run` that records argv and replays canned results.

    `results` maps the first argv element to a FakeProc (or a callable).
    """
    calls = []

    def run(argv, **kw):
        calls.append((list(argv), kw))
        out = results.get(argv[0], FakeProc())
        return out(argv) if callable(out) else out

    run.calls = calls
    return run


REPO = Path(__file__).resolve().parent.parent.parent    # the repo root


# ---- 1. caddy --------------------------------------------------------------

run = recorder({})
t = runner.Tools(REPO, run=run, which=lambda name: None)
r = t.caddy_validate()
check("caddy: no binary is a SKIP, not a pass", r.status == SKIP)
check("caddy: the SKIP says where the binary lives",
      r.note == ("SKIP: no caddy binary here (present in the jail image; "
                 "install caddy to run this locally)"), detail=str(r.note))
check("caddy: nothing was executed", run.calls == [])

seen = {}


def capture_caddy(argv):
    cfg = Path(argv[argv.index("--config") + 1])
    seen["config"] = str(cfg)
    seen["text"] = cfg.read_text()
    seen["envfile"] = Path(argv[argv.index("--envfile") + 1]).read_text()
    seen["routes"] = sorted(p.name for p in cfg.parent.parent.glob("apps/*/route.caddy"))
    return FakeProc(0)


run = recorder({"caddy": capture_caddy})
t = runner.Tools(REPO, run=run, which=lambda name: "/usr/bin/" + name)
r = t.caddy_validate()
check("caddy: a clean validate is OK", r.status == OK)
check("caddy: the OK note is unchanged", r.note == "OK: config adapts and validates")
check("caddy: it validates a COPY, never the checkout itself",
      not seen["config"].startswith(str(REPO)), detail=seen["config"])
check("caddy: the /srv/apps import is rewritten into the copy",
      "/srv/apps/" not in seen["text"] and "apps/*/route.caddy" in seen["text"],
      detail=seen["text"][:200])
check("caddy: the app routes came along, so a broken one would be read",
      len(seen["routes"]) > 0, detail=str(seen["routes"]))
check("caddy: dummy env values, never a real secret",
      "NODE_DOMAIN=localhost" in seen["envfile"] and "ACME_EMAIL=op@example.com"
      in seen["envfile"], detail=seen["envfile"])
check("caddy: the temp tree is cleaned up",
      not Path(seen["config"]).exists(), detail=seen["config"])
check("caddy: the adapter is named explicitly",
      run.calls[0][0][:4] == ["caddy", "validate", "--config", seen["config"]]
      and "--adapter" in run.calls[0][0], detail=str(run.calls[0][0]))

NOISE = ('{"level":"info","msg":"using config"}\n'
         "2026/09/20 shutting down\n"
         "Error: parsing caddyfile: unrecognized directive: header_up\n"
         "entering maintenance mode\n")
run = recorder({"caddy": FakeProc(1, stdout=NOISE)})
t = runner.Tools(REPO, run=run, which=lambda name: "/usr/bin/" + name)
r = t.caddy_validate()
check("caddy: a non-zero validate FAILS", r.status == R_FAIL)
check("caddy: the FAIL note is unchanged", r.note == "FAIL: caddy validate errored —")
check("caddy: the real error survives the noise filter",
      r.detail == ("Error: parsing caddyfile: unrecognized directive: header_up",),
      detail=str(r.detail))
check("caddy: only the last 12 log lines are shown", r.detail_tail == 12)
check("caddy: the filter is case-insensitive, as grep -i was",
      runner.filter_caddy_log("USING CONFIG x\nreal error\n") == ["real error"])


# ---- 2. shellcheck ---------------------------------------------------------

run = recorder({})
t = runner.Tools(REPO, run=run, which=lambda name: None)
r = t.shellcheck(["scripts/a.sh"])
check("shellcheck: no binary is a SKIP", r.status == SKIP)
check("shellcheck: the SKIP names the jail image",
      r.note == "SKIP: no shellcheck here (present in the jail image)")

run = recorder({"shellcheck": FakeProc(0)})
t = runner.Tools(REPO, run=run, which=lambda name: "/usr/bin/" + name)
r = t.shellcheck(["scripts/a.sh", "host/b.sh"])
check("shellcheck: a clean lint is OK", r.status == OK)
check("shellcheck: -S error, then every file",
      run.calls[0][0] == ["shellcheck", "-S", "error", "scripts/a.sh", "host/b.sh"],
      detail=str(run.calls[0][0]))
check("shellcheck: it runs from the repo root",
      run.calls[0][1]["cwd"] == str(REPO))
check("capture: the CHILD merges stderr into stdout, so the log keeps its order",
      run.calls[0][1]["stderr"] is __import__("subprocess").STDOUT,
      detail=str(run.calls[0][1]))

run = recorder({"shellcheck": FakeProc(1, stdout="\n".join(
    f"line {i}" for i in range(1, 41)))})
t = runner.Tools(REPO, run=run, which=lambda name: "/usr/bin/" + name)
r = t.shellcheck(["scripts/a.sh"])
check("shellcheck: an error FAILS", r.status == R_FAIL and r.note == "FAIL: shellcheck errors —")
check("shellcheck: the FIRST 20 lines are shown, as head -20 did",
      r.detail_head == 20 and r.render()[1] == "    line 1"
      and len(r.render()) == 21, detail=str(r.render()[:3]))


# ---- 3. label definitions ---------------------------------------------------

run = recorder({"bash": FakeProc(0)})
t = runner.Tools(REPO, run=run, which=lambda name: name)
r = t.ensure_tier_labels()
check("labels: --verify is the read-only mode that gets called",
      run.calls[0][0] == ["bash", "scripts/ensure-tier-labels.sh", "--verify"],
      detail=str(run.calls[0][0]))
check("labels: a clean verify is OK",
      r.status == OK and r.note == "OK: all label definitions parse correctly")

run = recorder({"bash": FakeProc(2, stdout="bad label encoding\n")})
t = runner.Tools(REPO, run=run, which=lambda name: name)
r = t.ensure_tier_labels()
check("labels: a non-zero verify FAILS with its output",
      r.status == R_FAIL and r.detail == ("bad label encoding",), detail=str(r))


# ---- 4. discovery: the union that has already lied -------------------------

check("union: tracked plus on-disk, deduped and sorted",
      discovery.union_tests(["scripts/node_backup/test_plan.py"],
                            ["scripts/node_verify/test_runner.py",
                             "scripts/node_backup/test_plan.py"]) ==
      ["scripts/node_backup/test_plan.py", "scripts/node_verify/test_runner.py"])
check("union: a NEW, untracked test is still discovered (the PASS-while-skipping bug)",
      "scripts/node_verify/test_new.py" in discovery.union_tests(
          ["scripts/node_backup/test_plan.py"], ["scripts/node_verify/test_new.py"]))
check("union: a tracked test deleted from disk is still attempted",
      discovery.union_tests(["scripts/gone/test_x.py"], []) == ["scripts/gone/test_x.py"])
check("union: blank lines are not test files",
      discovery.union_tests(["", "  "], []) == [])

check("shell_files: git's list is used when git succeeded",
      discovery.shell_files(["scripts/a.sh"], ["scripts/fallback.sh"]) == ["scripts/a.sh"])
check("shell_files: the fallback fires only when git FAILED",
      discovery.shell_files(None, ["scripts/fallback.sh"]) == ["scripts/fallback.sh"])
check("shell_files: an empty-but-successful git list lints nothing (bash did this too)",
      discovery.shell_files([], ["scripts/fallback.sh"]) == [])

run = recorder({"git": FakeProc(128, stderr="not a git repository")})
t = runner.Tools(REPO, run=run, which=lambda name: name)
check("git: a failed ls-files reports None so the fallback can fire",
      t.tracked_shell_scripts() is None)
check("git: a failed test listing contributes nothing, and does not crash",
      t.tracked_py_tests() == [])

run = recorder({"git": FakeProc(0, stdout="scripts/x.sh\nhost/y.sh\n")})
t = runner.Tools(REPO, run=run, which=lambda name: name)
check("git: the shell pathspecs are the two bash used",
      t.tracked_shell_scripts() == ["scripts/x.sh", "host/y.sh"]
      and run.calls[0][0] == ["git", "ls-files", "scripts/*.sh", "host/**/*.sh"],
      detail=str(run.calls[0][0]))

check("disk_tests: the on-disk glob finds this very file",
      "scripts/node_verify/test_runner.py" in discovery.disk_tests(REPO),
      detail=str(discovery.disk_tests(REPO)))
check("yaml_files: the globbed set includes the root compose and an app fragment",
      "docker-compose.yml" in discovery.yaml_files(REPO)
      and any(p.startswith("apps/") for p in discovery.yaml_files(REPO)))


# ---- 5. running one unit test ----------------------------------------------

run = recorder({"python3": FakeProc(0, stdout="  ok: x\ntest_x: PASS\n")})
t = runner.Tools(REPO, run=run, which=lambda name: name)
r = t.run_py_test("scripts/node_verify/test_x.py")
check("pytest: a passing test is OK, named by path",
      r.status == OK and r.note == "OK: scripts/node_verify/test_x.py")
check("pytest: each test runs in its own process, from the repo root",
      run.calls[0][0] == ["python3", "scripts/node_verify/test_x.py"]
      and run.calls[0][1]["cwd"] == str(REPO))

run = recorder({"python3": FakeProc(1, stdout="\n".join(f"l{i}" for i in range(30)))})
t = runner.Tools(REPO, run=run, which=lambda name: name)
r = t.run_py_test("scripts/node_verify/test_x.py")
check("pytest: a failing test FAILS the gate",
      r.status == R_FAIL and r.note == "FAIL: scripts/node_verify/test_x.py —")
check("pytest: the LAST 20 lines are shown, as tail -20 did",
      r.detail_tail == 20 and r.render()[-1] == "    l29" and len(r.render()) == 21,
      detail=str(r.render()[:2]))


# ---- 6. report: the vocabulary and the exit code ---------------------------

s = Section("t", [Result(OK, "OK: fine")])
check("render: a section is a blank line, a header, then its verdicts",
      s.render() == ["", "== t ==", "  OK: fine"], detail=str(s.render()))

r = Result(R_FAIL, "FAIL: x —", passthrough=("raw line",), detail=("a", "b"))
check("render: stdout leaks above the note, detail indented below it",
      r.render() == ["raw line", "  FAIL: x —", "    a", "    b"], detail=str(r.render()))

check("exit: a clean run is 0",
      report.exit_code([Section("a", [Result(OK, "x")])]) == 0)
check("exit: any FAIL is 1",
      report.exit_code([Section("a", [Result(OK, "x")]),
                        Section("b", [Result(R_FAIL, "y")])]) == 1)
check("exit: a SKIP alone is 0 — a missing binary is not a broken config",
      report.exit_code([Section("a", [Result(SKIP, "x")])]) == 0)
check("exit: a SKIP never masks a FAIL in the same section",
      report.exit_code([Section("a", [Result(SKIP, "x"), Result(R_FAIL, "y")])]) == 1)
check("exit: a FAIL in the FIRST section still counts after later sections pass",
      report.exit_code([Section("a", [Result(R_FAIL, "y")]),
                        Section("b", [Result(OK, "x")])]) == 1)
check("verdict: the PASS line is the one issue-work.md tells agents to paste",
      report.verdict_line([Section("a", [Result(OK, "x")])]) == "verify-config: PASS")
check("verdict: the FAIL line tells you what to do",
      report.verdict_line([Section("a", [Result(R_FAIL, "y")])]) ==
      "verify-config: FAIL (fix the above before pushing)")


# ---- 7. guarded: a crash is a FAIL, never a pass ---------------------------

def boom():
    raise ValueError("config/litellm.yaml is not parseable")


r = report.guarded("FAIL: unpriced model deployment —", boom)
check("guarded: an exception becomes a FAIL", r.status == R_FAIL)
check("guarded: the section's own FAIL note is used",
      r.note == "FAIL: unpriced model deployment —")
check("guarded: the traceback is shown, ending in the real error",
      r.detail[-1].endswith("config/litellm.yaml is not parseable"), detail=str(r.detail))
check("guarded: a healthy check passes its Result straight through",
      report.guarded("n", lambda: Result(OK, "OK: fine")).note == "OK: fine")
check("guarded: the caller can set the detail indent (the yaml section uses two)",
      report.guarded("n", boom, detail_indent="  ").detail_indent == "  ")


# ---- 8. equivalence with the merged bash implementation --------------------

BASELINE = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())
INPUTS = BASELINE["inputs"]

check("equiv: the yaml file set is exactly what bash's `ls` enumerated",
      discovery.yaml_files(REPO) == INPUTS["yaml_files"],
      detail=f"py={discovery.yaml_files(REPO)}")
check("equiv: every test bash discovered is still discovered",
      set(INPUTS["py_tests"]) <= set(discovery.union_tests([], discovery.disk_tests(REPO))),
      detail=str(discovery.disk_tests(REPO)))
check("equiv: this port's own tests are in the discovered set too",
      {"scripts/node_verify/test_runner.py", "scripts/node_verify/test_checks.py"}
      <= set(discovery.disk_tests(REPO)))
check("equiv: bash linted 44 shell scripts on this tree",
      len(INPUTS["shell_files"]) == 44, detail=str(len(INPUTS["shell_files"])))


print()
if FAIL == 0:
    print("test_runner: PASS")
    sys.exit(0)
print(f"test_runner: FAIL ({FAIL} failures)")
sys.exit(1)
