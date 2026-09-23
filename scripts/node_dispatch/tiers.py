"""
tiers — an operator's `difficulty:<tier>` label -> the model and budget the
issue run gets, via config/dispatch-tiers.yaml.

The bash did this as three inline python programs whose OUTPUT STRINGS were
then taken apart with `case` and `${X%%:*}` / `${X#*:}`. Those strings are kept
as the interface here, because they are the behavior: the case branch picks
fall-back vs abort, and the split decides what `--model`/`--budget` the tenant
is launched with. test_tiers.py pins each against the recorded bash output.

PRESERVED DEFECT: the split is at the FIRST colon, so a model id containing a
colon (e.g. `vendor/model:free`) would launch as model `vendor/model` with
budget `free:<n>`. No tier uses one today; verify-config's tier check is the
place to forbid it. Fixing it here would change what an existing tier table
launches, which is not a port's call.

The flow in dispatch_run.py:

  label present and operator-added?
    resolve_label()  -> "error:…"   log, then the default tier
                     -> "unknown:…" comment, then the default tier
                     -> "m:b"       check LiteLLM:
                          live       run m/b
                          not_found  comment, run deepseek-flash / 0.50
                          anything   comment, release the claim, abort (exit 1)
  still no model?  default_tier() -> "m:b" or "" (then run-task's brief default)
"""

import json

FALLBACK_MODEL = "deepseek-flash"
FALLBACK_BUDGET = "0.50"

ERROR = "error"
UNKNOWN = "unknown"
RESOLVED = "resolved"
NONE = "none"


def load(path) -> object:
    """The tier table as the bash's TIER_JSON round-tripped it: YAML parsed,
    then through JSON (so integer keys become strings), or {'error': msg}.
    A host python without PyYAML is an error here, as it was there."""
    try:
        import yaml                                          # noqa: PLC0415
        with open(path) as f:
            data = yaml.safe_load(f)
        return json.loads(json.dumps(data))
    except Exception as exc:                                 # noqa: BLE001
        return {"error": str(exc)}


def resolve_label(data, tier_key: str) -> str:
    """The TIER_RESOLVE string; "" where the bash's python crashed."""
    try:
        if "error" in data:
            return "error:" + data["error"]
        tiers = data.get("tiers", {})
        default_tier = data.get("default", "moderate")
        if tier_key not in tiers:
            return "unknown:" + tier_key + "->default:" + default_tier
        t = tiers[tier_key]
        return t["model"] + ":" + str(t["budget_usd"])
    except Exception:                                        # noqa: BLE001
        return ""


def default_tier(data) -> str:
    """The DEFAULT_TIER string; "" for no usable default."""
    try:
        if "error" in data:
            return ""
        default = data.get("default", "moderate")
        tiers = data.get("tiers", {})
        t = tiers.get(default, {})
        if t:
            return t.get("model", "") + ":" + str(t.get("budget_usd", 0.50))
        return ""
    except Exception:                                        # noqa: BLE001
        return ""


def classify(resolved: str) -> str:
    """The `case "$TIER_RESOLVE" in error:*) unknown:*) *:*)` branch."""
    if resolved.startswith("error:"):
        return ERROR
    if resolved.startswith("unknown:"):
        return UNKNOWN
    if ":" in resolved:
        return RESOLVED
    return NONE


def split(resolved: str) -> tuple:
    """(`${X%%:*}`, `${X#*:}`) — model before the first colon, budget after."""
    model, sep, budget = resolved.partition(":")
    return model, (budget if sep else resolved)


def llm_check(models_body: str, target: str) -> str:
    """'live' | 'not_found' | 'error:<why>' from LiteLLM's /v1/models body.
    An empty body (LiteLLM unreachable) is an error, not a not_found: the
    distinction is what makes an outage abort instead of silently running a
    different model."""
    try:
        data = json.loads(models_body)
        for m in data.get("data", []):
            mid = m.get("id", "")
            # exact, or the 'openrouter/' style prefix LiteLLM may rewrite to
            if mid == target or mid.endswith("/" + target):
                return "live"
        return "not_found"
    except Exception as exc:                                 # noqa: BLE001
        return "error:" + str(exc)


def unknown_comment(label: str, tier: str) -> str:
    return (f"Difficulty label `{label}` not recognized — tier `{tier}` is not in "
            f"the dispatch table. Falling back to the default tier. Valid tiers: "
            f"`trivial`, `easy`, `moderate`, `hard`.")


def not_served_comment(tier: str, model: str) -> str:
    return (f"⚠️ Tier `{tier}` resolved to model `{model}` but that model is not "
            f"currently served by LiteLLM. Falling back to `deepseek-flash`. Check "
            f"`config/litellm.yaml` if this persists.")


def unreachable_comment(check: str, tier: str, model: str) -> str:
    return (f"⚠️ Could not reach LiteLLM to verify model availability (`{check}`). "
            f"Aborting this run — the model for tier `{tier}` (`{model}`) may be "
            f"live but cannot be confirmed. Check LiteLLM and retry by assigning "
            f"the issue again.")
