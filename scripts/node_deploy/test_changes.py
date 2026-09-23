#!/usr/bin/env python3
"""
Offline tests for node_deploy.changes. No git, no docker, no daemon: the input
is a list of paths, so every branch is a list literal.

Covers the gates that decide whether a merge reaches the running node at all:

  - which apps a diff names, and which paths name none
  - the metadata exclusion — compose.yaml / route.caddy / env.example alone
    must NOT rebuild or restart anything, and anything else must
  - the `^<app>(-|$)` service boundary: redash owns redash-db, `redashing`
    is a different app
  - the single-file gates: agent/, SSO surfaces, config/litellm*, config/homepage/
  - the empty diff, which is PRESERVED DEFECT (1)'s whole surface

testdata equivalence against the executed bash lives in test_equivalence.py;
this file is the branch coverage a recorded fixture cannot give, because the
node has never merged some of these shapes.

Run:  python3 scripts/node_deploy/test_changes.py   (from the repo root)
Stdlib only.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_deploy import changes                          # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


# ---- 1. which apps a diff names -------------------------------------------

check("apps: one path per app, deduped",
      changes.changed_apps(["apps/memos/Dockerfile", "apps/memos/compose.yaml",
                            "apps/snake/index.html"]) == ["memos", "snake"])
check("apps: a file directly under apps/ names no app",
      changes.changed_apps(["apps/README.md"]) == [])
check("apps: a path that is not under apps/ names no app",
      changes.changed_apps(["docker-compose.yml", "config/homepage/custom.css"]) == [])
check("apps: a bare directory entry still names its app",
      changes.changed_apps(["apps/memos/"]) == ["memos"])
check("apps: an empty diff names nothing", changes.changed_apps([]) == [])


# ---- 2. the metadata exclusion --------------------------------------------

METADATA_ONLY = ["apps/redash/compose.yaml", "apps/redash/route.caddy",
                 "apps/redash/env.example"]
check("gate: all three metadata files alone do NOT imply a rebuild",
      changes.app_build_inputs_changed(METADATA_ONLY, "redash") is False)
check("gate: one real file alongside them does",
      changes.app_build_inputs_changed(METADATA_ONLY + ["apps/redash/Dockerfile"],
                                       "redash") is True)
check("gate: a file NESTED under a metadata-looking name is a build input",
      changes.app_build_inputs_changed(["apps/copilot/headroom/Dockerfile"],
                                       "copilot") is True,
      detail="the exclusion is anchored whole-path, not a prefix")
check("gate: another app's files never satisfy this app's gate",
      changes.app_build_inputs_changed(["apps/snake/Dockerfile"], "redash") is False)
check("gate: an app with no changed files at all is False",
      changes.app_build_inputs_changed([], "redash") is False)

check("candidates: metadata-only apps are dropped, real ones kept",
      changes.apps_with_changed_build_inputs(
          METADATA_ONLY + ["apps/snake/index.html"]) == ["snake"])


# ---- 3. the service boundary ----------------------------------------------

RUNNING = ["redash-db", "redash-server", "redash", "redashing", "snake",
           "not-redash", "caddy"]
targets = changes.restart_targets(["apps/redash/config.py"], RUNNING)
names = [svc for svc, _ in targets]
check("boundary: the app's own service and its <app>-* siblings match",
      names == ["redash-db", "redash-server", "redash"], detail=str(names))
check("boundary: a service that merely STARTS with the app name does not",
      "redashing" not in names)
check("boundary: a service that CONTAINS the app name does not",
      "not-redash" not in names)
check("boundary: the target carries the app, for the log line",
      all(app == "redash" for _, app in targets))
check("boundary: a metadata-only change restarts nothing",
      changes.restart_targets(METADATA_ONLY, RUNNING) == [])
check("boundary: nothing running means nothing to restart",
      changes.restart_targets(["apps/redash/config.py"], []) == [])


# ---- 4. the single-file gates ---------------------------------------------

check("agent: any path under agent/ fires",
      changes.agent_changed(["agent/AGENTS.md"]) is True)
check("agent: a path merely containing 'agent' does not",
      changes.agent_changed(["docs/agent-notes.md", "apps/agentx/a"]) is False)

for path in ("scripts/sso-setup.sh", "docker-compose.yml", "caddy/Caddyfile",
             "apps/redash/compose.yaml", "apps/redash/route.caddy"):
    check(f"sso: {path} fires", changes.sso_refresh_needed([path]) is True)
for path in ("docker-compose.staging.yml", "caddy/Caddyfile.bak",
             "apps/redash/env.example", "apps/a/b/compose.yaml",
             "scripts/sso-setup.sh.orig"):
    check(f"sso: {path} does NOT fire — the pattern is whole-line",
          changes.sso_refresh_needed([path]) is False)

check("litellm: config/litellm.yaml fires",
      changes.litellm_restart_needed(["config/litellm.yaml"]) is True)
check("litellm: config/litellm/ fires",
      changes.litellm_restart_needed(["config/litellm/models.yaml"]) is True)
check("litellm: the missing trailing slash is the bash's — config/litellmx "
      "fires too, and that width is preserved on purpose",
      changes.litellm_restart_needed(["config/litellmx"]) is True)
check("litellm: an unrelated config file does not",
      changes.litellm_restart_needed(["config/homepage/custom.css"]) is False)

check("homepage: config/homepage/ fires",
      changes.homepage_restart_needed(["config/homepage/services.yaml"]) is True)
check("homepage: config/homepage.yaml does NOT — this one IS slash-anchored",
      changes.homepage_restart_needed(["config/homepage.yaml"]) is False)


# ---- 5. the added-service scrape ------------------------------------------

DIFF = """\
diff --git a/docker-compose.yml b/docker-compose.yml
@@
   services:
+  redash-worker:
+    image: redash
+      nested-key:
+ one-space:
+   three-spaces:
+  trailing-space:\x20
+  has.dots-and_underscores:
+  _leading_underscore:
-  removed-service:
"""
added = changes.added_service_names(DIFF)
check("added: a two-space added key is picked up",
      "redash-worker" in added)
check("added: trailing whitespace is tolerated",
      "trailing-space" in added)
check("added: dots, dashes and underscores are legal inside a name",
      "has.dots-and_underscores" in added)
check("added: a name must START with alphanumeric",
      "_leading_underscore" not in added)
check("added: other indents are ignored",
      not {"nested-key", "one-space", "three-spaces"} & set(added))
check("added: a key with a VALUE is not an added service",
      "image" not in added)
check("added: a REMOVED service is not an added one",
      "removed-service" not in added)
check("added: an empty diff adds nothing", changes.added_service_names("") == [])


# ---- 6. the empty diff: PRESERVED DEFECT (1)'s surface --------------------
#
# When the operator ran `git pull` before deploying, OLD_HEAD == HEAD and the
# diff is empty. Every gate below then answers "no work", the deploy performs
# no build and no restart, and step 7 still records status=ok. That is the
# defect, reproduced deliberately and pinned here so the follow-up PR that
# fixes it has to change this test on purpose rather than by accident.

check("nothing changed: no app is a rebuild candidate",
      changes.apps_with_changed_build_inputs([]) == [])
check("nothing changed: nothing is restarted",
      changes.restart_targets([], RUNNING) == [])
check("nothing changed: no agent build, no SSO refresh, no core restarts",
      not changes.agent_changed([]) and not changes.sso_refresh_needed([])
      and not changes.litellm_restart_needed([])
      and not changes.homepage_restart_needed([]))

print(f"\n{'FAILED' if FAIL else 'PASS'}: node_deploy.changes ({FAIL} failure(s))")
sys.exit(1 if FAIL else 0)
