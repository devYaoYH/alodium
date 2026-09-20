#!/usr/bin/env python3
"""
Offline tests for node_deploy.info — deploy-info.json.

Two readers, and the second is why this file exists. The homepage renders the
badge; scripts/deploy-watch.sh reads `deployed_commit` every two minutes to
decide whether to deploy again. Get the carry-forward wrong in either direction
and the failure is silent:

  advanced on a failure  -> the broken tip looks deployed, nothing retries,
                            the node sits broken until a human notices
  not advanced on ok     -> every heartbeat redeploys, forever

So each branch of `deployed_commit_for` gets a test, including the
compatibility branch for files written before the field existed.

Byte-for-byte equality with what the bash's inline python wrote is asserted
separately, in test_equivalence.py, against output captured from running it.

Run:  python3 scripts/node_deploy/test_info.py   (from the repo root)
Stdlib only.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_deploy import info                             # noqa: E402

FAIL = 0
NEW = "1111111111111111111111111111111111111111"
OLD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


# ---- 1. status -------------------------------------------------------------

check("status: a clean run is ok", info.final_status([]) == "ok")
check("status: any recorded message downgrades to warning",
      info.final_status([("WARN", "x")]) == "warning")
check("status: an ERROR-level message downgrades the same way",
      info.final_status([("ERROR", "x")]) == "warning")


# ---- 2. deployed_commit carry-forward -------------------------------------

check("carry: an ok run advances deployed_commit to this commit",
      info.deployed_commit_for("ok", NEW, {"deployed_commit": OLD}) == NEW)
check("carry: a WARNING run still counts as deployed",
      info.deployed_commit_for("warning", NEW, {"deployed_commit": OLD}) == NEW,
      detail="a node running the merge with one app stale IS running the merge")
check("carry: a FAILED run keeps the previous deployed_commit, so the watcher "
      "retries instead of believing the broken tip is live",
      info.deployed_commit_for("failed", NEW, {"deployed_commit": OLD}) == OLD)
check("carry: a failed run after a failed run stays empty",
      info.deployed_commit_for("failed", NEW, {"deployed_commit": ""}) == "")
check("carry: no previous file at all yields empty, not this commit",
      info.deployed_commit_for("failed", NEW, None) == "")
check("carry: an empty previous dict yields empty",
      info.deployed_commit_for("failed", NEW, {}) == "")
check("carry: LEGACY file with no deployed_commit and status ok counts commit",
      info.deployed_commit_for("failed", NEW,
                               {"commit": OLD, "status": "ok"}) == OLD)
check("carry: LEGACY file whose own run FAILED does not count its commit",
      info.deployed_commit_for("failed", NEW,
                               {"commit": OLD, "status": "failed"}) == "")
check("carry: LEGACY file with no status at all counts its commit",
      info.deployed_commit_for("failed", NEW, {"commit": OLD}) == OLD)
check("carry: an explicit null deployed_commit is honoured over `commit`",
      info.deployed_commit_for("failed", NEW,
                               {"deployed_commit": None, "commit": OLD}) == "")


# ---- 3. messages -----------------------------------------------------------

check("messages: none renders an empty array", info.message_entries([]) == [])
check("messages: level and text are split into fields",
      info.message_entries([("WARN", "caddy config failed validation")])
      == [{"level": "WARN", "text": "caddy config failed validation"}])
check("messages: order is the order they were recorded",
      [m["text"] for m in info.message_entries([("WARN", "a"), ("ERROR", "b")])]
      == ["a", "b"])
check("messages: a text containing a TAB survives whole — the bash partitioned "
      "on the FIRST tab only, so the remainder stayed in the text",
      info.message_entries([("WARN", "a\tb")])
      == [{"level": "WARN", "text": "a\tb"}])
check("messages: a MULTI-LINE text degrades exactly as the TSV round-trip did",
      info.message_entries([("WARN", "first\nsecond")])
      == [{"level": "WARN", "text": "first"}, {"level": "second", "text": ""}])


# ---- 4. the URL ------------------------------------------------------------

check("url: built from the environment's domain and repo",
      info.commit_url("example.com", "operator/node-config", NEW)
      == f"https://git.example.com/operator/node-config/commit/{NEW}")
check("url: unset domain and repo fall back, so a deploy without .env still "
      "writes a well-formed file",
      info.commit_url("", "", NEW)
      == f"https://git.localhost/operator/node-config/commit/{NEW}")


# ---- 5. the whole artifact -------------------------------------------------

built = info.build(status="warning", timestamp="2026-09-20T15:09:35Z",
                   commit=NEW, short_hash="1111111", url="u",
                   messages=[("WARN", "x")], previous={"deployed_commit": OLD})
check("artifact: key order is the bash's, so a git diff of the file shows only "
      "values that moved",
      list(built) == ["timestamp", "commit", "short_hash", "url", "status",
                      "deployed_commit", "messages"], detail=str(list(built)))
check("artifact: renders as indent-2 JSON with a trailing newline",
      info.render(built).endswith("}\n")
      and json.loads(info.render(built)) == built)
check("artifact: non-ASCII survives the round trip",
      json.loads(info.render(info.build(
          status="warning", timestamp="t", commit=NEW, short_hash="s", url="u",
          messages=[("WARN", "vendor staging failed — see above")],
          previous=None)))["messages"][0]["text"]
      == "vendor staging failed — see above")

print(f"\n{'FAILED' if FAIL else 'PASS'}: node_deploy.info ({FAIL} failure(s))")
sys.exit(1 if FAIL else 0)
