#!/usr/bin/env python3
"""
Offline tests for node_deploy.compose. The inputs are a compose-config dict and
sets of names, so every branch is a dict literal and no daemon is involved.

Covers:

  - profile enumeration, including the quirks the bash pipeline has and that
    this port keeps: a commented-out `profiles:` still contributes, single
    quotes are NOT stripped where double quotes are, `profiles: []` yields an
    empty name that sorts first
  - the missing-image build list, in compose order, and its two skip branches
  - the unstarted-added-service filter: all four rejection clauses, and the
    grouping that produces one WARN per profile
  - the exact WARN text, because it carries the command the operator pastes

Run:  python3 scripts/node_deploy/test_compose.py   (from the repo root)
Stdlib only.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_deploy import compose                          # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


# ---- 1. profile enumeration ------------------------------------------------

check("profiles: the ordinary case",
      compose.all_profiles(["    profiles: [apps]\n    profiles: [on-demand]\n"])
      == "apps,on-demand")
check("profiles: deduped and sorted, not in file order",
      compose.all_profiles(["profiles: [zeta]\nprofiles: [alpha]\nprofiles: [zeta]\n"])
      == "alpha,zeta")
check("profiles: several names in one list",
      compose.all_profiles(["profiles: [apps, feeds,chat]\n"]) == "apps,chat,feeds")
check("profiles: double quotes are stripped",
      compose.all_profiles(['profiles: ["apps"]\n']) == "apps")
check("profiles: SINGLE quotes are not — the bash `tr -d ' \"'` never removed "
      "them, and a compose file that used them would produce a profile name "
      "with quotes in it, in both implementations",
      compose.all_profiles(["profiles: ['apps']\n"]) == "'apps'")
check("profiles: several sources are unioned",
      compose.all_profiles(["profiles: [a]\n", "profiles: [b]\n"]) == "a,b")
check("profiles: a line without brackets contributes nothing",
      compose.all_profiles(["profiles:\n  - apps\n"]) == "")
check("profiles: a COMMENTED line still contributes — substring match, as the "
      "bash's grep did; naming a profile no service declares enables nothing",
      compose.all_profiles(["# profiles: [ghost]\n"]) == "ghost")
check("profiles: an empty list yields an empty name that sorts first",
      compose.all_profiles(["profiles: []\nprofiles: [apps]\n"]) == ",apps")
check("profiles: nothing at all returns the empty string, which is the "
      "caller's signal to fall back to asking docker",
      compose.all_profiles(["services:\n  caddy: {}\n"]) == "")
check("profiles: the bracket match is GREEDY, so a trailing comment containing "
      "brackets is swallowed into ONE name with its spaces deleted — verified "
      "against the shipped pipeline, not reasoned about",
      compose.all_profiles(["profiles: [apps]  # and [more]\n"]) == "apps#andmore")


# ---- 2. missing local images -----------------------------------------------

CONFIG = {"services": {
    "zeta":       {"build": {"context": "./z"}, "image": "sovereign-node/zeta:local"},
    "alpha":      {"build": {"context": "./a"}, "image": "sovereign-node/alpha:local"},
    "no-build":   {"image": "postgres:16-alpine"},
    "no-image":   {"build": {"context": "./n"}},
    "blank-image": {"build": {"context": "./b"}, "image": "   "},
}}

missing = compose.missing_image_builds(CONFIG, lambda image: False)
check("images: compose ORDER is preserved, not alphabetical — it is the build "
      "order and the log has to match the bash's",
      missing == ["zeta", "alpha"], detail=str(missing))
check("images: an image-only service is skipped (compose build would error)",
      "no-build" not in missing)
check("images: a build with no image name is skipped — nothing to inspect",
      "no-image" not in missing)
check("images: a whitespace-only image name is skipped too",
      "blank-image" not in missing)
check("images: an image that exists is skipped",
      compose.missing_image_builds(
          CONFIG, {"sovereign-node/zeta:local"}.__contains__) == ["alpha"])
check("images: everything present means no build pass at all",
      compose.missing_image_builds(CONFIG, lambda image: True) == [])
check("images: an empty config is not an error",
      compose.missing_image_builds({}, lambda image: False) == [])


# ---- 3. the unstarted-added-service filter ---------------------------------

ADDED_CONFIG = {"services": {
    "redash-server": {"profiles": ["apps"], "restart": "unless-stopped"},
    "redash-worker": {"profiles": ["apps"], "restart": "unless-stopped"},
    "copilot-headroom": {"profiles": ["copilot"], "restart": "unless-stopped"},
    "core-thing": {"restart": "unless-stopped"},                 # no profiles
    "snake": {"profiles": ["on-demand"], "restart": "no"},       # one-shot
    "migrate-job": {"profiles": ["migrate"]},                    # restart absent
    "already-up": {"profiles": ["apps"], "restart": "always"},
}}
ALL_NAMES = ["redash-server", "redash-worker", "copilot-headroom", "core-thing",
             "snake", "migrate-job", "already-up", "redash_db", "not-a-service"]

result = compose.unstarted_added_services(ADDED_CONFIG, ALL_NAMES, ["already-up"])
check("unstarted: grouped by profile, profiles sorted",
      [profile for profile, _ in result] == ["apps", "copilot"], detail=str(result))
check("unstarted: names within a profile sorted",
      result[0][1] == ["redash-server", "redash-worker"], detail=str(result[0]))
check("unstarted: a name that is not a service is dropped — this is what makes "
      "the deliberately-dumb diff scrape safe",
      not any("redash_db" in names or "not-a-service" in names
              for _, names in result))
check("unstarted: a default-profile service is dropped; `up -d` started it",
      not any("core-thing" in names for _, names in result))
check("unstarted: `restart: no` is dropped — a one-shot is never expected up",
      not any("snake" in names for _, names in result))
check("unstarted: an ABSENT restart key defaults to 'no' and is dropped too",
      not any("migrate-job" in names for _, names in result))
check("unstarted: a service already running is dropped",
      not any("already-up" in names for _, names in result))
check("unstarted: nothing added means nothing to say",
      compose.unstarted_added_services(ADDED_CONFIG, [], []) == [])
check("unstarted: everything already running means nothing to say",
      compose.unstarted_added_services(ADDED_CONFIG, ALL_NAMES, ALL_NAMES) == [])
check("unstarted: a service on several profiles is filed under the FIRST",
      compose.unstarted_added_services(
          {"services": {"s": {"profiles": ["b", "a"], "restart": "always"}}},
          ["s"], []) == [("b", ["s"])])


# ---- 4. the WARN text ------------------------------------------------------

check("warn: carries a runnable command with every name",
      compose.unstarted_warning("apps", ["redash-server", "redash-worker"])
      == "new service(s) not started: redash-server redash-worker — run: "
         "docker compose --profile apps up -d redash-server redash-worker")

print(f"\n{'FAILED' if FAIL else 'PASS'}: node_deploy.compose ({FAIL} failure(s))")
sys.exit(1 if FAIL else 0)
