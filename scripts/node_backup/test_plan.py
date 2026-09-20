#!/usr/bin/env python3
"""
Offline tests for node_backup.plan (volumes). No Docker, no daemon, no fault injection:
the inputs are a compose-config dict, a set of volume names and a set of
services, so every branch is a dict literal.

Covers the classifications a backup gets wrong:

  - volumes: include / clean skip / manifest drift / ran-but-volume-gone
  - the empty include set is flagged fatal
  - core + manifest volumes dedupe (radicale_data is both)
  - volume_owners reads only real volume mounts, not bind mounts

Dump classification moved to test_dumps.py when the dump table became
manifest-declared (coordination#71 PR 2); the volume equivalence below stays
here because it is this module's claim.
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


# ---- 5. equivalence with the merged bash implementation --------------------
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

print()
if FAIL == 0:
    print("test_plan: PASS")
    sys.exit(0)
print(f"test_plan: FAIL ({FAIL} failures)")
sys.exit(1)
