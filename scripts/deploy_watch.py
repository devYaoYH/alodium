#!/usr/bin/env python3
"""
deploy-watch — closes the merge→deploy gap: one idempotent pass of "did a PR
merge on Forgejo? then run the normal deploy." The scheduler (launchd today,
whatever the host app uses tomorrow) provides the loop; this provides the
judgement.

    ./scripts/deploy-watch.sh            # one pass
    ./scripts/deploy-watch.sh --dry-run  # report what it would do; change nothing

scripts/deploy-watch.sh stays as the entry point because
host/deploy-watch/node.deploywatch.plist runs it every two minutes, and moving
a live launchd job's target to land a refactor is the wrong order. It is a
wrapper; this file is the pass; node_host has what it shares with the
dispatcher; node_deploy/info.py reads deploy-info.json.

The operator's merge REMAINS the authorization moment (docs/AGENT.md); this
only removes the manual `./scripts/deploy.sh` keystroke after it. It POLLS
Forgejo — no webhook listener, and no Actions runner on node-config (agents
push workflow files there in PR branches; a runner would execute them).

What keeps it safe to loop:
  - Deploys ONLY from a clean checkout parked on main. On a branch or with
    tracked edits = the operator is mid-work; skip, the next heartbeat catches
    up after they finish.
  - The trigger is the DEPLOYED hash, not the checked-out one, and nothing here
    pulls: deploy.py does its own fast-forward from forgejo/main, and its
    changed-file detection diffs the checkout's pre-deploy HEAD against that.
    Moving the checkout first would leave it nothing to diff.
  - A divergence deploys nothing and files a `blocked` coordination issue.
  - A failed deploy files ONE `blocked` issue per remote HEAD (stamp-deduped)
    so a broken deploy lands in the operator's notebook once, not every two
    minutes.

This is a behavior-preserving port of the bash; the section comments follow
it, and node_deploy/test_watch.py replays scenarios recorded from the bash.

PRESERVED DEFECT: the failure stamp is written BEFORE the `blocked` issue is
filed, and the label lookup that precedes the filing aborts the pass (exit 1)
if Forgejo does not answer — so a deploy that fails while Forgejo is down is
stamped and never reported. Same class as coordination #62; follow-up.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from node_deploy import info                                   # noqa: E402
from node_deploy.runner import Runner, StepFailed              # noqa: E402
from node_host import envfile, forgejo, text                   # noqa: E402
from node_host.host import Host                                # noqa: E402
from node_host.lock import CONTENDED, HELD, STOLEN, PassLock   # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = ".task-dispatch"
DEPLOY_INFO = "config/homepage/static/deploy-info.json"
STALE_AFTER = 7200      # the lock spans a live deploy; image pulls take minutes


def say(msg):
    print(msg, flush=True)


class MissingEnv(Exception):
    pass


def require(env, *names):
    """`set -u`: the bash aborted at the first reference to an unset name."""
    for name in names:
        if name not in env:
            raise MissingEnv(f"{name}: unbound variable (set it in .env)")
    return [env[name] for name in names]


class Watch:
    def __init__(self, repo_root, env, host, transport=forgejo.pinned_transport,
                 run=None):
        self.root = Path(repo_root)
        self.env = env
        self.host = host
        self.transport = transport
        self.git = Runner(self.root, **({"run": run} if run else {}))

    # --- git (the bash ran these bare under `set -e`, or as conditions) -----

    def git_out(self, *args) -> str:
        proc = self.git.capture(["git", *args])
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise StepFailed("git " + " ".join(args), proc.returncode)
        return proc.stdout.rstrip("\n")

    def git_ok(self, *args) -> bool:
        return self.git.capture(["git", *args]).returncode == 0

    # --- reporting ---------------------------------------------------------

    def report_blocked(self, title, body):
        """One `blocked` note in the operator's notebook. The label lookup ran
        under `set -e` (an unreadable answer ends the pass); the POST was
        `|| true`."""
        domain, repo = require(self.env, "NODE_DOMAIN", "COORDINATION_REPO")
        (token,) = require(self.env, "AGENT_FORGEJO_TOKEN")
        api = forgejo.Forgejo(domain, token, repo, self.transport)
        lid = forgejo.label_id(api.get_text("labels?limit=100"), "blocked")
        api.send("POST", "issues", {"title": title, "body": body,
                                    "labels": [int(lid)] if lid else []})

    # --- the pass ----------------------------------------------------------

    def run(self, dry: bool) -> int:
        state = self.root / STATE
        state.mkdir(parents=True, exist_ok=True)

        # Pass lock: the same mkdir lock as the dispatcher's, 2h stale-steal.
        lock = PassLock(state / "deploy-watch.lock", STALE_AFTER, self.host.now)
        verdict = lock.acquire()
        if verdict == STOLEN:
            say("[deploy-watch] stealing stale lock (>2h)")
        elif verdict == CONTENDED:
            say("[deploy-watch] stealing stale lock (>2h)")
            say("[deploy-watch] lock contended; exiting")
            return 0
        elif verdict == HELD:
            return 0    # another pass (or a live deploy) holds it — stay quiet
        with lock:
            return self._locked(dry)

    def _locked(self, dry: bool) -> int:
        # This checkout doubles as the operator's working tree; yanking it
        # forward mid-edit is how work gets lost.
        branch = self.git_out("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            say(f"[deploy-watch] checkout parked on '{branch}' — skipping")
            return 0
        if not self.git_ok("diff-index", "--quiet", "HEAD", "--"):
            say("[deploy-watch] tracked edits in working tree — skipping")
            return 0

        self.git.check(["git", "fetch", "-q", "forgejo", "main"])
        local = self.git_out("rev-parse", "HEAD")
        remote = self.git_out("rev-parse", "forgejo/main")

        # Deployed hash = the last commit deploy.py applied SUCCESSFULLY (see
        # node_deploy/info.py). Triggering on it rather than on `local`
        # decouples what is checked out from what is actually running.
        info_path = self.root / DEPLOY_INFO
        try:
            deployed = info.watcher_deployed(info_path.read_text()
                                             if info_path.is_file() else None)
        except OSError:
            deployed = ""

        if deployed == remote:
            if dry:
                say(f"[deploy-watch] deployed hash matches remote at {remote[:12]} "
                    f"— nothing to do")
            return 0    # the common every-2-minutes outcome; no log noise

        # One report per remote HEAD. Auto-retry resumes when main moves; to
        # retry sooner, fix the cause and run ./scripts/deploy.sh or rm the stamp.
        stamp = self.root / STATE / f"deploy-fail-{remote}"
        if os.path.exists(stamp):     # `[[ -e ]]` follows symlinks
            say(f"[deploy-watch] {remote[:12]} already failed and was reported — "
                f"waiting for a fix or new commits")
            return 0

        require(self.env, "NODE_DOMAIN", "COORDINATION_REPO")   # the bash's $GAPI

        # Fast-forward-only: LOCAL must be an ancestor of REMOTE, or the
        # operator's checkout has commits forgejo/main does not.
        if not self.git_ok("merge-base", "--is-ancestor", local, remote):
            say("[deploy-watch] local main and forgejo/main have DIVERGED — "
                "refusing (operator decision, not a script's)")
            if not dry:
                stamp.touch()
                self.report_blocked(
                    f"Auto-deploy blocked: main diverged at {remote[:12]}",
                    f"Local main (`{local[:12]}`) is not an ancestor of forgejo/main "
                    f"(`{remote[:12]}`), so the fast-forward-only deploy refuses to "
                    f"run. Reconcile the checkout by hand, then run "
                    f"`./scripts/deploy.sh` — auto-deploy resumes on the next merge "
                    f"after that. (Stamp `.task-dispatch/deploy-fail-{remote[:12]}…` "
                    f"suppresses repeat reports of this tip.)")
            return 1

        if dry:
            say(f"[deploy-watch] would deploy {deployed[:12]}..{remote[:12]}:")
            if deployed:
                proc = self.git.capture(["git", "log", "--oneline", f"{deployed}..{remote}"])
                for line in text.lines(proc.stdout):
                    say(f"    {line}")
                if proc.returncode != 0:
                    say("    (cannot show log; deployed hash not in current tree)")
            else:
                say("    (no successful deploy recorded — last commits on forgejo/main:)")
                for line in text.lines(self.git_out("log", "--oneline", "-10", remote)):
                    say(f"    {line}")
            return 0

        say(f"[deploy-watch] deployment needed — deploying from {deployed[:12]} "
            f"to {remote[:12]}")
        rc, out = self.host.run_combined(self.host.script("deploy.py"))
        out = text.capture(out)
        say(out)
        if rc == 0:
            say(f"[deploy-watch] deployed {remote[:12]}")
            for old in (self.root / STATE).glob("deploy-fail-*"):
                try:
                    old.unlink()        # any older failure report is moot now
                except OSError:
                    pass
            return 0
        stamp.touch()
        self.report_blocked(
            f"Auto-deploy FAILED at {remote[:12]} (exit {rc})",
            f"`scripts/deploy.sh` failed after the merge of `{remote[:12]}`. Tail:\n"
            f"\n"
            f"```\n"
            f"{text.tail_lines(out, 15)}\n"
            f"```\n"
            f"\n"
            f"Fix the cause, then run `./scripts/deploy.sh` by hand (or merge a fix "
            f"— auto-deploy retries when main moves). Full log: "
            f"`.task-dispatch/deploy-watch.log`.")
        return rc


def main(argv, repo_root=REPO_ROOT, base_env=None, host=None,
         transport=forgejo.pinned_transport, run=None) -> int:
    dry = len(argv) > 1 and argv[1] == "--dry-run"
    try:
        env = envfile.load(Path(repo_root) / ".env",
                           dict(os.environ if base_env is None else base_env))
        os.chdir(repo_root)       # `cd "$(dirname "$0")/.."`
        watch = Watch(repo_root, env, host or Host(repo_root, env), transport, run)
        return watch.run(dry)
    except StepFailed as exc:
        sys.stderr.write(f"[deploy-watch] {exc}\n")
        return exc.code
    except (OSError, envfile.EnvFileError, MissingEnv) as exc:
        sys.stderr.write(f"[deploy-watch] {exc}\n")
        return 1
    except ValueError as exc:
        # An unreadable Forgejo answer where the bash's `set -e` ended the pass.
        sys.stderr.write(f"[deploy-watch] unreadable Forgejo response: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
