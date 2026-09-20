"""
changes — what this merge touched, and what that implies.

Pure functions over one list of paths (`git diff --name-only OLD_HEAD HEAD`)
and one diff text. No subprocess, no docker, no filesystem. That is the point:
"which images get rebuilt", "which containers get restarted", "does SSO need
refreshing" are the decisions a deploy gets wrong, and here they are checkable
against a recorded diff in milliseconds instead of by merging something.

Every regex below is the Python spelling of a sed/grep pipeline in the bash
predecessor, and test_equivalence.py pins each one against the bash actually
executing on the same recorded input. Do not "tidy" one without re-running it:
the exclusion list, the anchors and the `(-|$)` boundary are all load-bearing,
and a widened pattern restarts containers that did not need restarting while a
narrowed one strands a merged fix in a stale image.
"""

import re

# Files under apps/<name>/ that are NOT baked into the image: compose metadata,
# the Caddy route fragment, and the env template. A merge that touches only
# these needs no rebuild and no restart — the container spec change is applied
# by `compose up` in step 5, and nothing inside the image moved.
APP_METADATA = ("compose.yaml", "route.caddy", "env.example")

# The surfaces whose change means Pocket ID's client callbacks or the proxy in
# front of them moved, so sso-setup.sh must re-derive them before first visit.
# Anchored and whole-line, exactly like the bash `grep -qE`.
_SSO_TRIGGER = re.compile(
    r"^(scripts/sso-setup\.sh|docker-compose\.yml|caddy/Caddyfile"
    r"|apps/[^/]+/(compose\.yaml|route\.caddy))$")

# `sed -n 's#^apps/\([^/]*\)/.*#\1#p'` — a path must have a slash AFTER the app
# name to name an app, so `apps/foo` (a file directly under apps/) names none.
_APP_PATH = re.compile(r"^apps/([^/]*)/.*$")

# `sed -n 's/^+  \([A-Za-z0-9][A-Za-z0-9_.-]*\):[[:space:]]*$/\1/p'` over a diff
# of the compose files: an added line at exactly two spaces of indent that is a
# bare `key:`. It is deliberately dumb — it also matches volume names, network
# names and any other two-space key — because the compose config it is filtered
# against (compose.unstarted_added_services) is what decides whether a name is
# really a service. Widening or narrowing it here changes nothing but noise.
_ADDED_KEY = re.compile(r"^\+  ([A-Za-z0-9][A-Za-z0-9_.-]*):[ \t]*$")


def changed_apps(changed) -> list[str]:
    """Every apps/<name>/ directory this merge touched, deduped and sorted."""
    return sorted({m.group(1) for m in
                   (_APP_PATH.match(path) for path in changed) if m})


def app_files(changed, app) -> list[str]:
    """The changed paths under apps/<app>/, in diff order."""
    prefix = f"apps/{app}/"
    return [path for path in changed if path.startswith(prefix)]


def app_build_inputs_changed(changed, app) -> bool:
    """True when something baked into apps/<app>'s image moved.

    The bash gate is a double grep: select this app's changed files, then
    require at least one that is NOT one of the three metadata files. An app
    whose ONLY changes are compose.yaml / route.caddy / env.example returns
    False and is skipped by both the rebuild pass and the restart pass.
    """
    excluded = {f"apps/{app}/{name}" for name in APP_METADATA}
    return any(path not in excluded for path in app_files(changed, app))


def apps_with_changed_build_inputs(changed) -> list[str]:
    """The rebuild/restart candidate set: changed apps, metadata-only removed.

    Computed once and used by three passes in deploy.py (rebuild in 4b,
    on-demand recreate in 5, mounted-config restart in 6) because the bash
    recomputes this identical gate in all three places. Having one function
    makes the three provably the same set, which reading three copies of a
    shell pipeline does not.
    """
    return [app for app in changed_apps(changed)
            if app_build_inputs_changed(changed, app)]


def agent_changed(changed) -> bool:
    """`grep -q '^agent/'` — the jail image's build context moved."""
    return any(path.startswith("agent/") for path in changed)


def sso_refresh_needed(changed) -> bool:
    """Deliberately conditional: sso-setup.sh reconfigures the live IdP, so an
    unrelated deploy must not perform external configuration work."""
    return any(_SSO_TRIGGER.match(path) for path in changed)


def litellm_restart_needed(changed) -> bool:
    """`grep -q '^config/litellm'` — note the missing trailing slash: this
    matches config/litellm.yaml as well as config/litellm/, and that width is
    the bash's, kept on purpose."""
    return any(path.startswith("config/litellm") for path in changed)


def homepage_restart_needed(changed) -> bool:
    """`grep -q '^config/homepage/'` — homepage reads custom.css/js and
    services.yaml at startup only."""
    return any(path.startswith("config/homepage/") for path in changed)


def restart_targets(changed, running) -> list[tuple[str, str]]:
    """[(service, app)] for every running service whose app's mounted files moved.

    A service belongs to an app when its name is the app name or starts with
    `<app>-` (`grep -E "^$app(-|$)"`): redash owns redash-db, redash-worker and
    the rest, while `redashing` would not be matched. Bind-mounted CONTENT
    changes do not recreate a container — compose only diffs the service spec —
    so without this pass a merged radicale `config` file never reaches the
    process that read it once at startup.
    """
    out = []
    for app in apps_with_changed_build_inputs(changed):
        boundary = re.compile(rf"^{re.escape(app)}(-|$)")
        for svc in running:
            if boundary.match(svc):
                out.append((svc, app))
    return out


def added_service_names(compose_diff: str) -> list[str]:
    """Candidate service names added by this merge, from a diff of the compose
    files. Sorted and deduped; see _ADDED_KEY for why it over-matches."""
    return sorted({m.group(1) for m in
                   (_ADDED_KEY.match(line) for line in compose_diff.splitlines()) if m})


__all__ = [
    "APP_METADATA", "changed_apps", "app_files", "app_build_inputs_changed",
    "apps_with_changed_build_inputs", "agent_changed", "sso_refresh_needed",
    "litellm_restart_needed", "homepage_restart_needed", "restart_targets",
    "added_service_names",
]
