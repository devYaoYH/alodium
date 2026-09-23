#!/usr/bin/env python3
"""
Offline tests for node_host.envfile against `set -a; source .env` itself.

testdata/bash_baseline.json holds, for each fixture line, what bash exported
after sourcing it under `set -euo pipefail` (or that it aborted). For every
fixture bash accepted, the parser must produce the same values — or be one of
the constructs it REFUSES by design, listed below with the reason. For every
fixture bash rejected, the parser must reject too.

Refused by design (bash accepts, the parser raises):
  $(…) / `…`       command substitution: .env would execute on every heartbeat
  ${VAR:-default}  parameter operators: compose's dialect differs from bash's
  echo hi          a bare command, run by `source`
  A=x;B=y          a command list
Deliberately different:
  a trailing CR    stripped (bash keeps it in the value); a CRLF .env written
                   on Windows would otherwise put '\\r' in NODE_DOMAIN

Run:  python3 scripts/node_host/test_envfile.py   (from the repo root)
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_host import envfile                                   # noqa: E402

BASELINE = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())
BASE = {"HOME": "/home/op", "BASE": "b"}
REFUSED_BY_DESIGN = {"A=$(echo hi)\n", "A=`echo hi`\n", "A=${BASE:-d}\n", "echo hi\n", "A=x;B=y\n"}
CR_STRIPPED = {"A=cr\r\n": {"A": "cr"}}

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def parse(text):
    try:
        return envfile.parse(text, BASE, home=BASE["HOME"]), None
    except envfile.EnvFileError as exc:
        return None, str(exc)


print("equivalence with bash `source` (recorded)")
seen = set()
for case in BASELINE["pipelines"]["envfile"]:
    text = case["text"]
    seen.add(text)
    got, err = parse(text)
    keys = ("A", "B", "EXTRA_TRUSTED_RANGES")
    got_view = {k: v for k, v in (got or {}).items() if k in keys}
    if case.get("error"):
        check(f"bash rejected {text!r} -> refused", err is not None, f"got {got}")
    elif text in REFUSED_BY_DESIGN:
        check(f"refused by design {text!r}", err is not None, f"got {got}")
    elif text in CR_STRIPPED:
        check(f"CR stripped {text!r}", got_view == CR_STRIPPED[text], f"got {got_view}")
    else:
        check(f"{text!r} -> {case['values']}", got_view == case["values"],
              f"got {got_view} err={err}")
check("every refused-by-design case was actually recorded", REFUSED_BY_DESIGN <= seen)

print("load(): .env overrides the inherited environment, as `set -a; source` did")
with tempfile.TemporaryDirectory() as t:
    p = Path(t) / ".env"
    p.write_text("NODE_DOMAIN=from-file\nNEW=${KEEP}-x\n")
    merged = envfile.load(p, {"NODE_DOMAIN": "inherited", "KEEP": "k", "HOME": "/h"})
    check("file value wins", merged["NODE_DOMAIN"] == "from-file")
    check("inherited-only value survives", merged["KEEP"] == "k")
    check("expansion sees the inherited env", merged["NEW"] == "k-x")
    try:
        envfile.load(Path(t) / "missing", {})
        check("a missing .env raises", False)
    except OSError:
        check("a missing .env raises", True)

print("the live node's .env shape")
live_shape = ("NODE_DOMAIN=localhost\nACME_EMAIL=you@example.com\n"
              "LITELLM_MASTER_KEY=sk-0123abcd\n# comment\n\n"
              'EXTRA_TRUSTED_RANGES="10.0.0.0/8 192.168.0.0/16"\nDISPATCH_MAX_CONCURRENCY=4\n')
got, err = parse(live_shape)
check("parses", err is None, err)
check("quoted value with a space", (got or {}).get("EXTRA_TRUSTED_RANGES") == "10.0.0.0/8 192.168.0.0/16")
check("errors name the line", "line 2" in (parse("A=1\nB=two words\n")[1] or ""))

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
