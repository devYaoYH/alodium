"""
compose — decisions read out of the compose tree, as pure functions.

Three of them, and each exists because a deploy once silently did nothing:

  - `all_profiles` enumerates EVERY declared profile from the compose source
    text. `docker compose config` filters to the active profiles, so a
    profile-gated app (on-demand snake) is invisible to a bare query — its
    rebuild was skipped and `up` then recreated it from the stale image, which
    is a recreate with no new image, which is no change at all.
  - `missing_image_builds` finds locally-built services whose image does not
    exist, so a first deploy after the PR that added an on-demand app, or a
    deploy after a prune, builds it instead of failing at launch time.
  - `unstarted_added_services` finds services this merge ADDED that are still
    not running, and groups them for one WARN per profile.

Inputs are the parsed `docker compose config --format json` dict and sets of
names. Nothing here runs docker; `runner` does that and hands the results in.
"""

import re

# `grep -o '\[.*\]'` — greedy, so the match runs from the first '[' on the line
# to the last ']'. Applied only to lines containing "profiles:", substring
# match, which is why a COMMENTED-OUT `# profiles: [x]` still contributes a
# profile name. That is the bash's behavior and it is harmless: naming a
# profile that no service declares enables nothing.
_BRACKETS = re.compile(r"\[.*\]")


def all_profiles(sources) -> str:
    """Every profile name declared anywhere in the compose text, comma-joined.

    `sources` is an iterable of file contents (docker-compose.yml, then each
    apps/*/compose.yaml). The bash pipeline this reproduces is:

        grep -h "profiles:" ... | grep -o '\\[.*\\]' | tr -d '[]' \\
          | tr ',' '\\n' | tr -d ' "' | sort -u | paste -sd, -

    Every step is reproduced including the ones that look like accidents: `tr
    -d ' "'` strips double quotes but NOT single quotes, and an empty bracket
    pair `profiles: []` contributes an empty name that sorts first and shows up
    as a leading comma. Both are preserved so the string this returns is
    byte-identical to the one the bash put in COMPOSE_PROFILES — a different
    string is a different set of visible services.

    Returns "" when nothing matched; the caller then falls back to asking
    docker, exactly as the bash does.
    """
    names = set()
    for text in sources:
        for line in text.splitlines():
            if "profiles:" not in line:
                continue
            match = _BRACKETS.search(line)
            if not match:
                continue
            inside = match.group(0).replace("[", "").replace("]", "")
            for piece in inside.split(","):
                names.add(piece.replace(" ", "").replace('"', ""))
    return ",".join(sorted(names))


def missing_image_builds(config: dict, image_exists) -> list[str]:
    """Services that build from a context whose image is not present locally.

    `image_exists(image)` is injected — `runner.Docker.image_exists` in
    production, a set membership in the tests. Services are visited in compose
    config order and the order is preserved: it is the order the builds run in,
    and a build log that matches the bash's is part of the evidence.

    A service with a build section but no `image:` key is skipped, because
    there is no name to inspect; compose would name it itself, and the bash
    declines to guess. So does this.
    """
    out = []
    for name, svc in (config.get("services") or {}).items():
        if not svc.get("build"):
            continue
        image = (svc.get("image") or "").strip()
        if not image:
            continue
        if image_exists(image):
            continue
        out.append(name)
    return out


def unstarted_added_services(config: dict, added, running) -> list[tuple[str, list[str]]]:
    """[(profile, [service, ...])] for services this merge added that are down.

    The filter, in the bash's order, and each clause is why this is a WARN and
    not a crash:

      - not a real service in the compose config -> the name came from the
        deliberately-dumb diff scrape (a volume, a network, a homepage key);
      - no `profiles` -> a default-profile service, which `compose up -d`
        already started in step 5, so silence is correct;
      - already running -> nothing to say;
      - `restart: "no"` -> a one-shot: an on-demand app or an init/migrate job,
        which is never expected to be up.

    What survives is the real gap: a profile-gated service that this merge
    introduced and that nothing started. PRESERVED DEFECT (3): the deploy only
    warns. Starting it stays the operator's call, so the whole value here is
    making the gap loud — one WARN per profile carrying the exact command —
    instead of a green deploy over a service with zero containers (redash,
    #123) or a consumer recreated pointing at a name that does not exist
    (copilot-headroom, #128).

    Profiles come out sorted, and names within a profile sorted, so the WARN
    text is stable across runs and diffable in deploy-info.json.
    """
    services = config.get("services") or {}
    running = set(running)
    by_profile: dict[str, list[str]] = {}
    for name in added:
        svc = services.get(name)
        if not svc or not svc.get("profiles") or name in running:
            continue
        if (svc.get("restart") or "no") == "no":
            continue
        by_profile.setdefault(svc["profiles"][0], []).append(name)
    return [(profile, sorted(names)) for profile, names in sorted(by_profile.items())]


def unstarted_warning(profile: str, names) -> str:
    """The exact WARN text, so the command the operator pastes is pinned by a
    test rather than by whoever last edited the string."""
    joined = " ".join(names)
    return (f"new service(s) not started: {joined} — run: "
            f"docker compose --profile {profile} up -d {joined}")


__all__ = ["all_profiles", "missing_image_builds", "unstarted_added_services",
           "unstarted_warning"]
