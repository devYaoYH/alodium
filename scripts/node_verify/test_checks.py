#!/usr/bin/env python3
"""
Offline tests for node_verify.checks — yaml parse, dispatch tiers, pricing.

Both branches of every check, from dict literals. No repo, no daemon: the old
way to ask "does this still bite?" was to break the tree on purpose, which is
why nobody asked.

  - yaml: a clean set passes; a duplicate key and a doubled `include` each FAIL
    with the file named; the loader's self-check runs FIRST and short-circuits
    before a single file is read
  - tiers: SKIP when not deployed, OK lists the models, FAIL names exactly the
    unknown ones
  - pricing: a model missing either cost FAILS and the message points at
    model-pricing.sh; non-openrouter models are not policed
  - the exact verdict strings, which humans and tasks/issue-work.md read
  - EQUIVALENCE with the merged bash implementation: testdata/bash_baseline.json
    records this tree's inputs and the verdicts bash produced from them

Run:  python3 scripts/node_verify/test_checks.py     (from the repo root)
      ./scripts/verify-config.sh                     (runs it with the rest)
PyYAML + stdlib only.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_verify import checks                                  # noqa: E402
from node_verify.report import FAIL as R_FAIL, OK, SKIP         # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def reader(files):
    """An injected `read` over a dict of path -> text."""
    return lambda path: files[path]


# ---- 1. yaml parse ---------------------------------------------------------

CLEAN = {
    "docker-compose.yml": "services:\n  a: {image: x}\ninclude:\n  - apps/a/compose.yaml\n",
    "config/litellm.yaml": "model_list: []\n",
}
r = checks.check_yaml(sorted(CLEAN), reader(CLEAN))
check("yaml: a clean tree is OK", r.status == OK, detail=str(r))
check("yaml: the OK note is the one the operator knows",
      r.note == "OK: all YAML parses (no duplicate keys or includes)", detail=str(r.note))

DUP = {"docker-compose.yml": "services:\n  a: {image: x}\n  a: {image: y}\n"}
r = checks.check_yaml(["docker-compose.yml"], reader(DUP))
check("yaml: a duplicate key FAILS", r.status == R_FAIL)
check("yaml: the failure names the file and the duplicate",
      len(r.detail) == 1 and r.detail[0].startswith("FAIL: docker-compose.yml — ")
      and "duplicate key 'a'" in r.detail[0], detail=str(r.detail))
check("yaml: the failure prints with no note line, indented by two",
      r.note is None and r.detail_indent == "  ", detail=str(r))
check("yaml: the rendered failure is the log line, indented",
      r.render() == ["  " + r.detail[0]], detail=str(r.render()))

DOUBLE_INCLUDE = {
    "docker-compose.yml":
        "include:\n  - apps/egress-broker/compose.yaml\n"
        "  - apps/egress-broker/compose.yaml\n",
}
r = checks.check_yaml(["docker-compose.yml"], reader(DOUBLE_INCLUDE))
check("yaml: a doubled include FAILS (the #93 shape)", r.status == R_FAIL)
check("yaml: the doubled-include message names the target",
      r.detail == ("FAIL: docker-compose.yml — include lists "
                   "apps/egress-broker/compose.yaml more than once",), detail=str(r.detail))

DICT_INCLUDE = {
    "docker-compose.yml":
        "include:\n  - path: apps/a/compose.yaml\n  - path: apps/a/compose.yaml\n",
}
r = checks.check_yaml(["docker-compose.yml"], reader(DICT_INCLUDE))
check("yaml: the long-form {path: …} include is compared too", r.status == R_FAIL)

# The self-check gates the section: a loader that cannot be trusted must not be
# used to pronounce on real files.
read_calls = []


def counting_read(path):
    read_calls.append(path)
    return "a: 1\n"


r = checks.check_yaml(["docker-compose.yml"], counting_read,
                      self_check=lambda: ["FAIL: yaml self-check — broken"])
check("yaml: a broken loader FAILS the section", r.status == R_FAIL)
check("yaml: a broken loader stops the section before ANY file is read",
      read_calls == [], detail=str(read_calls))
check("yaml: the self-check failure is what gets printed",
      r.detail == ("FAIL: yaml self-check — broken",), detail=str(r.detail))

r = checks.check_yaml([], counting_read)
check("yaml: the real self-check runs on every invocation and passes",
      r.status == OK)


# ---- 2. dispatch tiers vs litellm ------------------------------------------

TIERS = {"tiers": {"easy": {"model": "a-model"}, "hard": {"model": "b-model"}}}
LLM = {"model_list": [{"model_name": "a-model"}, {"model_name": "b-model"},
                      {"model_name": "unused"}]}

r = checks.check_tiers(TIERS, LLM)
check("tiers: every tier model present is OK", r.status == OK)
check("tiers: the OK line lists the tier models, sorted",
      r.passthrough == ("OK: all tier models (a-model, b-model) are in litellm.yaml",),
      detail=str(r.passthrough))
check("tiers: the OK note is unchanged",
      r.note == "OK: tier models exist in litellm.yaml", detail=str(r.note))

r = checks.check_tiers({"tiers": {"hard": {"model": "ghost"}, "easy": {"model": "a-model"}}}, LLM)
check("tiers: a model missing from litellm FAILS", r.status == R_FAIL)
check("tiers: the failure names exactly the unknown model",
      r.passthrough == ("FAIL: tier models not in litellm.yaml: ghost",),
      detail=str(r.passthrough))
check("tiers: the FAIL note points at dispatch-tiers.yaml",
      r.note == "FAIL: dispatch-tiers.yaml references models not in litellm.yaml —")

r = checks.check_tiers(None, LLM)
check("tiers: not deployed yet is a SKIP, not a pass", r.status == SKIP)
check("tiers: the SKIP says which file is missing",
      r.note == "SKIP: no config/dispatch-tiers.yaml (not yet deployed)")

r = checks.check_tiers({"tiers": {}}, LLM)
check("tiers: an empty tier table is vacuously OK", r.status == OK)

r = checks.check_tiers(TIERS, {"model_list": None})
check("tiers: an empty litellm model_list FAILS both tiers",
      r.status == R_FAIL and "a-model, b-model" in r.passthrough[0],
      detail=str(r.passthrough))


# ---- 3. model pricing ------------------------------------------------------

PRICED = {"model_list": [
    {"model_name": "glm", "litellm_params": {
        "model": "openrouter/z-ai/glm", "input_cost_per_token": 1e-7,
        "output_cost_per_token": 2e-7}},
    {"model_name": "local", "litellm_params": {"model": "ollama/llama"}},
]}
r = checks.check_pricing(PRICED)
check("pricing: a pinned openrouter model is OK", r.status == OK)
check("pricing: a non-openrouter model is not policed",
      r.status == OK and r.passthrough == (
          "OK: every openrouter/* model pins input+output cost",), detail=str(r))
check("pricing: the OK note is unchanged",
      r.note == "OK: every openrouter/* model pins its own pricing")

UNPRICED = {"model_list": [
    {"model_name": "new-model", "litellm_params": {"model": "openrouter/x/new"}},
]}
r = checks.check_pricing(UNPRICED)
check("pricing: an unpinned model FAILS", r.status == R_FAIL)
check("pricing: both missing fields are named",
      r.detail[:2] == ("  FAIL: new-model: input_cost_per_token not pinned in litellm_params",
                       "  FAIL: new-model: output_cost_per_token not pinned in litellm_params"),
      detail=str(r.detail))
check("pricing: the failure tells the operator how to fix it",
      r.detail[-1] == "  Fetch the real rates: ./scripts/model-pricing.sh fetch <slug>",
      detail=str(r.detail))

HALF = {"model_list": [
    {"model_name": "half", "litellm_params": {
        "model": "openrouter/x/half", "input_cost_per_token": 1e-7}},
]}
r = checks.check_pricing(HALF)
check("pricing: pinning only the input cost still FAILS",
      r.status == R_FAIL and len(r.detail) == 2
      and "output_cost_per_token" in r.detail[0], detail=str(r.detail))

ZERO = {"model_list": [
    {"model_name": "free", "litellm_params": {
        "model": "openrouter/x/free", "input_cost_per_token": 0,
        "output_cost_per_token": 0}},
]}
r = checks.check_pricing(ZERO)
check("pricing: an explicit zero is a pin, not a miss", r.status == OK)

r = checks.check_pricing({})
check("pricing: no model_list at all is OK", r.status == OK)


# ---- 4. equivalence with the merged bash implementation --------------------
# Same inputs, same verdicts. The fixture records this tree and what bash
# decided on it, so a refactor that quietly reclassifies a check fails here.

BASELINE = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())
INPUTS = BASELINE["inputs"]
VERDICTS = BASELINE["bash_verdicts"]


def same_verdict(name, result):
    want = VERDICTS[name]
    check(f"equiv: {name} — same status",
          result.status == want["status"], detail=f"py={result.status}")
    check(f"equiv: {name} — same note",
          result.note == want["note"], detail=f"py={result.note!r}")
    check(f"equiv: {name} — same stdout line(s)",
          list(result.passthrough) == want["passthrough"],
          detail=f"py={list(result.passthrough)}")


same_verdict("dispatch tiers vs litellm",
             checks.check_tiers(INPUTS["dispatch_tiers"],
                                {"model_list": INPUTS["litellm_model_list"]}))
same_verdict("model pricing",
             checks.check_pricing({"model_list": INPUTS["litellm_model_list"]}))

# The yaml section's claim is the file SET as much as the parse: a section that
# silently narrows what it looks at passes everything.
check("equiv: the yaml file list bash enumerated is still 18 files",
      len(INPUTS["yaml_files"]) == 18, detail=str(len(INPUTS["yaml_files"])))
check("equiv: the tier models bash resolved are still the three shipped",
      sorted(t["model"] for t in INPUTS["dispatch_tiers"]["tiers"].values()) ==
      ["deepseek-flash", "deepseek-flash", "glm-5.2", "minimax-m3"],
      detail=str(INPUTS["dispatch_tiers"]))


print()
if FAIL == 0:
    print("test_checks: PASS")
    sys.exit(0)
print(f"test_checks: FAIL ({FAIL} failures)")
sys.exit(1)
