#!/usr/bin/env python3
"""
The bridge between "an agent wants a task run" and "no agent may touch
docker". One pass; the scheduler provides the loop.

    ./scripts/task-dispatcher.sh            # one pass
    ./scripts/task-dispatcher.sh --dry-run  # validate + report, run nothing

scripts/task-dispatcher.sh stays as the entry point: host/dispatch/
node.dispatch.plist runs it on every doorbell and every 10 minutes, and up.sh,
the docs and the request-task skill name it. It is a wrapper; this is the pass.

Two modes, both re-deriving every fact from the Forgejo API and the merged
checkout, so a forged nudge or a compromised runner cannot cause a run:

  task-request   an agent files `run: <brief>` with the `task-request` label.
                 Only a TRACKED brief marked `dispatch: auto` runs, the issue
                 body is never passed on, and each brief runs at most hourly.
                 (node_dispatch/task_request.py)
  assigned       the operator assigns an issue to agent-dev. Only an
                 assignment MADE BY THE OPERATOR authorizes a launch; the
                 issue is claimed with `in-progress` and worked by a detached
                 scripts/dispatch_run.py. (node_dispatch/assigned.py)

Spend is bounded by LiteLLM (every run mints its own budget-capped key);
DISPATCH_MAX_CONCURRENCY is only a host-resource backstop.

This is a behavior-preserving port of the bash, section for section; the
comments are the bash's, and node_dispatch/test_equivalence.py replays
scenarios recorded from it. The one Forgejo-outage behavior of the bash — an
unreadable API answer ends the pass with no log line (#62) — is kept
deliberately; its fix is a follow-up on top of this, not part of a port.
"""

import glob
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from node_dispatch import assigned, task_request                  # noqa: E402
from node_host import envfile, forgejo, jail, text                # noqa: E402
from node_host.frontmatter import front_file                      # noqa: E402
from node_host.host import Host                                   # noqa: E402
from node_host.lock import CONTENDED, HELD, STOLEN, PassLock      # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = ".task-dispatch"
STALE_AFTER = 1800          # a crashed pass's lock is stolen after 30 min
COOLDOWN = 3600             # per-brief and per-failed-issue: at most hourly


def say(msg):
    print(msg, flush=True)


class Abort(Exception):
    """A step the bash ran under `set -e` failed; `.code` is its status."""

    def __init__(self, code, why):
        super().__init__(why)
        self.code = code


class MissingEnv(Exception):
    pass


def require(env, *names):
    for name in names:
        if name not in env:
            raise MissingEnv(f"{name}: unbound variable (set it in .env)")
    return [env[name] for name in names]


def must(code, what):
    """A write the bash ran bare: an unreachable Forgejo ended the pass."""
    if code != 0:
        raise Abort(code, f"{what} failed (curl status {code})")


def age(path: Path, now: float):
    """Whole seconds since `path`'s mtime if it is a regular file, else None."""
    try:
        if not path.is_file():
            return None
        return int(now) - int(path.stat().st_mtime)
    except OSError:
        return None


class Dispatcher:
    def __init__(self, repo_root, env, host, transport=forgejo.pinned_transport):
        self.root = Path(repo_root)
        self.env = env
        self.host = host
        domain, token, repo = require(env, "NODE_DOMAIN", "AGENT_FORGEJO_TOKEN",
                                      "COORDINATION_REPO")
        self.repo = repo
        self.api = forgejo.Forgejo(domain, token, repo, transport)
        # the ONLY actor whose assignment authorizes a launch (host env may override)
        self.operator = (env.get("OPERATOR_LOGIN") or env.get("FORGEJO_ADMIN_USER")
                         or "operator")
        self.agent = env.get("AGENT_GIT_USER") or "agent-dev"   # who we dispatch for

    def audit(self, issue, action, detail):
        line = text.audit_line(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               issue, action, "", detail)
        with open(self.root / STATE / "dispatch-audit.log", "a") as f:
            f.write(line)

    # --- the pass ------------------------------------------------------------

    def run(self, dry: bool) -> int:
        (self.root / STATE).mkdir(parents=True, exist_ok=True)  # cooldown stamps

        # Pass lock: at most one PASS at a time so claim-checks never race.
        # Detached issue runs do NOT hold it. Per-issue exclusivity does not
        # depend on it — the in-progress label is the durable claim — this
        # only stops two passes claiming the same fresh issue in one instant.
        lock = PassLock(self.root / STATE / "pass.lock", STALE_AFTER, self.host.now)
        verdict = lock.acquire()
        if verdict in (STOLEN, CONTENDED):
            say("[dispatch] stealing stale pass lock (>30m)")
            if verdict == CONTENDED:
                say("[dispatch] lock contended; exiting")
                return 0
        elif verdict == HELD:
            say("[dispatch] another pass holds the lock; exiting")
            return 0
        with lock:
            self.task_requests(dry)
            self.assigned_issues(dry)
            self.consume_doorbell()
        say(f"[dispatch] pass complete: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
        return 0

    # --- task-request mode ----------------------------------------------------

    def task_requests(self, dry: bool):
        requests = [(str(i["number"]), str(i["title"])) for i in json.loads(
            self.api.get_text("issues?state=open&labels=task-request&type=issues"))]
        for num, title in requests:
            name = task_request.brief_name(title)
            say(f"[dispatch] #{num} -> brief '{name}'")
            brief = self.root / "tasks" / f"{name}.md"
            is_file = bool(name) and brief.is_file()
            kind = task_request.verdict(
                name, is_file, front_file(brief, "dispatch") if is_file else "")
            if kind != task_request.ELIGIBLE:
                must(self.api.comment(num, task_request.rejection(kind, name)), "comment")
                must(self.api.close(num), "close")
                continue
            stamp = self.root / STATE / name
            elapsed = age(stamp, self.host.now())
            if elapsed is not None and elapsed < COOLDOWN:
                say(f"[dispatch] #{num} deferred — '{name}' ran within the hour")
                continue

            if dry:
                must(self.api.comment(num, task_request.dry_run_comment(
                    name, front_file(brief, "budget_usd"), front_file(brief, "model"))),
                    "comment")
                continue

            stamp.touch()
            _, out = self.host.run_combined(
                self.host.script("run-task.sh") + [f"tasks/{name}.md"])
            out = text.tail_lines(out, 15)
            # Jail summary: brief frontmatter + AGENT_IMAGE + the skills library
            # as shipped in this checkout. No difficulty override on this path,
            # so model and budget are exactly what the brief says.
            harness = front_file(brief, "harness") or "forge"
            model = (front_file(brief, "model") or self.env.get("AGENT_FAST_MODEL")
                     or "deepseek-flash")
            budget = front_file(brief, "budget_usd") or "0.50"
            image = jail.image_name(self.env)
            skill_list, skill_count = jail.skills(self.root)
            tail = jail.summary_tail(harness, image,
                                     jail.short_id(self.host.image_id(image)),
                                     skill_list, skill_count)
            must(self.api.comment(num, task_request.ran_comment(
                name, model, budget, tail, out)), "comment")
            must(self.api.close(num), "close")

    # --- assigned-issue mode --------------------------------------------------

    def assigned_issues(self, dry: bool):
        # The in-progress label must pre-exist — creating it at runtime is how
        # coordination's other labels got triplicated. Look up, don't create.
        inprog = forgejo.label_id(self.api.get_text("labels"), assigned.IN_PROGRESS)
        max_conc = self.env.get("DISPATCH_MAX_CONCURRENCY") or "4"
        if not inprog:
            say(f"[dispatch] assigned-issue mode DISABLED: no 'in-progress' label in "
                f"{self.repo} (operator must create it once)")
            return
        for num in assigned.eligible(
                self.api.get_text("issues?state=open&type=issues&limit=50"), self.agent):
            say(f"[assign] #{num} assigned to {self.agent}, unclaimed — checking "
                f"authorization")

            actor = assigned.assign_actor(
                self.api.get_text(f"issues/{num}/timeline?limit=100"), self.agent)
            if actor != self.operator:
                say(f"[assign] #{num} REFUSED: latest assignment actor="
                    f"'{actor or 'none'}' != operator='{self.operator}'")
                self.audit(num, "refused", f"actor={actor or 'none'}")
                if not dry:
                    must(self.api.comment(num, assigned.refusal_comment(self.agent, actor)),
                         "comment")
                continue

            # Retry cooldown: a launch that FAILED released the claim and
            # stamped here. Bounded hot-looping, eventual retry.
            elapsed = age(self.root / STATE / f"issue-{num}", self.host.now())
            if elapsed is not None and elapsed < COOLDOWN:
                say(f"[assign] #{num} deferred — a launch failed within the hour; "
                    f"cooling down")
                self.audit(num, "deferred", "cooldown")
                continue

            # Host-resource backstop (not spend): at the ceiling, leave the
            # issue UNCLAIMED so the next pass dispatches it.
            if max_conc != "0" and self.host.running_tenants() >= int(max_conc):
                say(f"[assign] #{num} deferred — {max_conc} tenants already running "
                    f"(host cap); next pass")
                self.audit(num, "deferred", f"at-host-cap={max_conc}")
                continue

            if dry:
                say(f"[assign] #{num} WOULD LAUNCH issue-work (operator-authorized, "
                    f"unclaimed)")
                continue

            # Claim FIRST — the durable per-issue lock — then spawn the run
            # DETACHED so it outlives this pass and runs alongside other issues.
            # If the spawn itself fails the run never starts, so its own
            # failure path cannot fire: release the claim here, or the issue
            # is stranded "in-progress" forever.
            must(self.api.add_label(num, inprog), "claim")
            say(f"[assign] #{num} claimed (in-progress); spawning detached issue-work")
            try:
                self.host.spawn_detached(self.host.script("dispatch_run.py") + [num],
                                         self.root / STATE / "dispatch-run.log")
            except OSError:
                self.api.remove_label(num, inprog)                 # `|| true`
                must(self.api.comment(num, assigned.SPAWN_FAILED_COMMENT), "comment")
                self.audit(num, "spawn-failed", "detach spawn returned nonzero")
                continue
            # The "Dispatched to..." comment with the jail summary is posted by
            # dispatch_run.py once it has resolved the difficulty tier.
            self.audit(num, "dispatched", f"actor={actor}")
            self.host.sleep(3)   # let the container register before the next cap check

    def consume_doorbell(self):
        """Doorbell markers are wake signals only — this pass re-derived
        everything — so clearing them just stops relaunch churn."""
        spool = self.env.get("DISPATCH_SPOOL", "")
        if spool and os.path.isdir(spool):
            # glob.glob, not Path.glob: like the shell's `*`, it skips dotfiles
            for marker in glob.glob(os.path.join(spool, "*.nudge")):
                try:
                    os.unlink(marker)
                except OSError:
                    pass


def main(argv, repo_root=REPO_ROOT, base_env=None, host=None,
         transport=forgejo.pinned_transport) -> int:
    dry = len(argv) > 1 and argv[1] == "--dry-run"
    try:
        env = envfile.load(Path(repo_root) / ".env",
                           dict(os.environ if base_env is None else base_env))
        max_conc = env.get("DISPATCH_MAX_CONCURRENCY") or "4"
        if not max_conc.lstrip("-").isdigit():
            raise MissingEnv(f"DISPATCH_MAX_CONCURRENCY={max_conc!r} is not an integer")
        os.chdir(repo_root)       # `cd "$(dirname "$0")/.."`
        dispatcher = Dispatcher(repo_root, env, host or Host(repo_root, env), transport)
        return dispatcher.run(dry)
    except Abort as exc:
        sys.stderr.write(f"[dispatch] {exc}\n")
        return exc.code
    except (OSError, envfile.EnvFileError, MissingEnv) as exc:
        sys.stderr.write(f"[dispatch] {exc}\n")
        return 1
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # An unreadable Forgejo answer, where the bash's `set -e` ended the pass.
        sys.stderr.write(f"[dispatch] unreadable Forgejo response: {exc!r}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
