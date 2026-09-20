#!/usr/bin/env python3
"""
Offline tests for node_backup.dumps. No Docker, no daemon: manifests are
written into a temp dir, the node's state is a set of names, and the dump
drivers are injected, so every branch is a dict literal.

Covers the decisions a dump gets wrong:

  - a manifest that declares nothing is UNDECLARED, one that declares nonsense
    is MALFORMED, and the two never collapse into one message
  - two apps cannot claim the same filename inside the snapshot
  - postgres classifies on the CONTAINER (only the running engine can dump it)
  - sqlite classifies on the VOLUME (we read it :ro; a stopped app is the easy
    case, not a degraded one) — the asymmetry is asserted, not tolerated
  - a dump that cannot be read back is degraded AND deleted, in the same list
    as one that was never taken
  - "installed, never initialized" is a clean skip, not a hole
  - the repo's own manifests all declare `dump` (fenced section 7 — that one
    reads repo state rather than fixtures)
  - the three databases the merged bash dumped are all still dumped

Run:  python3 scripts/node_backup/test_dumps.py    (from the repo root)
      ./scripts/verify-config.sh                   (runs it with the rest)
Stdlib only.
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_backup import dumps  # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}" + (f"\n        {detail}" if detail else ""))


def write_manifests(tmp, files: dict) -> Path:
    d = Path(tmp)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


PG_ENTRY = ('{ kind = "postgres", service = "a-db", container = "a-db", '
            'user = "u", database = "a", file = "a.dump" }')
SQ_ENTRY = ('{ kind = "sqlite", service = "b", volume = "b_data", '
            'path = "b.db", file = "b.db" }')


# ---- 1. manifest parsing ---------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    md = write_manifests(td, {
        "a.toml": f"[lifecycle]\ndump = [\n  {PG_ENTRY},\n]\n",
        "b.toml": f"[lifecycle]\ndump = [\n  {SQ_ENTRY},\n]\n",
        "empty.toml": "[lifecycle]\ndump = []   # holds no database\n",
        "app.example.toml": f"[lifecycle]\ndump = [\n  {PG_ENTRY},\n]\n",
    })
    specs, errors = dumps.manifest_dumps(md)

check("parse: a postgres declaration becomes a PostgresDump",
      any(isinstance(s, dumps.PostgresDump) and s.database == "a" for s in specs))
check("parse: a sqlite declaration becomes a SqliteDump",
      any(isinstance(s, dumps.SqliteDump) and s.path == "b.db" for s in specs))
check("parse: `dump = []` is a declaration, not an omission", errors == [],
      detail=str([str(e) for e in errors]))
check("parse: the example manifest is documentation, not inventory",
      len(specs) == 2, detail=str([s.filename for s in specs]))

with tempfile.TemporaryDirectory() as td:
    md = write_manifests(td, {"silent.toml": "[lifecycle]\nbackup = [\"x\"]\n"})
    _, errors = dumps.manifest_dumps(md)

check("parse: a manifest with no dump key is UNDECLARED",
      len(errors) == 1 and errors[0].category == dumps.UNDECLARED)
check("parse: the undeclared message names the file and the key",
      "silent.toml" in str(errors[0]) and "[lifecycle] dump" in str(errors[0]),
      detail=str(errors[0]))

BAD = {
    "kind.toml": '[lifecycle]\ndump = [{ kind = "mysql", service = "s", file = "f" }]\n',
    "fields.toml": '[lifecycle]\ndump = [{ kind = "postgres", service = "s", file = "f" }]\n',
    "unknown.toml": ('[lifecycle]\ndump = [{ kind = "sqlite", service = "s", '
                     'volume = "v", path = "p", file = "f", colour = "blue" }]\n'),
    "path.toml": ('[lifecycle]\ndump = [{ kind = "sqlite", service = "s", '
                  'volume = "v", path = "p", file = "../escape.db" }]\n'),
    "notalist.toml": '[lifecycle]\ndump = "yes please"\n',
    "broken.toml": "[lifecycle\ndump = []\n",
}
for name, text in BAD.items():
    with tempfile.TemporaryDirectory() as td:
        md = write_manifests(td, {name: text})
        _, errs = dumps.manifest_dumps(md)
    check(f"parse: {name} is MALFORMED, not silently ignored",
          len(errs) == 1 and errs[0].category == dumps.MALFORMED,
          detail=str([str(e) for e in errs]))

with tempfile.TemporaryDirectory() as td:
    md = write_manifests(td, {
        "one.toml": f"[lifecycle]\ndump = [\n  {PG_ENTRY},\n]\n",
        "two.toml": f"[lifecycle]\ndump = [\n  {PG_ENTRY},\n]\n",
    })
    specs, errors = dumps.manifest_dumps(md)

check("parse: two apps cannot claim one filename in the snapshot",
      len(specs) == 1 and len(errors) == 1 and errors[0].category == dumps.MALFORMED)
check("parse: the collision message names the manifest that got there first",
      "one.toml" in str(errors[0]), detail=str(errors[0]))


# ---- 2. declared_dumps: core first, and core names are reserved ------------

with tempfile.TemporaryDirectory() as td:
    md = write_manifests(td, {"a.toml": f"[lifecycle]\ndump = [\n  {PG_ENTRY},\n]\n"})
    specs, errors = dumps.declared_dumps(md)
    core_clash = write_manifests(td, {
        "clash.toml": ('[lifecycle]\ndump = [{ kind = "sqlite", service = "x", '
                       'volume = "v", path = "p", file = "gitea.db" }]\n')})
    _, clash_errors = dumps.declared_dumps(core_clash)

check("declared: the core stack comes first",
      [s.filename for s in specs][:len(dumps.CORE_DUMPS)]
      == [s.filename for s in dumps.CORE_DUMPS])
check("declared: a manifest cannot shadow a core dump filename",
      any("core" in e.manifest for e in clash_errors),
      detail=str([str(e) for e in clash_errors]))
check("declared: the core list itself has no duplicate filenames",
      len({s.filename for s in dumps.CORE_DUMPS}) == len(dumps.CORE_DUMPS))


# ---- 3. plan_dumps: postgres on the container, sqlite on the volume --------

PG = dumps.PostgresDump("pg-svc", "pg.dump", "pg-db", "u", "pg")
SQ = dumps.SqliteDump("sq-svc", "sq.db", "sq_data", "sq.db")

p = dumps.plan_dumps([PG], running_containers={"pg-db"}, ran_services=set())
check("postgres: a running container is dumped", p.to_run == [PG])

p = dumps.plan_dumps([PG], running_containers=set(), ran_services={"pg-svc"})
check("postgres: has run here but is down -> degraded",
      len(p.degraded) == 1 and not p.to_run)
check("postgres: the degraded message names the service", "'pg-svc'" in p.degraded[0],
      detail=p.degraded[0])

p = dumps.plan_dumps([PG], running_containers=set(), ran_services=set())
check("postgres: never run here -> clean skip, not degraded",
      len(p.skipped) == 1 and not p.degraded)

p = dumps.plan_dumps([SQ], running_containers=set(), ran_services={"sq-svc"},
                     existing_volumes={"sovereign-node_sq_data"})
check("sqlite: the volume is what matters, not the container", p.to_run == [SQ])
check("sqlite: a stopped app is NOT degraded — that is the easy case",
      not p.degraded,
      detail="classifying sqlite on container state would degrade every "
             "deliberate shutdown")

p = dumps.plan_dumps([SQ], running_containers=set(), ran_services={"sq-svc"},
                     existing_volumes=set())
check("sqlite: volume gone for a service that has run -> degraded",
      len(p.degraded) == 1 and "sovereign-node_sq_data" in p.degraded[0],
      detail=str(p.degraded))

p = dumps.plan_dumps([SQ], running_containers=set(), ran_services=set(),
                     existing_volumes=set())
check("sqlite: never run here -> clean skip", len(p.skipped) == 1 and not p.degraded)

p = dumps.plan_dumps([PG, SQ], running_containers={"pg-db"}, ran_services=set(),
                     existing_volumes={"sovereign-node_sq_data"})
check("mixed: both kinds plan together", len(p.to_run) == 2)


# ---- 4. declaration_outcomes: two categories, two sentences ----------------

lines = dumps.declaration_outcomes([
    dumps.DumpError(dumps.UNDECLARED, "a.toml", "has no [lifecycle] dump"),
    dumps.DumpError(dumps.MALFORMED, "b.toml", "dump[0]: kind='mysql'"),
])
check("outcomes: undeclared says the data is NOT backed up",
      "NOT backed up" in lines[0], detail=lines[0])
check("outcomes: malformed says it could not be parsed, so it did not run",
      "could not be parsed" in lines[1], detail=lines[1])
check("outcomes: the two read differently — 3am needs the difference",
      lines[0] != lines[1])


# ---- 5. execute_dumps: written / skipped / degraded-and-deleted ------------

def run_with(results_by_file, specs):
    """Execute a plan with injected drivers. Returns (written, degraded,
    skipped, unlinked, calls)."""
    calls = {"pg": 0, "sq": 0, "pg_specs": [], "sq_specs": []}
    unlinked = []

    def fake_pg(s, _dir):
        calls["pg"] += 1
        calls["pg_specs"] = [x.filename for x in s]
        return {x.filename: results_by_file[x.filename] for x in s
                if x.filename in results_by_file}

    def fake_sq(s, _dir):
        calls["sq"] += 1
        calls["sq_specs"] = [x.filename for x in s]
        return {x.filename: results_by_file[x.filename] for x in s
                if x.filename in results_by_file}

    with tempfile.TemporaryDirectory() as td:
        for s in specs:
            (Path(td) / s.filename).write_text("-- bytes on disk\n")
        written, degraded, skipped = dumps.execute_dumps(
            dumps.DumpPlan(to_run=list(specs)), td, fake_pg, fake_sq,
            lambda p: (unlinked.append(Path(p).name), Path(p).unlink(missing_ok=True)))
        left = sorted(x.name for x in Path(td).iterdir())
    return written, degraded, skipped, unlinked, left, calls


written, degraded, skipped, unlinked, left, calls = run_with(
    {"pg.dump": dumps.DumpResult(dumps.OK, "12 entries"),
     "sq.db": dumps.DumpResult(dumps.OK, "ok")}, [PG, SQ])
check("execute: verified dumps are reported written", written == ["pg.dump", "sq.db"])
check("execute: nothing degraded when both verify", not degraded and not skipped)
check("execute: each driver is called once, with only its own kind",
      calls["pg"] == 1 and calls["sq"] == 1
      and calls["pg_specs"] == ["pg.dump"] and calls["sq_specs"] == ["sq.db"],
      detail=str(calls))

written, degraded, skipped, unlinked, left, _ = run_with(
    {"pg.dump": dumps.DumpResult(dumps.FAILED, "written but unreadable by pg_restore"),
     "sq.db": dumps.DumpResult(dumps.OK, "ok")}, [PG, SQ])
check("execute: a dump that fails readback is degraded", len(degraded) == 1)
check("execute: the unverifiable file is DELETED, not shipped",
      unlinked == ["pg.dump"] and left == ["sq.db"],
      detail=f"unlinked={unlinked} left={left}")
check("execute: verification failure lands in the same list as a missing dump",
      "unreadable" in degraded[0], detail=degraded[0])

written, degraded, skipped, unlinked, left, _ = run_with(
    {"sq.db": dumps.DumpResult(dumps.MISSING, "the app has not created it yet")}, [SQ])
check("execute: installed-but-never-initialized is a clean skip",
      len(skipped) == 1 and not degraded, detail=str(skipped) + str(degraded))
check("execute: a clean skip deletes nothing", unlinked == [])

written, degraded, skipped, unlinked, left, _ = run_with({}, [PG])
check("execute: a driver that returns no verdict is degraded, never assumed ok",
      len(degraded) == 1 and not written, detail=str(degraded))


# ---- 6. what bash dumped, we still dump ------------------------------------
# testdata/bash_baseline.json records the merged bash implementation's decisions
# on this node (node-config a248c6f). The FILENAMES changed deliberately in this
# PR — plain .sql became -Fc .dump — so this asserts the databases, which is the
# property that matters: no database bash captured may quietly stop being
# captured. The baseline file itself is not edited.

BASELINE = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())
bash_databases = {f.rsplit(".", 1)[0].replace("_", "-") for f in BASELINE["bash_dumps"]}

with tempfile.TemporaryDirectory() as td:
    repo_specs, repo_errors = dumps.declared_dumps(Path("manifest").resolve())
planned = dumps.plan_dumps(repo_specs, BASELINE["bash_running_containers"],
                           BASELINE["ran_services"],
                           BASELINE["existing_volumes"])
now_databases = {s.label.replace("_", "-") for s in planned.to_run}

check("equiv: every database bash dumped is still dumped",
      bash_databases <= now_databases,
      detail=f"bash={sorted(bash_databases)} now={sorted(now_databases)}")
check("equiv: and this PR adds more than bash had",
      len(now_databases) > len(bash_databases),
      detail=f"now={sorted(now_databases)}")


# ---- 7. THE REPO'S OWN MANIFESTS -------------------------------------------
# Not a unit test over fixtures: this reads the real manifest/ directory and is
# the gate that makes `[lifecycle] dump` mandatory. verify-config.sh hard-fails
# here, at the point where a missing declaration is cheap to fix; the backup
# itself only degrades, so one incomplete manifest never stops the other twelve
# apps being protected.

_, repo_errors = dumps.declared_dumps(Path("manifest").resolve())
check("manifests: every real manifest declares [lifecycle] dump",
      repo_errors == [],
      detail="\n        ".join(str(e) for e in repo_errors) or "")

print()
if FAIL == 0:
    print("test_dumps: PASS")
    sys.exit(0)
print(f"test_dumps: FAIL ({FAIL} failures)")
sys.exit(1)
