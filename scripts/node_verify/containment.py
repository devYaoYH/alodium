"""
containment — the copilot's code/data-plane split, asserted as config.

The copilot seat is a capable, subscription-backed Claude Code session. Its
safety rests on a hard code/data-plane split: it may reach ONLY its door
(front), the agent spur (agents) and its egress proxy (copilot-egress), and
its ONLY internet path is that proxy. A config change that puts it on a
data-plane network, on `edge` directly, or hands it the docker socket / a
secrets mount would silently dismantle that boundary — this catches it before
it can be proposed, let alone merged.

Pure: one parsed compose fragment in, a list of violation strings out. Every
rule below is one assertion in test_containment.py, with a fixture that
violates it — because a containment check nobody has seen reject anything is
indistinguishable from one that checks nothing.
"""

import re

from .report import FAIL, OK, SKIP, Result

# The load-bearing boundary: reach front (door), agents (spur) and its egress
# proxy — nothing else. Any other network, above all a data-plane net or `edge`
# (direct internet), breaks the separation the whole design rests on.
ALLOWED = {"front", "agents", "copilot-egress"}
# Only the egress companion may bridge to the internet, and only paired with
# its private link to the copilot — exactly the search-egress shape.
EGRESS_ALLOWED = {"copilot-egress", "edge"}
# The headroom token-optimizer: a third-party proxy on the copilot's already
# allowed traffic. copilot-egress is its one and only network.
HEADROOM_ALLOWED = {"copilot-egress"}
ANTHROPIC_DOMAINS = ("anthropic.com", "claude.com")


def networks(services: dict, name: str) -> set:
    """The set of networks a service joins, from either compose spelling."""
    n = services.get(name, {}).get("networks", []) or []
    return set(n.keys()) if isinstance(n, dict) else set(n)


def violations(compose_doc: dict) -> list[str]:
    """Every containment rule the fragment breaks, in reporting order."""
    svcs = (compose_doc or {}).get("services", {}) or {}
    errs = []

    def nets(name):
        return networks(svcs, name)

    extra = nets("copilot") - ALLOWED
    if extra:
        errs.append(f"copilot joins forbidden network(s) {sorted(extra)} "
                    f"(allowed: {sorted(ALLOWED)}) — data-plane reach / direct "
                    f"internet must never be granted to the copilot.")
    if "edge" in nets("copilot"):
        errs.append("copilot must NOT join `edge` — its only egress is via copilot-egress.")

    e_extra = nets("copilot-egress") - EGRESS_ALLOWED
    if e_extra:
        errs.append(f"copilot-egress joins unexpected network(s) {sorted(e_extra)} "
                    f"(allowed: {sorted(EGRESS_ALLOWED)}).")

    # copilot-headroom is what the copilot points ANTHROPIC_BASE_URL at. It
    # must NEVER join `edge` (that would hand a third-party proxy a general
    # internet path around the Anthropic-only allowlist), and never `front`
    # (the door), `agents` (the spur) or any data-plane net.
    h_extra = nets("copilot-headroom") - HEADROOM_ALLOWED
    if h_extra:
        errs.append(f"copilot-headroom joins forbidden network(s) {sorted(h_extra)} "
                    f"(allowed: {sorted(HEADROOM_ALLOWED)}) — it must reach Anthropic "
                    f"only via copilot-egress, never `edge`/`front`/`agents`/data-plane.")
    if "edge" in nets("copilot-headroom"):
        errs.append("copilot-headroom must NOT join `edge` — its only egress is via "
                    "copilot-egress (the Anthropic-only allowlist). A direct `edge` "
                    "route would give a third-party proxy general internet.")
    # No host socket — same posture as the seat itself.
    for v in svcs.get("copilot-headroom", {}).get("volumes", []) or []:
        src = (v.split(":", 1)[0] if isinstance(v, str) else v.get("source", "")).strip()
        if "docker.sock" in src:
            errs.append("copilot-headroom mounts the docker socket — forbidden.")

    # No host socket, no secrets mount — node maintenance only, no host control.
    for v in svcs.get("copilot", {}).get("volumes", []) or []:
        src = (v.split(":", 1)[0] if isinstance(v, str) else v.get("source", "")).strip()
        if "docker.sock" in src:
            errs.append("copilot mounts the docker socket — forbidden (no host control).")
        if src == "secrets" or src.startswith("./secrets") or src.startswith("/"):
            if "COPILOT.md" not in (v if isinstance(v, str) else ""):
                errs.append(f"copilot mounts host path {src!r} — only ./COPILOT.md "
                            f"(ro) is allowed.")

    errs += egress_allowlist_violations(svcs)
    return errs


def egress_allowlist_violations(svcs: dict) -> list[str]:
    """The egress allowlist default must stay Anthropic-owned.

    A widened default here would quietly turn the one controlled hole into
    general internet access. The value is a `${VAR:-<default>}` string — pull
    out the default and require EVERY entry to be an Anthropic domain
    (anthropic.com = API, claude.com = auth plane).
    """
    errs = []
    egress_env = svcs.get("copilot-egress", {}).get("environment", {}) or {}
    raw = str(egress_env.get("EGRESS_ALLOW", ""))
    m = re.search(r":-([^}]*)\}", raw)          # ${VAR:-<default>} -> <default>
    default = m.group(1) if m else raw
    entries = [e.strip().lstrip(".").lower() for e in default.split(",") if e.strip()]
    if not entries:
        errs.append("copilot-egress EGRESS_ALLOW has no default allowlist.")
    for e in entries:
        if not any(e == d or e.endswith("." + d) for d in ANTHROPIC_DOMAINS):
            errs.append(f"copilot-egress EGRESS_ALLOW default entry {e!r} is not an "
                        f"Anthropic-owned host ({'/'.join(ANTHROPIC_DOMAINS)}) — the "
                        f"egress must stay Anthropic-only.")
    return errs


def check_copilot(compose_doc) -> Result:
    """None for compose_doc means apps/copilot/compose.yaml is absent — SKIP."""
    if compose_doc is None:
        return Result(SKIP, "SKIP: no apps/copilot/compose.yaml")
    errs = violations(compose_doc)
    if errs:
        return Result(FAIL, "FAIL: copilot containment violated —",
                      detail=tuple("  - " + e for e in errs))
    return Result(OK, "OK: copilot reaches only front/agents/egress; no socket, "
                      "secrets, or data-plane net")
