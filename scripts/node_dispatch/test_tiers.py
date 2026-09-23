#!/usr/bin/env python3
"""
Offline tests for node_dispatch.tiers (and dispatch_run's two small parsers)
against the bash's inline programs, recorded by executing them
(testdata/bash_baseline.json):

  tiers         TIER_JSON (YAML -> JSON round trip, or an error), TIER_RESOLVE,
                DEFAULT_TIER, the `case` branch and the ${X%%:*}/${X#*:} split
                — over a missing table, an empty one, a broken one, integer
                keys, a colon in a model id (PRESERVED DEFECT), missing fields
  llm_check     live / not_found / error over the /v1/models bodies LiteLLM
                (or its absence) can produce
  issue_number  `[[ "$NUM" =~ ^[0-9]+$ ]]`
  trace_dir     the `sed -n 's#.*trace saved to \\(…\\).*#\\1#p' | tail -1`

Run:  python3 scripts/node_dispatch/test_tiers.py   (from the repo root)
"""

import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from node_dispatch import tiers                                  # noqa: E402

_spec = importlib.util.spec_from_file_location("dispatch_run", SCRIPTS / "dispatch_run.py")
dispatch_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispatch_run)

PIPES = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())["pipelines"]
FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def bash_case(model_budget_kind):
    """Our (classify, split) rendered the way the recorder printed the bash's."""
    kind, resolved = model_budget_kind
    if kind == tiers.RESOLVED:
        m, b = tiers.split(resolved)
        return f"resolved|{m}|{b}"
    return kind


print("tiers: equivalence with TIER_JSON / TIER_RESOLVE / DEFAULT_TIER / case / split")
try:
    import yaml                                              # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False
    print("  SKIP: host python has no PyYAML; load() is checked for its error shape only")

seen_yaml = {}
with tempfile.TemporaryDirectory() as t:
    for case in PIPES["tiers"]:
        key = case["yaml"]
        if key not in seen_yaml:
            path = Path(t) / f"t{len(seen_yaml)}.yaml"
            if case["yaml"] is not None:
                path.write_text(case["yaml"])
            cwd = os.getcwd()
            os.chdir(t)
            try:
                data = tiers.load(path.name)
            finally:
                os.chdir(cwd)
            seen_yaml[key] = data
            want = json.loads(case["tier_json"])
            if HAVE_YAML:
                # The error text names the file the bash opened by its relative
                # path; compare errors by kind, values exactly.
                same = (data == want) or ("error" in data and "error" in want)
                check(f"load {case['yaml']!r}", same, f"got {data!r} want {want!r}")
        data = json.loads(case["tier_json"])     # continue from the BASH's table
        resolved = tiers.resolve_label(data, case["key"])
        check(f"resolve {case['yaml']!r} [{case['key']}]",
              resolved == case["resolve"], f"got {resolved!r} want {case['resolve']!r}")
        got_case = bash_case((tiers.classify(case["resolve"]), case["resolve"]))
        check(f"  case/split -> {case['case']!r}", got_case == case["case"], f"got {got_case!r}")
        dflt = tiers.default_tier(data)
        check(f"  default -> {case['default']!r}", dflt == case["default"], f"got {dflt!r}")
        m, b = tiers.split(case["default"])
        check(f"  default split", f"{m}|{b}" == case["default_split"], f"got {m}|{b}")

print("tiers: named properties")
check("PRESERVED DEFECT: a colon in a model id splits at the first colon",
      tiers.split("vendor/x:free:1.0") == ("vendor/x", "free:1.0"))
check("no colon: ${X#*:} leaves the whole string", tiers.split("abc") == ("abc", "abc"))
check("an unreadable table is an error, not a crash", "error" in tiers.load("/nonexistent/t.yaml"))

print("llm_check: equivalence with the inline program")
for case in PIPES["llm_check"]:
    got = tiers.llm_check(case["body"], case["target"])
    check(f"llm_check({case['body']!r}, {case['target']!r}) = {case['out']!r}",
          got == case["out"], f"got {got!r}")
check("an empty body (unreachable) is an error, never not_found",
      tiers.llm_check("", "m").startswith("error:"))

print("issue number: equivalence with [[ =~ ^[0-9]+$ ]]")
for case in PIPES["issue_number"]:
    got = re.fullmatch(r"[0-9]+", case["in"]) is not None
    check(f"{case['in']!r} valid={case['valid']}", got == case["valid"])

print("trace_dir: equivalence with the sed | tail -1")
for case in PIPES["trace_dir"]:
    got = dispatch_run.trace_dir(case["in"])
    check(f"{case['in']!r} -> {case['out']!r}", got == case["out"], f"got {got!r}")

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
