#!/usr/bin/env python3
"""
Offline tests for node_verify.strict_yaml — the duplicate-key loader.

This loader has been got wrong twice in production, and neither mistake was
visible in the gate's output because the checked files happened not to
exercise it. So it carries its own self-check, and this file tests the
self-check as well as the loader:

  - the seven shipped self-assertions all pass against the real loader
  - self_check REPORTS, rather than raises, each way a loader can be wrong:
    rejects-what-should-parse, parses-what-should-be-rejected, and
    parses-to-the-wrong-value
  - duplicate keys are rejected: top level, nested in a service, inside an
    anchor, and a repeated `<<`
  - the legal merge patterns parse, and an explicit key after `<<` WINS
    (the bug the obvious flatten-first repair introduces)
  - compose's own !reset / !override tags parse, mapping payload included
  - multi-document files come back as a list

Run:  python3 scripts/node_verify/test_strict_yaml.py     (from the repo root)
      ./scripts/verify-config.sh                          (runs it with the rest)
PyYAML + stdlib only.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import yaml                                                     # noqa: E402

from node_verify import strict_yaml                             # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def parses(doc):
    try:
        return True, yaml.load(doc, Loader=strict_yaml.StrictLoader)
    except yaml.YAMLError as e:
        return False, e


# ---- 1. the shipped self-check ---------------------------------------------

check("self_check: the real loader passes all seven assertions",
      strict_yaml.self_check() == [], detail=str(strict_yaml.self_check()))
check("self_check: there are still seven assertions",
      len(strict_yaml.SELF_TESTS) == 7, detail=str(len(strict_yaml.SELF_TESTS)))

# A loader that rejects a legal document: the a924d45 bug, where every compose
# file using an anchor merge went red.
rejects_legal = [("legal merge", "x: &x\n  a: 1\n  a: 2\n", {"a": 1})]
out = strict_yaml.self_check(rejects_legal)
check("self_check: reports a legal document that was rejected",
      len(out) == 1 and "rejected, should parse" in out[0], detail=str(out))

# A loader that accepts a duplicate: the #93 bug, where compose was broken on
# main while the gate said OK.
accepts_dup = [("duplicate", "y:\n  a: 1\n", None)]
out = strict_yaml.self_check(accepts_dup)
check("self_check: reports a bad document that parsed anyway",
      len(out) == 1 and "parsed, should be rejected" in out[0], detail=str(out))

# A loader that parses but produces the wrong mapping: the flatten-first
# repair, where an override after a merge loses to the anchor.
wrong_value = [("override wins", "x: &x {a: 1}\ny:\n  <<: *x\n  a: 2\n", {"a": 99})]
out = strict_yaml.self_check(wrong_value)
check("self_check: reports a document that parsed to the wrong value",
      len(out) == 1 and "y={'a': 2}, want {'a': 99}" in out[0], detail=str(out))

check("self_check: a healthy loader reports nothing at all",
      strict_yaml.self_check([("ok", "y: {a: 1}\n", {"a": 1})]) == [])


# ---- 2. duplicate keys are rejected ----------------------------------------

ok, _ = parses("a: 1\na: 2\n")
check("dup: a top-level duplicate is rejected", ok is False)

ok, _ = parses("services:\n  s:\n    image: x\n    image: y\n")
check("dup: a duplicate nested in a service is rejected", ok is False)

ok, _ = parses("x: &x\n  a: 1\n  a: 2\ny:\n  <<: *x\n")
check("dup: a duplicate inside an anchor is rejected", ok is False)

ok, err = parses("x: &x {a: 1}\nz: &z {b: 2}\ny:\n  <<: *x\n  <<: *z\n")
check("dup: a repeated merge key is rejected, as compose rejects it", ok is False)
check("dup: the repeated-merge message names the merge key",
      ok is False and "duplicate merge key '<<'" in str(err), detail=str(err))

ok, err = parses("a: 1\na: 2\n")
check("dup: the message names the key and the first line",
      "duplicate key 'a'" in str(err) and "line 1" in str(err), detail=str(err))


# ---- 3. the legal merge patterns parse -------------------------------------

ok, got = parses("x: &x {a: 1}\ny:\n  <<: *x\n  b: 2\n")
check("merge: a plain merge is accepted", ok and got["y"] == {"a": 1, "b": 2},
      detail=str(got))

ok, got = parses("x: &x {a: 1}\ny:\n  <<: *x\n  a: 2\n")
check("merge: an explicit key after `<<` WINS over the anchor",
      ok and got["y"] == {"a": 2}, detail=str(got))

ok, got = parses("x: &x {a: 1}\nz: &z {a: 9, b: 2}\ny:\n  <<: [*x, *z]\n")
check("merge: a list of anchors merges, first anchor winning",
      ok and got["y"] == {"a": 1, "b": 2}, detail=str(got))


# ---- 4. compose's own tags -------------------------------------------------

ok, got = parses("services:\n  s:\n    ports: !reset []\n")
check("tags: !reset parses, payload preserved",
      ok and got["services"]["s"]["ports"] == [], detail=str(got))

ok, got = parses("services:\n  s:\n    environment: !override\n      A: 1\n")
check("tags: !override on a mapping parses",
      ok and got["services"]["s"]["environment"] == {"A": 1}, detail=str(got))

ok, _ = parses("services:\n  s:\n    environment: !override\n      A: 1\n      A: 2\n")
check("tags: a duplicate INSIDE a tagged mapping is still rejected", ok is False)


# ---- 5. load_all -----------------------------------------------------------

docs = strict_yaml.load_all("a: 1\n---\nb: 2\n")
check("load_all: every document comes back",
      docs == [{"a": 1}, {"b": 2}], detail=str(docs))

try:
    strict_yaml.load_all("a: 1\na: 2\n")
    check("load_all: a duplicate raises YAMLError", False)
except yaml.YAMLError:
    check("load_all: a duplicate raises YAMLError", True)

# The error message is what the operator reads. Naming the file (rather than
# "<unicode string>") is the message bash printed, and the reason load_all
# wraps the text in a named stream.
try:
    strict_yaml.load_all("a: 1\na: 2\n", name="docker-compose.yml")
    check("load_all: the error names the file it came from", False)
except yaml.YAMLError as e:
    msg = " ".join(str(e).split())
    check("load_all: the error names the file it came from",
          '"docker-compose.yml"' in msg and "<unicode string>" not in msg, detail=msg)


print()
if FAIL == 0:
    print("test_strict_yaml: PASS")
    sys.exit(0)
print(f"test_strict_yaml: FAIL ({FAIL} failures)")
sys.exit(1)
