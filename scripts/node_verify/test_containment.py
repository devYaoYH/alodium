#!/usr/bin/env python3
"""
Offline tests for node_verify.containment — the copilot's code/data-plane split.

Every rule gets a fixture that BREAKS it. A containment check nobody has
watched reject anything is indistinguishable from one that checks nothing, and
this is the check standing between a config PR and a capable agent with
data-plane reach.

  - the shipped apps/copilot/compose.yaml (recorded in testdata) passes
  - copilot on a data-plane net, or on `edge`, is rejected
  - copilot mounting the docker socket, or any host path but COPILOT.md, is
    rejected — in both compose volume spellings
  - copilot-egress on an unexpected net is rejected; `edge` on it is allowed,
    because it IS the one controlled hole
  - copilot-headroom anywhere but copilot-egress is rejected, `edge` loudest
  - the EGRESS_ALLOW default must be Anthropic-owned: a widened default, a
    lookalike domain and an empty default are each rejected
  - SKIP when the app is not deployed — and a SKIP is not a pass
  - EQUIVALENCE: the recorded services produce bash's exact verdict

Run:  python3 scripts/node_verify/test_containment.py     (from the repo root)
      ./scripts/verify-config.sh                          (runs it with the rest)
PyYAML + stdlib only.
"""

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_verify import containment                             # noqa: E402
from node_verify.report import FAIL as R_FAIL, OK, SKIP         # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


BASELINE = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())
SHIPPED = {"services": BASELINE["inputs"]["copilot_services"]}


def mutate(**services):
    """The shipped fragment with some services replaced or extended."""
    doc = copy.deepcopy(SHIPPED)
    for name, patch in services.items():
        name = name.replace("_", "-")
        doc["services"].setdefault(name, {}).update(patch)
    return doc


def rejects(name, doc, needle):
    errs = containment.violations(doc)
    check(name, any(needle in e for e in errs), detail=f"errors={errs}")


# ---- 1. the shipped config passes ------------------------------------------

check("shipped: apps/copilot/compose.yaml has no violations",
      containment.violations(SHIPPED) == [],
      detail=str(containment.violations(SHIPPED)))


# ---- 2. the copilot's own reach --------------------------------------------

rejects("copilot on a data-plane net is rejected",
        mutate(copilot={"networks": ["front", "agents", "copilot-egress", "data"]}),
        "copilot joins forbidden network(s) ['data']")

doc = mutate(copilot={"networks": ["front", "agents", "copilot-egress", "edge"]})
errs = containment.violations(doc)
check("copilot on `edge` is rejected twice — forbidden net AND direct internet",
      sum(1 for e in errs if e.startswith("copilot ")) == 2, detail=str(errs))
check("the `edge` message names the only allowed egress",
      any("its only egress is via copilot-egress" in e for e in errs), detail=str(errs))

rejects("the mapping spelling of networks: is read too",
        mutate(copilot={"networks": {"front": None, "agents": None,
                                     "copilot-egress": None, "secrets-net": None}}),
        "copilot joins forbidden network(s) ['secrets-net']")

check("a copilot on exactly front/agents/copilot-egress is accepted",
      containment.violations(
          mutate(copilot={"networks": ["front", "agents", "copilot-egress"]})) == [])


# ---- 3. mounts: no socket, no secrets --------------------------------------

rejects("copilot mounting the docker socket is rejected",
        mutate(copilot={"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}),
        "copilot mounts the docker socket — forbidden (no host control).")

rejects("copilot mounting a host path is rejected",
        mutate(copilot={"volumes": ["/etc/passwd:/etc/passwd:ro"]}),
        "copilot mounts host path '/etc/passwd'")

rejects("copilot mounting ./secrets is rejected",
        mutate(copilot={"volumes": ["./secrets:/secrets:ro"]}),
        "copilot mounts host path './secrets'")

check("the COPILOT.md mount is the one host path allowed",
      containment.violations(
          mutate(copilot={"volumes": ["./apps/copilot/COPILOT.md:/COPILOT.md:ro"]})) == [],
      detail=str(containment.violations(
          mutate(copilot={"volumes": ["./apps/copilot/COPILOT.md:/COPILOT.md:ro"]}))))

rejects("the long-form {source: …} volume spelling is read too",
        mutate(copilot={"volumes": [{"type": "bind",
                                     "source": "/var/run/docker.sock",
                                     "target": "/var/run/docker.sock"}]}),
        "copilot mounts the docker socket")

check("a named volume is not a host path",
      containment.violations(
          mutate(copilot={"volumes": ["copilot_workspace:/workspace"]})) == [])


# ---- 4. the egress companion and the headroom proxy ------------------------

rejects("copilot-egress on an unexpected net is rejected",
        mutate(copilot_egress={"networks": ["copilot-egress", "edge", "agents"]}),
        "copilot-egress joins unexpected network(s) ['agents']")

check("copilot-egress on `edge` is allowed — it IS the controlled hole",
      containment.violations(
          mutate(copilot_egress={"networks": ["copilot-egress", "edge"]})) == [])

rejects("copilot-headroom on `front` is rejected",
        mutate(copilot_headroom={"networks": ["copilot-egress", "front"]}),
        "copilot-headroom joins forbidden network(s) ['front']")

doc = mutate(copilot_headroom={"networks": ["copilot-egress", "edge"]})
errs = containment.violations(doc)
check("copilot-headroom on `edge` is rejected twice, loudly",
      sum(1 for e in errs if e.startswith("copilot-headroom ")) == 2, detail=str(errs))
check("the headroom `edge` message says why: a third-party proxy with general internet",
      any("would give a third-party proxy general internet" in e for e in errs),
      detail=str(errs))

rejects("copilot-headroom mounting the docker socket is rejected",
        mutate(copilot_headroom={"volumes": ["/var/run/docker.sock:/sock"]}),
        "copilot-headroom mounts the docker socket — forbidden.")


# ---- 5. the egress allowlist default ---------------------------------------

def with_allow(value):
    return mutate(copilot_egress={"environment": {"EGRESS_ALLOW": value}})


check("the shipped Anthropic-only default is accepted",
      containment.violations(
          with_allow("${EGRESS_ALLOW:-api.anthropic.com,claude.com}")) == [],
      detail=str(containment.violations(
          with_allow("${EGRESS_ALLOW:-api.anthropic.com,claude.com}"))))

rejects("a widened default is rejected",
        with_allow("${EGRESS_ALLOW:-api.anthropic.com,github.com}"),
        "EGRESS_ALLOW default entry 'github.com' is not an Anthropic-owned host")

rejects("a lookalike domain is rejected",
        with_allow("${EGRESS_ALLOW:-anthropic.com.evil.net}"),
        "'anthropic.com.evil.net' is not an Anthropic-owned host")

rejects("an empty default is rejected",
        with_allow("${EGRESS_ALLOW:-}"),
        "copilot-egress EGRESS_ALLOW has no default allowlist.")

rejects("a missing EGRESS_ALLOW is rejected",
        mutate(copilot_egress={"environment": {}}),
        "copilot-egress EGRESS_ALLOW has no default allowlist.")

check("a subdomain of an allowed domain is accepted",
      containment.violations(with_allow("${EGRESS_ALLOW:-.api.anthropic.com}")) == [])


# ---- 6. the section verdict ------------------------------------------------

r = containment.check_copilot(SHIPPED)
check("verdict: the shipped config is OK", r.status == OK)
check("verdict: the OK note is the one the operator knows",
      r.note == ("OK: copilot reaches only front/agents/egress; no socket, "
                 "secrets, or data-plane net"), detail=str(r.note))

r = containment.check_copilot(mutate(copilot={"networks": ["front", "data"]}))
check("verdict: a violation FAILS the section", r.status == R_FAIL)
check("verdict: the FAIL note is unchanged",
      r.note == "FAIL: copilot containment violated —")
check("verdict: each violation is printed as a bulleted, indented line",
      all(line.startswith("  - ") for line in r.detail) and r.detail_indent == "    ",
      detail=str(r.detail))

r = containment.check_copilot(None)
check("verdict: no apps/copilot/compose.yaml is a SKIP", r.status == SKIP)
check("verdict: the SKIP names the missing file",
      r.note == "SKIP: no apps/copilot/compose.yaml")


# ---- 7. equivalence with the merged bash implementation --------------------

want = BASELINE["bash_verdicts"]["copilot containment"]
r = containment.check_copilot(SHIPPED)
check("equiv: same status as bash", r.status == want["status"])
check("equiv: same note as bash", r.note == want["note"], detail=repr(r.note))
check("equiv: bash found no violations, and neither do we",
      list(r.passthrough) == want["passthrough"] and r.detail == ())
check("equiv: all three copilot services are still in the recorded fragment",
      sorted(SHIPPED["services"]) == ["copilot", "copilot-egress", "copilot-headroom"],
      detail=str(sorted(SHIPPED["services"])))


print()
if FAIL == 0:
    print("test_containment: PASS")
    sys.exit(0)
print(f"test_containment: FAIL ({FAIL} failures)")
sys.exit(1)
