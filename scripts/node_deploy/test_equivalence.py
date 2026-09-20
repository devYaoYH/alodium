#!/usr/bin/env python3
"""
EQUIVALENCE with the bash deploy this package replaces. The load-bearing test.

A live A/B is not available and must not be manufactured: proving the port by
running the real deploy twice against production is precisely the thing a
deploy port must never do. So the decisions were extracted instead.

  testdata/node_state.json    the real node's inputs, recorded 2026-09-20 with
                              read-only queries at 0d45b51: the compose graph,
                              which services are running, which local images
                              exist, and six real `git diff --name-only`
                              outputs from merges that actually happened.
  testdata/bash_baseline.json what scripts/deploy.sh DECIDES from those inputs,
                              produced by EXECUTING its own grep/sed/python
                              pipelines — not by reading the script and writing
                              down what it looks like it does.

This file asserts the Python reproduces all of it. If one of these fails, the
port changed behavior, and on this node behavior means: which containers get
restarted on the next merge, with nobody watching.

The six recorded merges were chosen to cover the shapes that have bitten:

  redash123    #123 — a whole new profile-gated app (7 files, 3 under apps/)
  headroom128  #128 — a new sidecar inside an already-enabled profile
  agent125     #125 — an agent/-only change, no apps/ at all
  redash132    metadata-only: compose.yaml + route.caddy, must NOT rebuild
  backup141    26 files, none of which imply any container work
  noop         an empty diff — the "nothing changed" branch

`running_pre` is the recorded running set with redash and copilot-headroom
removed: the state the node was in the moment those two merges landed, which
is the only way the recorded fixture can exercise the branch that made them
bugs. Today they are up, so today's state answers "nothing to warn about".

Run:  python3 scripts/node_deploy/test_equivalence.py   (from the repo root)
      ./scripts/verify-config.sh                        (runs it with the rest)
Stdlib only.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_deploy import changes, compose, info            # noqa: E402

STATE = json.loads((HERE / "testdata" / "node_state.json").read_text())
BASH = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())["decisions"]

CONFIG = {"services": STATE["compose_services"]}
BUILDABLE = set(STATE["buildable"])
CASES = sorted(STATE["changed"])

FAIL = 0
CHECKED = 0


def check(name, got, want):
    global FAIL, CHECKED
    CHECKED += 1
    if got == want:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}\n        bash: {want!r}\n        py:   {got!r}")
        FAIL += 1


def joined(items):
    """The baseline records list answers as the bash's space-joined strings."""
    return " ".join(items)


print("equivalence: profile enumeration")
# The COMPOSE_PROFILES string, byte for byte. A different string is a different
# set of visible services, which is a different rebuild set.
check("all_profiles", compose.all_profiles([STATE["profile_lines"]]),
      BASH["all_profiles"])

print("equivalence: per-merge decisions")
for case in CASES:
    changed = STATE["changed"][case]
    running = STATE["running"]

    check(f"apps.{case}", joined(changes.changed_apps(changed)), BASH[f"apps.{case}"])

    gated = changes.apps_with_changed_build_inputs(changed)
    check(f"rebuild.{case}", joined([a for a in gated if a in BUILDABLE]),
          BASH[f"rebuild.{case}"])
    check(f"notbuildable.{case}", joined([a for a in gated if a not in BUILDABLE]),
          BASH[f"notbuildable.{case}"])

    check(f"restarts.{case}",
          joined([svc for svc, _ in changes.restart_targets(changed, running)]),
          BASH[f"restarts.{case}"])

    check(f"agent.{case}", "1" if changes.agent_changed(changed) else "0",
          BASH[f"agent.{case}"])
    check(f"sso.{case}", "1" if changes.sso_refresh_needed(changed) else "0",
          BASH[f"sso.{case}"])
    check(f"litellm.{case}", "1" if changes.litellm_restart_needed(changed) else "0",
          BASH[f"litellm.{case}"])
    check(f"homepage.{case}", "1" if changes.homepage_restart_needed(changed) else "0",
          BASH[f"homepage.{case}"])

    # The scrape itself, run on the raw diff text the bash's sed was run on.
    # `added.<case>` is what the shipped sed produced; the Python regex must
    # reach the same list from the same bytes — including the false positives
    # (`dash`, `front`, `redash_db` in #123), which the compose-config filter
    # below is what actually discards.
    check(f"added.{case}",
          joined(changes.added_service_names(STATE["compose_diffs"][case])),
          BASH[f"added.{case}"])

    unstarted = compose.unstarted_added_services(CONFIG, STATE["added"][case], running)
    check(f"unstarted.{case}",
          "".join(f"{p}={' '.join(n)};" for p, n in unstarted),
          BASH[f"unstarted.{case}"])

print("equivalence: the #123 / #128 branch, against the pre-merge running set")
for case in ("redash123", "headroom128"):
    pre = STATE["running_pre"]
    unstarted = compose.unstarted_added_services(CONFIG, STATE["added"][case], pre)
    check(f"unstarted_pre.{case}",
          "".join(f"{p}={' '.join(n)};" for p, n in unstarted),
          BASH[f"unstarted_pre.{case}"])
    check(f"restarts_pre.{case}",
          joined([svc for svc, _ in
                  changes.restart_targets(STATE["changed"][case], pre)]),
          BASH[f"restarts_pre.{case}"])

print("equivalence: missing local images (step 4c)")
for variant in ("existing_images", "existing_images_pruned"):
    existing = set(STATE[variant])
    check(f"missing_images.{variant}",
          joined(compose.missing_image_builds(CONFIG, existing.__contains__)),
          BASH[f"missing_images.{variant}"])

print("equivalence: deploy-info.json, byte for byte")
CASES_INFO = STATE["deploy_info_cases"]
MESSAGES = [tuple(pair) for pair in CASES_INFO["messages_two"]]
COMMIT = "1111111111111111111111111111111111111111"
COMMON = dict(timestamp="2026-09-20T15:09:35Z", commit=COMMIT, short_hash="1111111",
              url=info.commit_url("localhost", "operator/node-config", COMMIT))

for name, status, messages, previous in (
        ("ok", info.OK, [], CASES_INFO["prev_ok"]),
        ("warning", info.WARNING, MESSAGES, CASES_INFO["prev_ok"]),
        ("failed", info.FAILED, [], CASES_INFO["prev_ok"]),
        # An unreadable or absent previous file must NOT advance deployed_commit
        # to this failed run's tip; it yields "", which the watcher retries.
        ("failed_noprev", info.FAILED, [], None),
        # Files written before `deployed_commit` existed: count `commit`, but
        # only if that run had not itself failed.
        ("failed_legacy_ok", info.FAILED, [], CASES_INFO["prev_legacy_ok"]),
        ("failed_legacy_failed", info.FAILED, [], CASES_INFO["prev_legacy_failed"])):
    rendered = info.render(info.build(status=status, messages=messages,
                                      previous=previous, **COMMON))
    check(f"info.{name}", rendered.rstrip("\n"), BASH[f"info.{name}"])

print(f"\n{CHECKED} decisions compared against the executed bash; {FAIL} failed")
if FAIL:
    print("EQUIVALENCE NOT PROVEN — do not merge this port")
sys.exit(1 if FAIL else 0)
