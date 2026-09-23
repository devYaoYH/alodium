"""
runner — the edge. Everything that shells out lives here and nowhere else.

git, docker, docker compose, and the sibling scripts deploy.sh chained. The
pure modules next door never import this one; deploy.py is the wiring.

Two rules this module exists to hold:

  - Every command that the bash ran BARE (so `set -e` would abort the deploy)
    goes through `check()`, which raises StepFailed carrying the exit code.
    Every command the bash ran with `|| record_msg WARN` returns its code to
    the caller instead. Which is which is behavior, not style: it decides
    whether a deploy stops or limps, and deploy-watch.sh turns "stopped" into
    a `blocked` issue in the operator's notebook.
  - Read-only queries never raise. `ps --services` returning nothing because
    the daemon is mid-restart is the same empty list the bash's `$(...)` would
    have produced, and the deploy continues into `up -d` where the real error
    surfaces with docker's own message.
"""

import json
import os
import subprocess
from pathlib import Path


class StepFailed(Exception):
    """A step the bash ran bare under `set -e`.

    Carries the exit code because deploy.sh exits with the FAILING command's
    status, not a flat 1, and deploy-watch.sh reports that number in the
    `blocked` issue it files. Flattening it to 1 would lose the only signal
    distinguishing a compose failure from a killed build.
    """

    def __init__(self, command: str, code: int):
        super().__init__(f"step failed (exit {code}): {command}")
        self.command = command
        self.code = code


def render(argv) -> str:
    """The command as the ERR trap's ${BASH_COMMAND} would have shown it."""
    return " ".join(str(part) for part in argv)


class Runner:
    """Shared subprocess plumbing. `run` is injectable so tests never fork."""

    def __init__(self, repo_root, run=subprocess.run):
        self.repo_root = Path(repo_root)
        self._run = run

    def call(self, argv, **kw):
        kw.setdefault("cwd", self.repo_root)
        return self._run(list(argv), **kw)

    def capture(self, argv, **kw):
        kw.setdefault("capture_output", True)
        kw.setdefault("text", True)
        return self.call(argv, **kw)

    def check(self, argv, **kw):
        """Run it; a non-zero exit aborts the deploy, as `set -e` did."""
        proc = self.call(argv, **kw)
        if proc.returncode != 0:
            raise StepFailed(render(argv), proc.returncode)
        return proc

    def code(self, argv, **kw) -> int:
        """Run it; hand the caller the exit code to WARN on. Never raises."""
        return self.call(argv, **kw).returncode


class Git(Runner):
    """Read-only queries plus the one write: the fast-forward-only merge."""

    def rev_parse(self, ref="HEAD", short=False) -> str:
        argv = ["git", "rev-parse"] + (["--short"] if short else []) + [ref]
        return self.capture(argv).stdout.strip()

    def fetch(self, remote, branch) -> None:
        self.check(["git", "fetch", remote, branch])

    def merge_ff_only(self, ref) -> bool:
        """False on divergence. NOT a StepFailed: the bash handles this case by
        hand with its own message, because "reconcile by hand" is an operator
        decision and deserves to say so rather than print a failing command."""
        return self.code(["git", "merge", "--ff-only", ref]) == 0

    def has_remote(self, name) -> bool:
        return self.capture(["git", "remote", "get-url", name]).returncode == 0

    def push(self, remote, branch) -> int:
        return self.code(["git", "push", remote, branch])

    def changed_files(self, old, new) -> list[str]:
        """`git diff --name-only OLD HEAD`.

        PRESERVED DEFECT (1): `old` is HEAD from BEFORE the fast-forward merge.
        An operator who runs `git pull` before deploying has already advanced
        HEAD, so old == new, this returns [], and the deploy rebuilds nothing,
        restarts nothing and reports ok. Reproduced deliberately; the fix is
        its own PR (compare against deploy-info.json's `deployed_commit`
        instead, which is the commit that was actually applied).
        """
        out = self.capture(["git", "diff", "--name-only", old, new]).stdout
        return out.splitlines()

    def diff_paths(self, old, new, paths) -> str:
        return self.capture(["git", "diff", old, new, "--", *paths]).stdout


class Compose(Runner):
    """docker compose, with the profile set carried so no call forgets it."""

    def __init__(self, repo_root, profiles="", run=subprocess.run):
        super().__init__(repo_root, run)
        self.profiles = profiles

    def _env(self, profiles=None):
        return dict(os.environ, COMPOSE_PROFILES=profiles if profiles is not None
                    else self.profiles)

    def declared_profiles(self) -> str:
        """`docker compose config --profiles` — the FALLBACK path only.

        Used when the source-text scrape found nothing. It misses profiles no
        active service declares, which is exactly why it is not the primary.
        """
        proc = self.capture(["docker", "compose", "config", "--profiles"])
        return ",".join(proc.stdout.split()) if proc.returncode == 0 else ""

    def config_services(self, profiles=None):
        """(services, stderr, rc). A non-zero rc stops the deploy at the call
        site with compose's OWN message: this is the first full parse of the
        merged tree, and #93's duplicate keys hid behind a 2>/dev/null here."""
        proc = self.capture(["docker", "compose", "config", "--services"],
                            env=self._env(profiles))
        return proc.stdout.split(), proc.stderr, proc.returncode

    def config_json(self, profiles=None) -> dict:
        """The parsed config, or {} on any failure — the bash's `2>/dev/null ||
        true` followed by an `if [[ -n ... ]]` guard. Silent because the hard
        parse gate above already ran and passed; a failure here means docker
        moved under us mid-deploy, and the passes that need this config skip
        rather than guess."""
        proc = self.capture(["docker", "compose", "config", "--format", "json"],
                            env=self._env(profiles))
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return {}

    def ps_services(self) -> list[str]:
        """Services with a RUNNING container in this project, regardless of
        which profile flags are set. This is what "deploy recreates what runs"
        means, and it is why a profile the operator has not enabled is never
        started by a deploy."""
        proc = self.capture(["docker", "compose", "ps", "--services"])
        return proc.stdout.split() if proc.returncode == 0 else []

    def ps_table(self) -> None:
        self.check(["docker", "compose", "ps", "--format",
                    "table {{.Name}}\t{{.Status}}"])

    def up(self, services=(), remove_orphans=False, profiles=None, check=True):
        argv = ["docker", "compose", "up", "-d"]
        if remove_orphans:
            argv.append("--remove-orphans")
        argv += list(services)
        env = self._env(profiles) if profiles is not None else None
        kw = {"env": env} if env else {}
        return self.check(argv, **kw) if check else self.code(argv, **kw)

    def build(self, service, profiles=None) -> int:
        """Always returns a code: every `compose build` in the deploy is a WARN
        site, because step 5 can still run the existing image and a node that
        keeps serving the old version beats a node that stops."""
        return self.code(["docker", "compose", "build", service],
                         env=self._env(profiles))

    def restart(self, service) -> None:
        self.check(["docker", "compose", "restart", service])

    def caddy_validate(self) -> bool:
        return self.code(
            ["docker", "compose", "exec", "-T", "caddy", "caddy", "validate",
             "--config", "/etc/caddy/Caddyfile"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

    def caddy_reload(self) -> None:
        """Bare: a reload that fails after validation passed means Caddy is not
        serving the merged routes and cannot say why. That aborted the bash
        deploy and it aborts this one."""
        self.check(["docker", "compose", "exec", "-T", "caddy", "caddy",
                    "reload", "--config", "/etc/caddy/Caddyfile"])


class Docker(Runner):
    """Plain `docker`, for the agent jail image that compose does not own."""

    def image_exists(self, image) -> bool:
        return self.capture(["docker", "image", "inspect", image]).returncode == 0

    def build(self, tag, context, log_path) -> int:
        with open(log_path, "wb") as log:
            return self.code(["docker", "build", "-t", tag, context],
                             stdout=log, stderr=subprocess.STDOUT)

    def tag(self, src, dst) -> int:
        return self.code(["docker", "tag", src, dst])

    def image_rm(self, image) -> int:
        return self.code(["docker", "image", "rm", image],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Scripts(Runner):
    """The sibling scripts deploy chains. Kept apart so the sequence in
    deploy.py reads as the numbered steps the bash documented."""

    def run(self, name, *args, env=None, check=True, **kw):
        argv = [f"./scripts/{name}", *args]
        if env:
            kw["env"] = dict(os.environ, **env)
        return self.check(argv, **kw) if check else self.code(argv, **kw)

    def capture_stderr(self, name):
        """(rc, stderr) — for sso-setup.sh, whose reason (an expired
        POCKET_ID_API_KEY, say) has to reach deploy-info.json rather than only
        the log nobody opens."""
        proc = self.call([f"./scripts/{name}"], stderr=subprocess.PIPE, text=True)
        return proc.returncode, proc.stderr or ""


__all__ = ["StepFailed", "render", "Runner", "Git", "Compose", "Docker", "Scripts"]
