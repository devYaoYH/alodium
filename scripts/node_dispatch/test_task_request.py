#!/usr/bin/env python3
"""
Offline tests for node_dispatch.task_request — the `run: <brief>` gate.

This is the rule that lets an agent make the HOST execute something, so each
property is asserted by name, not only by equivalence:

  - the name is reduced to [a-z0-9_-]: no `/`, no `.`, no way out of tasks/
  - only a brief that EXISTS as a file can run
  - only `dispatch: auto`, read from the brief's own frontmatter, can run
  - nothing from the issue except its title is used (the verdict takes no body)

and brief_name is checked against the sed|tr pipeline, as recorded
(testdata/bash_baseline.json), including its fail-closed quirk: uppercase is
deleted, not lowercased.

Run:  python3 scripts/node_dispatch/test_task_request.py   (from the repo root)
"""

import inspect
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_dispatch import task_request as tr                    # noqa: E402

PIPES = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())["pipelines"]
FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


print("brief_name: equivalence with `sed 's/^run:[[:space:]]*//i' | tr -cd 'a-z0-9_-'`")
for case in PIPES["brief_name"]:
    got = tr.brief_name(case["title"])
    check(f"{case['title']!r} -> {case['name']!r}", got == case["name"], f"got {got!r}")

print("brief_name: it can only ever name a file directly inside tasks/")
hostile = ["run: ../../etc/passwd", "run: /abs/path", "run: a/b", "run: ..", "run: x.md",
           "run: $(id)", "run: `id`", "run: a\x00b", "run: CON", "run: ∕etc"]
for title in hostile:
    name = tr.brief_name(title)
    check(f"{title!r} -> {name!r} is [a-z0-9_-]*", re.fullmatch(r"[a-z0-9_-]*", name) is not None)
check("a newline cannot smuggle a second request into the name",
      tr.brief_name("run: digest\n99 run: evil") == "digest")

print("verdict: the order of the gates")
check("empty name -> missing", tr.verdict("", True, "auto") == tr.MISSING)
check("no such file -> missing", tr.verdict("x", False, "auto") == tr.MISSING)
check("file without the flag -> not-auto", tr.verdict("x", True, "") == tr.NOT_AUTO)
check("flag must be exactly 'auto'", tr.verdict("x", True, "auto\nauto") == tr.NOT_AUTO)
check("tracked + auto -> eligible", tr.verdict("x", True, "auto") == tr.ELIGIBLE)
check("the verdict cannot see an issue body",
      list(inspect.signature(tr.verdict).parameters) == ["name", "brief_is_file", "dispatch_value"])

print("messages (posted verbatim; changing them is a behavior change)")
check("missing", tr.rejection(tr.MISSING, "x") ==
      "Rejected: no tracked brief `tasks/x.md`. New capabilities are a `handoff` to "
      "agent-dev (brief ships as a PR), not a request.")
check("not auto", tr.rejection(tr.NOT_AUTO, "x") ==
      "Rejected: `tasks/x.md` is not marked `dispatch: auto`. Flipping that flag is a "
      "reviewed PR — ask agent-dev via `handoff`.")

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
