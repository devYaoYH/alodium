#!/usr/bin/env python3
"""
Offline tests for node_host.frontmatter and node_host.text against the shell
pipelines they replace, recorded by executing them (testdata/bash_baseline.json):

  front        the awk frontmatter reader — the `dispatch: auto` gate reads it,
               so every quirk is pinned: no-frontmatter briefs scanned to the
               end, `key:value` without a space, `#` mid-value, CRLF, repeats
  clean_run    dispatch-run's failure tail (sed ANSI strip, tr -d '\\r',
               grep -viE noise, grep -v blank, tail -12)
  tail15       `$(… | tail -15)`
  audit_line   the audit log's printf, quote substitution included

Run:  python3 scripts/node_host/test_text.py   (from the repo root)
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_host import frontmatter, text                        # noqa: E402

PIPES = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())["pipelines"]
FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


print("front: equivalence with the awk")
for case in PIPES["front"]:
    got = frontmatter.front(case["text"], case["key"])
    check(f"front({case['text']!r}, {case['key']}) = {case['out']!r}", got == case["out"],
          f"got {got!r}")

print("front: the gate's safety-relevant cases, by name")
auto = lambda t: frontmatter.front(t, "dispatch") == "auto"         # noqa: E731
check("frontmatter flag counts", auto("---\ndispatch: auto\n---\n"))
check("a repeated key is NOT auto (fails closed)", not auto("---\ndispatch: auto\ndispatch: auto\n---\n"))
check("quoted 'auto' is not auto", not auto('---\ndispatch: "auto"\n---\n'))
check("uppercase is not auto", not auto("---\ndispatch: AUTO\n---\n"))
check("a commented-out flag is not auto", not auto("---\n#dispatch: auto\n---\n"))
check("PRESERVED: no frontmatter => a body line counts", auto("text\ndispatch: auto\n"))
check("a body line after the frontmatter does not count",
      not auto("---\nx: 1\n---\ndispatch: auto\n"))
check("front_file on a missing file is empty", frontmatter.front_file("/nonexistent/x.md", "k") == "")

print("clean_run: equivalence with the sed|tr|grep|grep|tail pipeline")
for case in PIPES["clean_run"]:
    got = text.clean_run(case["in"])
    check(f"clean_run({case['in'][:30]!r}…)", got == case["out"], f"got {got!r} want {case['out']!r}")

print("tail_lines: equivalence with $(… | tail -15)")
for case in PIPES["tail15"]:
    got = text.tail_lines(case["in"], 15)
    check(f"tail15({case['in'][:30]!r}…)", got == case["out"], f"got {got!r} want {case['out']!r}")

print("audit_line: equivalence with the printf")
for case in PIPES["audit_line"]:
    got = text.audit_line("TS", 7, "act", "", case["detail"])
    check(f"audit_line(detail={case['detail']!r})", got == case["out"], f"got {got!r}")

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
