#!/usr/bin/env python3
"""
The deterministic deploy step — deliberately host-side, deliberately dumb.
The agent proposes (PR), you merge on Forgejo, THIS applies to the running
node. Operator-triggered by design: merge = authorization, this = apply.

    ./scripts/deploy.sh

Note the wrapper: scripts/deploy.sh still exists and still works, because
host/deploy-watch/node.deploywatch.plist runs it on a 2-minute heartbeat and
the docs teach that path. Nothing invokes this file directly.

Why it pulls FORGEJO, not origin: agent PRs merge on the Forgejo node-config
repo (the tree the jail clones). GitHub `origin` is the public template and
lags until we mirror to it. The old `git pull` pulled origin and so deployed
NOTHING after an agent PR merged — the merge was never on the branch it pulled.

The decisions live in scripts/node_deploy/{changes,compose,info}.py as pure
functions with offline tests; this file is the wiring between them and
scripts/node_deploy/runner.py, which is the only place that shells out. The
step numbers below are the bash predecessor's, kept so a reviewer can read the
two side by side — this is a behavior-preserving port and that correspondence
is the evidence.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from node_deploy import changes, compose as compose_mod, info      # noqa: E402
from node_deploy.runner import (Compose, Docker, Git,              # noqa: E402
                                Scripts, StepFailed)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The homepage lower-left "deployed" stamp reads this file. We record not just
# the deployed commit but the OUTCOME, so a broken deploy shows a clickable
# badge on the dashboard instead of failing silently. See node_deploy/info.py
# for why `commit` and `deployed_commit` are two fields.
DEPLOY_INFO = Path("config/homepage/static/deploy-info.json")

AGENT_CANDIDATE = "sovereign-node/agent:candidate"
AGENT_LOCAL = "sovereign-node/agent:local"


def out(msg=""):
    print(msg, flush=True)


def last_nonblank(text: str) -> str:
    """`grep -v '^[[:space:]]*$' | tail -1` — the line worth putting in the
    badge, out of a script's whole stderr."""
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


class Deploy:
    """One deploy run: the message log, the artifact, and the ordered steps."""

    def __init__(self, repo_root=REPO_ROOT, run=None):
        kw = {"run": run} if run else {}
        self.repo_root = Path(repo_root)
        self.messages: list[tuple[str, str]] = []
        # Set by build_pass and read by apply_pass. Declared here so the two
        # halves are obviously one run's state and not a surprise attribute.
        self.profiles = ""
        self.buildable: set[str] = set()
        self.config: dict = {}
        self.rebuilt_apps: list[str] = []
        self.git = Git(repo_root, **kw)
        self.docker = Docker(repo_root, **kw)
        self.scripts = Scripts(repo_root, **kw)
        self.compose = Compose(repo_root, **kw)

    # --- bookkeeping -------------------------------------------------------

    def record(self, level, text):
        """Echo to the console AND stash for deploy-info.

        Every WARN site in the bash is a `|| record_msg WARN` and every one of
        them is a place the deploy CONTINUES. That is the design: a node that
        keeps serving the previous version of one app beats a node that stops
        halfway through applying a merge. The badge is how the operator finds
        out anyway.
        """
        print(f"deploy: {level} {text}", file=sys.stderr, flush=True)
        self.messages.append((level, text))

    def write_info(self, status):
        """(Re)write the JSON artifact the homepage and the watcher read."""
        path = self.repo_root / DEPLOY_INFO
        path.parent.mkdir(parents=True, exist_ok=True)
        commit = self.git.rev_parse("HEAD")
        previous = None
        try:
            previous = json.loads(path.read_text())
        except (OSError, ValueError):
            previous = None
        payload = info.build(
            status=status,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            commit=commit,
            short_hash=self.git.rev_parse("HEAD", short=True),
            url=info.commit_url(os.environ.get("NODE_DOMAIN", ""),
                                os.environ.get("NODE_CONFIG_REPO", ""), commit),
            messages=self.messages,
            previous=previous)
        # Written beside the target and renamed: the homepage serves this file
        # continuously, and a half-written one is a broken badge.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(info.render(payload))
        tmp.replace(path)

    def fail(self, text) -> int:
        """A hard abort the deploy diagnoses itself, with its own message."""
        self.record("ERROR", text)
        try:
            self.write_info(info.FAILED)
        except Exception:                                        # noqa: BLE001
            pass
        return 1

    # --- the steps ---------------------------------------------------------

    def run(self) -> int:
        # 1. Bring the merged tree into the working checkout FROM FORGEJO
        #    (where the PR merged), fast-forward only — a divergence is an
        #    operator decision, not a silent merge commit from a deploy script.
        #    Remember where we started: OLD_HEAD..HEAD drives every gate below.
        old_head = self.git.rev_parse("HEAD")
        self.git.fetch("forgejo", "main")
        if not self.git.merge_ff_only("forgejo/main"):
            return self.fail("local main and forgejo/main have diverged — "
                             "reconcile by hand, then re-run.")

        # 2. Mirror the now-merged main back to GitHub origin, IF an `origin`
        #    remote is configured. The public GitHub repo is frozen for now, so
        #    `origin` has been removed from local tracking and this no-ops
        #    quietly rather than WARN-ing on every deploy.
        if self.git.has_remote("origin"):
            if self.git.push("origin", "main") != 0:
                self.record("WARN", "could not push origin (continuing; node is "
                                    "already at merged main)")
        else:
            out("   skipping origin mirror (no 'origin' remote configured)")

        # 3. Refresh derived secrets before compose reads .env.
        self.scripts.run("derive-secrets.sh")
        # 3b. Mint per-app credentials the merged tree now expects: a missing
        #     env_file aborts the WHOLE compose up before any container starts.
        self.scripts.run("mint-secrets.sh")
        # 3c. difficulty:* labels for issue-work dispatch. Idempotent.
        if self.scripts.run("ensure-tier-labels.sh", check=False) != 0:
            self.record("WARN", "ensure-tier-labels.sh failed (non-fatal; labels "
                                "may need manual creation)")
        # 4. Build any mirrored images that are missing, before compose up so
        #    the image reference in the compose fragment resolves.
        self.scripts.run("build-mirrored.sh")
        # 4a-bis. Stage browser-side vendor deps from the manifests' pins. Must
        #     run BEFORE any image build: an app whose vendor dir is empty fails
        #     its build on purpose rather than shipping a half-working page.
        if self.scripts.run("fetch-vendor.sh", check=False) != 0:
            self.record("WARN", "vendor staging failed — see above")

        changed = self.git.changed_files(old_head, "HEAD")
        rc = self.build_pass(changed)
        if rc != 0:
            return rc
        return self.apply_pass(changed, old_head)

    # --- 4b/4c/4d: the build passes ---------------------------------------

    def build_pass(self, changed) -> int:
        # Enumerate services across ALL declared profiles, not just the enabled
        # ones. `docker compose config --services` filters to active profiles,
        # so a profile-gated app is INVISIBLE to the gate below — its rebuild
        # gets skipped and step 5 then recreates it from the STALE image, which
        # is exactly what stranded snake's fixes. This only affects the BUILD
        # pass: naming a service builds its image and starts nothing, so the
        # operator still owns which profiles actually run.
        self.profiles = compose_mod.all_profiles(self.compose_sources())
        if not self.profiles:
            # Fallback: ask docker, which may miss on-demand services. (The
            # bash also had an `|| echo "on-demand"` here; it was unreachable,
            # since the pipeline's status came from `paste`, and it stays
            # unreachable rather than becoming a new behavior in this port.)
            self.profiles = self.compose.declared_profiles()
        self.compose.profiles = self.profiles
        out(f"   rebuilding with profiles: {self.profiles}")

        # This is the first full parse of the merged compose tree, so a broken
        # file stops the deploy HERE — with compose's own error in the log and
        # in deploy-info, not a bare "step failed" (#93's duplicate keys hid
        # behind a 2>/dev/null at exactly this line).
        buildable, stderr_text, rc = self.compose.config_services()
        if rc != 0:
            sys.stderr.write(stderr_text)
            return self.fail("docker compose config failed; no containers were "
                             f"changed: {last_nonblank(stderr_text)}")
        self.buildable = set(buildable)

        # 4b. Rebuild locally-built images whose build inputs changed in this
        #     merge. `compose up -d` does NOT rebuild an existing image, so a
        #     merged Dockerfile change would otherwise never reach the running
        #     container. Diff-driven so deploys stay fast.
        self.rebuilt_apps = []
        for app in changes.apps_with_changed_build_inputs(changed):
            if app not in self.buildable:
                out(f"   skipping {app} rebuild (not found in BUILDABLE services)")
                continue
            self.rebuilt_apps.append(app)
            out(f"   rebuilding {app} image (build inputs changed)")
            if self.compose.build(app) != 0:
                self.record("WARN", f"build failed for {app} (continuing; step 5 "
                                    f"uses existing image)")

        # 4c. Build locally-built images that do not exist yet. Catches a new
        #     on-demand app whose image was never built, and recovers from a
        #     pruned image. `compose build` is a no-op when it already exists.
        self.config = self.compose.config_json()
        if self.config:
            for app in compose_mod.missing_image_builds(self.config,
                                                        self.docker.image_exists):
                out(f"   building {app} (missing local image — step 4c)")
                if self.compose.build(app) != 0:
                    self.record("WARN", f"build failed for {app} (continuing; "
                                        f"launcher will build on demand)")

        # 4d. Rebuild the agent jail image when agent/ changed.
        if changes.agent_changed(changed):
            self.build_agent_image()
        return 0

    def build_agent_image(self):
        """Build a candidate, gate it on the jail smoke test, then move :local.

        Tenants run from sovereign-node/agent:local (scripts/run-task.sh and the
        agent/assistant compose services), but nothing above rebuilds it — 4b
        covers only apps/* and 4c only builds a MISSING image — so merged jail
        fixes never reached dispatched runs and the image once sat two months
        stale.

        PRESERVED DEFECT (2): this is a bare `docker build`, not
        `docker compose build agent`, so the compose service's own build args
        and context settings are bypassed; and a failed build or smoke test
        keeps the CURRENT :local behind a WARN, which downgrades the deploy to
        "warning" but leaves dispatched runs on the stale jail. Follow-up PR.
        """
        handle, name = tempfile.mkstemp(prefix="deploy-agent-build.")
        os.close(handle)                    # Docker.build reopens it by path
        log_path = Path(name)
        out(f"   rebuilding agent jail image (agent/ changed) -> {AGENT_CANDIDATE}")
        try:
            built = self.docker.build(AGENT_CANDIDATE, "./agent", log_path) == 0
            smoked = built and self.scripts.run(
                "test-jail-image.sh", "--no-build",
                env={"JAIL_IMAGE": AGENT_CANDIDATE}, check=False) == 0
            if smoked:
                self.docker.tag(AGENT_CANDIDATE, AGENT_LOCAL)
                out(f"   agent jail image passed its smoke test -> {AGENT_LOCAL}")
            else:
                tail = log_path.read_text(errors="replace").splitlines()[-20:]
                sys.stderr.write("\n".join(tail) + "\n")
                self.record("WARN", "agent jail image build or smoke test failed "
                                    f"— kept the existing {AGENT_LOCAL} (see "
                                    f"deploy log)")
        finally:
            # Drops the candidate tag; a promoted image lives on as :local.
            self.docker.image_rm(AGENT_CANDIDATE)
            log_path.unlink(missing_ok=True)

    # --- 5/5b/6/7: apply ---------------------------------------------------

    def apply_pass(self, changed, old_head) -> int:
        # 5. Recreate any service whose spec changed. Two passes, because
        #    "which profiles are enabled" is the OPERATOR's call: first the core
        #    plane (default profile), then every profile-gated service that is
        #    CURRENTLY RUNNING. Deploy recreates what runs; it never starts a
        #    profile the operator has not enabled.
        self.compose.up(remove_orphans=True)
        running = self.compose.ps_services()
        if running:
            self.compose.up(running)
        # On-demand apps (restart: "no") are not in `running`, so the pass above
        # skips them even when their image just changed. Recreate the ones 4b
        # rebuilt so the container spec is refreshed.
        if self.rebuilt_apps:
            out("   recreating rebuilt on-demand apps: "
                + " ".join(self.rebuilt_apps))
            self.compose.up(self.rebuilt_apps, profiles=self.profiles, check=False)

        # 5b. Flag services this merge ADDED that are still not running.
        added = changes.added_service_names(
            self.git.diff_paths(old_head, "HEAD",
                                ["docker-compose.yml", "apps/*/compose.yaml"]))
        if added and self.config:
            running_now = self.compose.ps_services()
            for profile, names in compose_mod.unstarted_added_services(
                    self.config, added, running_now):
                self.record("WARN", compose_mod.unstarted_warning(profile, names))

        # SSO has one host-side source of truth: Pocket ID's client callbacks
        # and the local-dev override are derived by sso-setup.sh. A merged
        # browser surface or door/proxy change must refresh that before its
        # first visit. Deliberately conditional: the setup touches the IdP, so
        # unrelated deploys do not perform external configuration work.
        if changes.sso_refresh_needed(changed):
            out("   refreshing SSO wiring (callback or proxy configuration changed)")
            rc, stderr_text = self.scripts.capture_stderr("sso-setup.sh")
            sys.stderr.write(stderr_text)
            if rc != 0:
                return self.fail(f"sso-setup.sh failed: {last_nonblank(stderr_text)}")

        # 6. Bind-mounted CONTENT changes do not recreate containers — compose
        #    only diffs the service spec. Caddy gets a validated reload (routes
        #    are its config); any app whose mounted files changed gets a restart
        #    so the process re-reads what the merge changed.
        if self.compose.caddy_validate():
            self.compose.caddy_reload()
            out("   caddy reloaded")
        else:
            self.record("WARN", "caddy config failed validation — NOT reloading "
                                "(fix the route, re-run)")

        for svc, app in changes.restart_targets(changed, running):
            out(f"   restarting {svc} (mounted config changed in apps/{app}/)")
            self.compose.restart(svc)
        # Same class, core plane: these read their config only at startup.
        if changes.litellm_restart_needed(changed):
            out("   restarting litellm (config/litellm* changed)")
            self.compose.restart("litellm")
        if changes.homepage_restart_needed(changed):
            out("   restarting homepage (config/homepage/* changed)")
            self.compose.restart("homepage")

        self.compose.ps_table()

        # 7. Record deployment info for the homepage widget. Any WARN collected
        #    along the way downgrades the status so the dashboard shows the
        #    badge; a clean run is "ok". Hard aborts wrote "failed" already.
        status = info.final_status(self.messages)
        self.write_info(status)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out(f"   deploy-info recorded (status={status}, "
            f"{self.git.rev_parse('HEAD', short=True)} at {stamp})")
        return 0

    # --- helpers -----------------------------------------------------------

    def compose_sources(self):
        """The compose source TEXT, for the profile scrape. Root file first,
        then apps/*/compose.yaml in glob order, matching the bash's argument
        order to `grep -h`; an unreadable file is skipped, as `2>/dev/null`
        did."""
        paths = [self.repo_root / "docker-compose.yml"]
        paths += sorted(self.repo_root.glob("apps/*/compose.yaml"))
        for path in paths:
            try:
                yield path.read_text()
            except OSError:
                continue


def main(argv) -> int:
    os.chdir(REPO_ROOT)
    deploy = Deploy()
    try:
        return deploy.run()
    except StepFailed as exc:
        # The ERR trap's job: flag the deploy failed and record the failing
        # command before the process exits. deploy.sh exited with the FAILING
        # command's status and deploy-watch.sh reports that number, so it is
        # carried through rather than flattened to 1.
        deploy.record("ERROR", str(exc))
        try:
            deploy.write_info(info.FAILED)
        except Exception:                                        # noqa: BLE001
            pass
        return exc.code


if __name__ == "__main__":
    try:
        code = main(sys.argv)
    except KeyboardInterrupt:
        sys.stderr.write("deploy: interrupted\n")
        sys.exit(130)
    except BaseException:                                        # noqa: BLE001
        # Anything unforeseen is the ERR-trap path too: say so in the artifact
        # rather than leaving a stale "ok" badge over a node that is half
        # through applying a merge.
        import traceback
        traceback.print_exc()
        try:
            broken = Deploy()
            broken.record("ERROR", "deploy aborted with an unexpected error "
                                   "(traceback above)")
            broken.write_info(info.FAILED)
        except Exception:                                        # noqa: BLE001
            pass
        sys.exit(1)
    sys.exit(code)
