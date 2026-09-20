"""
checks — the config-consistency sections, as functions over parsed data.

Each one takes what it needs already parsed (a dict, or None for "the file is
not there") and returns a Result. No filesystem except through an injected
`read`, no subprocess, no network: that is what makes both branches of every
check assertable from a dict literal in test_checks.py, instead of by
temporarily breaking the repo.

Copilot containment and build provenance are big enough to own their modules
(`containment`, `provenance`); this holds the rest.
"""

from . import strict_yaml
from .report import FAIL, OK, SKIP, Result

import yaml


# --------------------------------------------------------------------------
# YAML parse
# --------------------------------------------------------------------------

def check_yaml(paths, read, self_check=strict_yaml.self_check) -> Result:
    """Every listed YAML file parses strictly, and includes nothing twice.

    The loader's own self-check runs FIRST and short-circuits: if the thing
    doing the checking is broken, what it says about the real files is worth
    nothing. That ordering is load-bearing, not stylistic — test_checks.py
    asserts a broken loader stops the section before any file is read.
    """
    self_failures = self_check()
    if self_failures:
        return Result(FAIL, None, detail=tuple(self_failures),
                      detail_indent="  ", detail_tail=12)

    failures = []
    for path in paths:
        try:
            docs = strict_yaml.load_all(read(path), name=path)
        except yaml.YAMLError as e:
            failures.append("FAIL: %s — %s" % (path, " ".join(str(e).split())))
            continue
        for doc in docs:
            seen = set()
            for entry in (doc.get("include") or []) if isinstance(doc, dict) else []:
                target = str(entry.get("path") if isinstance(entry, dict) else entry)
                if target in seen:
                    failures.append("FAIL: %s — include lists %s more than once"
                                    % (path, target))
                seen.add(target)

    if failures:
        return Result(FAIL, None, detail=tuple(failures),
                      detail_indent="  ", detail_tail=12)
    return Result(OK, "OK: all YAML parses (no duplicate keys or includes)")


# --------------------------------------------------------------------------
# Dispatch tiers vs litellm
# --------------------------------------------------------------------------

def check_tiers(tiers_doc, litellm_doc) -> Result:
    """Every model a dispatch tier names must exist in litellm.yaml.

    A tier pointing at a model the proxy does not serve is a dispatch that
    fails at run time, on the node, with the task already accepted. `None` for
    tiers_doc means config/dispatch-tiers.yaml is not deployed yet — SKIP.
    """
    if tiers_doc is None:
        return Result(SKIP, "SKIP: no config/dispatch-tiers.yaml (not yet deployed)")

    llm_models = {m["model_name"] for m in ((litellm_doc or {}).get("model_list") or [])}
    tier_models = {t["model"] for t in ((tiers_doc.get("tiers", {})) or {}).values()}
    unknown = tier_models - llm_models

    if unknown:
        # The heredoc printed this to stdout while the section captured only
        # stderr, so the detail line landed unindented ABOVE its own verdict.
        # Preserved verbatim; see the PR's "found, not fixed" list.
        return Result(
            FAIL,
            "FAIL: dispatch-tiers.yaml references models not in litellm.yaml —",
            passthrough=("FAIL: tier models not in litellm.yaml: "
                         + ", ".join(sorted(unknown)),))
    return Result(
        OK, "OK: tier models exist in litellm.yaml",
        passthrough=("OK: all tier models (" + ", ".join(sorted(tier_models))
                     + ") are in litellm.yaml",))


# --------------------------------------------------------------------------
# Model pricing
# --------------------------------------------------------------------------

def check_pricing(litellm_doc) -> Result:
    """Every openrouter/* model pins its own input and output cost.

    LiteLLM prices by looking up litellm_params.model verbatim in its cost map,
    which carries almost no openrouter/* keys. A miss bills $0.00 silently, so
    the model never counts against litellm_settings.max_budget and the spend
    ceiling stops being a ceiling. Offline check: the pins EXIST. Whether they
    are CURRENT is scripts/model-pricing.sh check (needs network).
    """
    missing = []
    for entry in (litellm_doc or {}).get("model_list") or []:
        params = entry.get("litellm_params") or {}
        if not str(params.get("model", "")).startswith("openrouter/"):
            continue
        name = entry.get("model_name", params.get("model"))
        for cost in ("input_cost_per_token", "output_cost_per_token"):
            if params.get(cost) is None:
                missing.append(f"{name}: {cost}")

    if missing:
        detail = [f"  FAIL: {m} not pinned in litellm_params" for m in missing]
        detail.append("  Fetch the real rates: ./scripts/model-pricing.sh fetch <slug>")
        return Result(FAIL, "FAIL: unpriced model deployment —", detail=tuple(detail))
    return Result(OK, "OK: every openrouter/* model pins its own pricing",
                  passthrough=("OK: every openrouter/* model pins input+output cost",))
