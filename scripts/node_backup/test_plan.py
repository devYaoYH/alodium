#!/usr/bin/env python3
"""
Offline tests for node_backup.plan. No Docker, no daemon, no fault injection:
the inputs are a compose-config dict, a set of volume names and a set of
services, so every branch is a dict literal.

Covers the classifications a backup gets wrong:

  - volumes: include / clean skip / manifest drift / ran-but-volume-gone
  - the empty include set is flagged fatal
  - core + manifest volumes dedupe (radicale_data is both)
  - volume_owners reads only real volume mounts, not bind mounts
  - dumps: running -> dump, has-run-but-down -> degraded, never-ran -> skip
  - a failed pg_dump is degraded AND the half-written file is deleted
  - EQUIVALENCE with the merged bash implementation: testdata/bash_baseline.json
    holds the live node's state and the classification bash produced from it
    (node-config a248c6f, PR #137); these functions must reproduce it exactly

Run:  python3 scripts/node_backup/test_plan.py     (from the repo root)
      ./scripts/verify-config.sh                   (runs it with the rest)
Stdlib only.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
spec = importlib.util.spec_from_file_location("node_backup.plan", HERE / "plan.py")
plan = importlib.util.module_from_spec(spec)
sys.modules.setdefault("node_backup", importlib.import_module("node_backup"))
spec.loader.exec_module(plan)

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


# A miniature node: two core-ish volumes, one app volume whose service has run,
# one whose profile has never run, and a bind mount that must be ignored.
COMPOSE = {
    "volumes": {
        "forgejo_data": {}, "litellm_db": {}, "miniflux_db": {},
        "bridge_data": {}, "caddy_config": {},
    },
    "services": {
        "forgejo": {"volumes": [
            {"type": "volume", "source": "forgejo_data", "target": "/data"},
            {"type": "bind", "source": "./config", "target": "/etc/x"},
        ]},
        "litellm-db": {"volumes": [{"type": "volume", "source": "litellm_db"}]},
        "miniflux-db": {"volumes": [{"type": "volume", "source": "miniflux_db"}]},
        "gog-bridge": {"volumes": [{"type": "volume", "source": "bridge_data"}]},
        "caddy": {"volumes": [{"type": "volume", "source": "caddy_config"}]},
        "homepage": {},                      # no volumes key at all
    },
}
OWNERS = plan.volume_owners(COMPOSE)


# ---- 1. volume_owners ------------------------------------------------------

check("owners: maps a volume to its service", OWNERS["forgejo_data"] == ["forgejo"])
check("owners: ignores bind mounts", "./config" not in OWNERS)
check("owners: a service with no volumes is harmless", OWNERS["caddy_config"] == ["caddy"])
check("owners: every declared volume appears", set(OWNERS) == set(COMPOSE["volumes"]))


# ---- 2. declared_volumes: dedupe -------------------------------------------

deduped = plan.declared_volumes(["radicale_data", "memos_data"],
                                core=["forgejo_data", "radicale_data"])
check("declared: order preserved, core first",
      deduped == ["forgejo_data", "radicale_data", "memos_data"], detail=str(deduped))
check("declared: radicale_data appears once", deduped.count("radicale_data") == 1)
check("declared: real CORE_VOLUMES has no duplicates",
      len(plan.CORE_VOLUMES) == len(set(plan.CORE_VOLUMES)))


# ---- 3. plan_volumes: the four branches ------------------------------------

EXISTING = {"sovereign-node_forgejo_data", "sovereign-node_litellm_db",
            "sovereign-node_miniflux_db"}
RAN = {"forgejo", "litellm-db", "miniflux-db", "caddy", "homepage"}

p = plan.plan_volumes(["forgejo_data", "litellm_db", "miniflux_db", "bridge_data"],
                      OWNERS, EXISTING, RAN)
check("volumes: existing ones are included",
      p.include == ["sovereign-node_forgejo_data", "sovereign-node_litellm_db",
                    "sovereign-node_miniflux_db"], detail=str(p.include))
check("volumes: never-run profile is a clean skip", len(p.skipped) == 1 and not p.missing)
check("volumes: the skip names the service that would own it",
      "gog-bridge" in p.skipped[0], detail=p.skipped[0])
check("volumes: skip is not fatal on its own", p.fatal is False)

# manifest drift: declared, but no compose service defines it
p2 = plan.plan_volumes(["forgejo_data", "ghost_data"], OWNERS, EXISTING, RAN)
check("drift: missing, not skipped", len(p2.missing) == 1 and not p2.skipped)
check("drift: message says manifest drift", "manifest drift" in p2.missing[0],
      detail=p2.missing[0])
check("drift: fatal", p2.fatal is True)

# the service has run here, but its volume is gone
p3 = plan.plan_volumes(["caddy_config"], OWNERS, EXISTING, RAN)
check("gone: a volume whose service has run is missing, not skipped",
      len(p3.missing) == 1 and not p3.skipped)
check("gone: message names the service that ran", "'caddy'" in p3.missing[0],
      detail=p3.missing[0])
check("gone: fatal", p3.fatal is True)

# the same volume, on a node where that service never ran -> clean skip
p4 = plan.plan_volumes(["caddy_config"], OWNERS, EXISTING, ran_services=set())
check("gone-vs-skip: identical input, no container -> skip",
      len(p4.skipped) == 1 and not p4.missing, detail=str(p4))

# empty include set
p5 = plan.plan_volumes(["bridge_data"], OWNERS, EXISTING, ran_services=set())
check("empty: nothing to include is fatal", p5.fatal is True and not p5.include)

# the full name is what gets mounted, not the short one
check("volumes: include uses the project-prefixed name",
      all(v.startswith("sovereign-node_") for v in p.include))


# ---- 4. manifest_backup_volumes --------------------------------------------

with tempfile.TemporaryDirectory() as td:
    (Path(td) / "memos.toml").write_text(
        '[app]\nname="memos"\n[lifecycle]\nbackup = ["memos_data"]\n')
    (Path(td) / "prefixed.toml").write_text(
        '[lifecycle]\nbackup = ["sovereign-node_redash_db"]\n')
    (Path(td) / "none.toml").write_text('[app]\nname="x"\n')
    (Path(td) / "app.example.toml").write_text('[lifecycle]\nbackup = ["notes-data"]\n')
    vols = plan.manifest_backup_volumes(Path(td))
check("manifest: reads [lifecycle].backup", "memos_data" in vols)
check("manifest: strips the project prefix", "redash_db" in vols, detail=str(vols))
check("manifest: a manifest with no backup list contributes nothing", len(vols) == 2)
check("manifest: the example manifest is not inventory", "notes-data" not in vols)


# ---- 5. plan_dumps: the three outcomes -------------------------------------

SPECS = [
    plan.DumpSpec("litellm-db", "litellm-db", "litellm", "litellm", "litellm.sql"),
    plan.DumpSpec("redash-db", "redash-db", "redash", "redash", "redash.sql"),
    plan.DumpSpec("egress-audit-db", "egress-audit-db", "e", "egress_audit", "e.sql"),
]
d = plan.plan_dumps(SPECS,
                    running_containers={"litellm-db"},
                    ran_services={"litellm-db", "redash-db"})
check("dumps: a running container is dumped",
      [s.container for s in d.to_run] == ["litellm-db"])
check("dumps: has run here but is down -> degraded", len(d.degraded) == 1)
check("dumps: the degraded message names the service", "'redash-db'" in d.degraded[0],
      detail=d.degraded[0])
check("dumps: never ran here -> clean skip, not degraded",
      len(d.skipped) == 1 and "egress_audit" in d.skipped[0], detail=str(d.skipped))
check("dumps: a down container is NOT silently omitted",
      any("redash" in x for x in d.degraded))

all_up = plan.plan_dumps(SPECS, {s.container for s in SPECS}, set())
check("dumps: everything up -> nothing degraded, nothing skipped",
      len(all_up.to_run) == 3 and not all_up.degraded and not all_up.skipped)


# ---- 6. execute_dumps: a failed pg_dump is degraded AND cleaned up ---------

with tempfile.TemporaryDirectory() as td:
    good = plan.DumpSpec("a-db", "a", "u", "a", "a.sql")
    bad = plan.DumpSpec("b-db", "b", "u", "b", "b.sql")
    dplan = plan.DumpPlan(to_run=[good, bad])
    unlinked = []

    def fake_pg_dump(spec, path):
        # Both write something: the failing one leaves a half-written file,
        # which is exactly the case that must not survive into a snapshot.
        Path(path).write_text("-- partial\n")
        return spec is good

    written, degraded = plan.execute_dumps(
        dplan, td, fake_pg_dump, lambda p: (unlinked.append(Path(p).name),
                                            Path(p).unlink(missing_ok=True)))
    left = sorted(os.listdir(td))

check("execute: the good dump is reported written", written == ["a.sql"], detail=str(written))
check("execute: the failed dump is degraded", len(degraded) == 1)
check("execute: the degraded message names the container", "'b-db'" in degraded[0],
      detail=degraded[0])
check("execute: the half-written file is deleted", unlinked == ["b.sql"] and left == ["a.sql"],
      detail=f"unlinked={unlinked} left={left}")


# ---- 7. the shipped dump list ----------------------------------------------

check("shipped: three dumps, one per known Postgres", len(plan.DUMP_SPECS) == 3)
check("shipped: litellm-db is not special-cased into an unconditional dump",
      all(isinstance(s, plan.DumpSpec) for s in plan.DUMP_SPECS))
check("shipped: every spec names a container and a service",
      all(s.container and s.service for s in plan.DUMP_SPECS))


# ---- 8. equivalence with the merged bash implementation --------------------
# This is the port's whole claim: same inputs, same decisions. The fixture is a
# recording of the live node plus what bash decided there, so a refactor that
# quietly reclassifies a volume fails here rather than in six months when
# someone tries to restore.

BASELINE = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())

bl_owners = BASELINE["volume_owners"]
bl_declared = BASELINE["declared_volumes"]
bl_plan = plan.plan_volumes(bl_declared, bl_owners,
                            BASELINE["existing_volumes"], BASELINE["ran_services"])

check("equiv: the declared list still matches what bash enumerated",
      plan.declared_volumes(bl_declared[len(plan.CORE_VOLUMES):]) == bl_declared,
      detail=str(bl_declared))
check("equiv: same include list, same order",
      bl_plan.include == BASELINE["bash_include"],
      detail=f"py={bl_plan.include} bash={BASELINE['bash_include']}")
check("equiv: same skip list",
      bl_plan.skipped == BASELINE["bash_skipped"],
      detail=f"py={bl_plan.skipped} bash={BASELINE['bash_skipped']}")
check("equiv: bash found nothing missing, and neither do we", bl_plan.missing == [])
check("equiv: ten volumes, two skipped — the node as it actually was",
      len(bl_plan.include) == 10 and len(bl_plan.skipped) == 2)

bl_dumps = plan.plan_dumps(plan.DUMP_SPECS, BASELINE["bash_running_containers"],
                           BASELINE["ran_services"])
check("equiv: same dump set as bash wrote",
      [s.filename for s in bl_dumps.to_run] == BASELINE["bash_dumps"],
      detail=f"py={[s.filename for s in bl_dumps.to_run]} bash={BASELINE['bash_dumps']}")
check("equiv: bash degraded nothing on that node, and neither do we",
      bl_dumps.degraded == [] and bl_dumps.skipped == [])

# The same recording with one container taken away. The merged bash already
# degrades here (node-config 75afad1) and so must the port: this is the branch
# no single live run can exercise without stopping a production database, which
# is exactly why it belongs in a fixture and not in a fault-injection session.
down = [c for c in BASELINE["bash_running_containers"] if c != "miniflux-db"]
partial = plan.plan_dumps(plan.DUMP_SPECS, down, BASELINE["ran_services"])
check("equiv: a down miniflux-db is degraded, never silently omitted",
      len(partial.degraded) == 1 and "miniflux" in partial.degraded[0],
      detail=str(partial.degraded))
check("equiv: the other two dumps still run", len(partial.to_run) == 2)

# And with litellm-db down — the case the PRE-#137 script turned into a total
# abort, because its dump was unconditional. Volumes still get backed up.
no_litellm = [c for c in BASELINE["bash_running_containers"] if c != "litellm-db"]
without = plan.plan_dumps(plan.DUMP_SPECS, no_litellm, BASELINE["ran_services"])
check("equiv: litellm-db down degrades the run, it does not abort it",
      len(without.degraded) == 1 and len(without.to_run) == 2,
      detail=str(without.degraded))


print()
if FAIL == 0:
    print("test_plan: PASS")
    sys.exit(0)
print(f"test_plan: FAIL ({FAIL} failures)")
sys.exit(1)
