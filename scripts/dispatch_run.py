#!/usr/bin/env python3
"""
One dispatched issue-work run, DETACHED from the dispatcher pass so tenants run
concurrently: task_dispatcher.py claims the issue (adds `in-progress`), spawns
this in its own session, and moves on. This process owns the run's whole
lifecycle — tier resolution, the ephemeral tenant, the completion comment,
releasing the claim on failure, and the audit trail. Not called by humans.

    scripts/dispatch-run.sh <issue-number>     (or this file directly)

Per-issue exclusivity is guaranteed by the caller: the claim is added BEFORE
this spawns and the dispatcher's scan skips claimed issues. Spend is bounded by
LiteLLM (each run mints its own budget-capped key).

Operator-gated labels take two checks: the label is on the issue NOW, and the
most recent ADD of it was the operator's (node_dispatch/assigned.py). An agent
labelling its own issue `difficulty:hard` or `trace` is ignored.

Everything here fails soft, as the bash (`set -uo pipefail`, no `-e`) did: an
unanswered API call costs a comment or a label, never the run — with one
deliberate exception, the LiteLLM check below, which aborts rather than launch
a model it could not confirm. Behavior-preserving port; the section comments
are the bash's and node_dispatch/test_equivalence.py replays recorded runs.
"""

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from node_dispatch import assigned, tiers                         # noqa: E402
from node_host import envfile, forgejo, jail, text                # noqa: E402
from node_host.frontmatter import front_file                      # noqa: E402
from node_host.host import Host                                   # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = ".task-dispatch"
BRIEF = "tasks/issue-work.md"
TIERS = "config/dispatch-tiers.yaml"
_TRACE_DIR = re.compile(r".*trace saved to (traces/[A-Za-z0-9._-]*)")


def say(msg):
    print(msg, flush=True)


def trace_dir(out: str) -> str:
    """`sed -n 's#.*trace saved to \\(traces/[A-Za-z0-9._-]*\\).*#\\1#p' | tail -1`"""
    found = ""
    for line in text.lines(out + "\n"):
        m = _TRACE_DIR.match(line)
        if m:
            found = m.group(1)
    return found


class Run:
    def __init__(self, repo_root, env, host, num, transport=forgejo.pinned_transport):
        self.root = Path(repo_root)
        self.env = env
        self.host = host
        self.num = num
        self.transport = transport
        self.domain = env["NODE_DOMAIN"]
        self.api = forgejo.Forgejo(self.domain, env["AGENT_FORGEJO_TOKEN"],
                                   env["COORDINATION_REPO"], transport)
        self.operator = (env.get("OPERATOR_LOGIN") or env.get("FORGEJO_ADMIN_USER")
                         or "operator")
        self.agent = env.get("AGENT_GIT_USER") or "agent-dev"
        self.run_tag = f"issue-{num}"      # coarse tag; run-task mints its own alias
        self.stamp = self.root / STATE / f"issue-{num}"
        self.inprog = ""

    # --- helpers -------------------------------------------------------------

    def comment(self, body):
        self.api.comment(self.num, body)        # soft: costs the comment only

    def audit(self, action, detail):
        line = text.audit_line(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               self.num, action, self.run_tag, detail)
        try:
            with open(self.root / STATE / "dispatch-audit.log", "a") as f:
                f.write(line)
        except OSError:
            pass                    # `printf >> log` failing cost the line only

    def release_claim(self):
        try:
            self.stamp.touch()      # the 1h cooldown the dispatcher honours
        except OSError:
            pass                    # `touch` failing did not stop the bash either
        if self.inprog:
            self.api.remove_label(self.num, self.inprog)

    def label_actor(self, label) -> str:
        return assigned.label_add_actor(
            self.api.get_text(f"issues/{self.num}/timeline?limit=100"), label)

    def llm_check(self, model) -> str:
        """`$(curl … /v1/models | python3 … || echo "error:curl_failed")`.

        Under pipefail a failed curl ALSO fires the `|| echo`, so an outage
        reads as the parse error AND "error:curl_failed", two lines, and that
        is what the log line, the comment and the audit entry carried. Kept."""
        body, reached = forgejo.fetch(
            self.transport, f"https://llm.{self.domain}/v1/models",
            {"Authorization": f"Bearer {self.env.get('LITELLM_MASTER_KEY', '')}"},
            timeout=10)
        check = tiers.llm_check(body, model)
        return check if reached else check + "\nerror:curl_failed"

    # --- the run -------------------------------------------------------------

    def run(self) -> int:
        n = self.num
        try:
            self.inprog = forgejo.label_id(self.api.get_text("labels?limit=100"),
                                           assigned.IN_PROGRESS)
        except Exception:                                    # noqa: BLE001
            self.inprog = ""
        labels = assigned.label_names(self.api.get_text(f"issues/{n}"))

        # --- difficulty tier resolution ---------------------------------------
        # Gates, each independently sufficient to fall back: the label must be
        # the operator's; an unknown tier falls back to the default tier; a
        # model LiteLLM does not serve falls back to deepseek-flash (loudly);
        # verify-config checks tier models exist in litellm.yaml.
        model, budget, source = "", "", "brief"
        data = tiers.load(TIERS)          # relative, as the bash opened it

        diff_label = assigned.difficulty_label(labels)
        if diff_label:
            actor = self.label_actor(diff_label)
            if not (actor and actor == self.operator):
                say(f"[dispatch-run] #{n}: label '{diff_label}' present but not added "
                    f"by operator (actor='{actor or 'none'}'); ignoring")
                diff_label = ""

        if diff_label:
            tier = diff_label[len("difficulty:"):]
            say(f"[dispatch-run] #{n}: operator-applied label '{diff_label}' -> tier '{tier}'")
            resolved = tiers.resolve_label(data, tier)
            kind = tiers.classify(resolved)
            if kind == tiers.ERROR:
                say(f"[dispatch-run] #{n}: ERROR parsing dispatch-tiers.yaml: "
                    f"{resolved[len('error:'):]}")
            elif kind == tiers.UNKNOWN:
                say(f"[dispatch-run] #{n}: unknown tier '{tier}' "
                    f"({resolved[len('unknown:'):]}); falling back to brief default")
                self.comment(tiers.unknown_comment(diff_label, tier))
            elif kind == tiers.RESOLVED:
                model, budget = tiers.split(resolved)
                source = "difficulty-label"
                say(f"[dispatch-run] #{n}: resolved tier '{tier}' -> model={model} "
                    f"budget=\"{budget}\"")
                # Distinguish "model definitively absent" (fall back) from
                # "couldn't reach LiteLLM" (abort — never silently run the
                # wrong model).
                check = self.llm_check(model)
                if check == "not_found":
                    say(f"[dispatch-run] #{n}: model '{model}' not in LiteLLM model "
                        f"list; falling back to deepseek-flash")
                    self.comment(tiers.not_served_comment(tier, model))
                    model, budget, source = (tiers.FALLBACK_MODEL,
                                             tiers.FALLBACK_BUDGET, "fallback")
                elif check != "live":
                    say(f"[dispatch-run] #{n}: could not reach LiteLLM ({check}); "
                        f"aborting — won't silently run the wrong model")
                    self.comment(tiers.unreachable_comment(check, tier, model))
                    # cooldown stamp, so the dispatcher does not re-dispatch
                    # during an outage; release the claim so it is not stranded
                    self.release_claim()
                    self.audit("abort", f"LiteLlm unreachable: {check}")
                    return 1

        if not model:
            default = tiers.default_tier(data)
            if default:
                model, budget = tiers.split(default)
                source = "default-tier"
                say(f"[dispatch-run] #{n}: no difficulty label, using default tier -> "
                    f"model={model} budget=\"{budget}\"")

        # --- jail summary on the kickoff comment ------------------------------
        # Computed AFTER tier resolution so the model shown is the one that runs.
        harness = front_file(self.root / BRIEF, "harness") or "forge"
        image = jail.image_name(self.env)
        img_short = jail.short_id(self.host.image_id(image))
        skill_list, skill_count = jail.skills(self.root)
        self.comment(
            f"Dispatched to an ephemeral `{self.agent}` tenant (operator-authorized). "
            f"Claimed with `in-progress`.\n"
            f"\n"
            f"**Jail summary**\n"
            f"- **Model:** `{model or '<unresolved>'}` (budget ${budget or '0'}, "
            f"resolved via `{source or 'brief'}`)\n"
            f"{jail.summary_tail(harness, image, img_short, skill_list, skill_count)}\n"
            f"\n"
            f"Deliverable is a node-config PR + a comment here; if I'm blocked I'll "
            f"say so.")
        self.audit("announced", f"model={model or '?'} harness={harness} "
                                f"image={image}:{img_short} skills={skill_count}")

        # --- tracing: the `trace` label ---------------------------------------
        # Tracing grants no privilege, but it keeps the container until teardown
        # and writes to the host's traces/, so it takes the same operator gate.
        trace = False
        if "trace" in labels:
            actor = self.label_actor("trace")
            if actor and actor == self.operator:
                trace = True
                say(f"[dispatch-run] #{n}: operator-applied label 'trace' -> tracing this run")
            else:
                say(f"[dispatch-run] #{n}: label 'trace' present but not added by "
                    f"operator (actor='{actor or 'none'}'); ignoring")

        argv = self.host.script("run-task.sh") + [BRIEF, "--issue", n]
        if model:
            argv += ["--model", model]
        if budget:
            argv += ["--budget", budget]
        if trace:
            argv += ["--trace"]
        rc, out = self.host.run_combined(argv)
        out = text.capture(out)

        if rc == 0:
            self.comment(
                "Run finished — see the agent's comment above for the PR. **To request "
                "changes:** leave your feedback as a comment here, then **remove the "
                "`in-progress` label**; that re-launches a tenant which reads your "
                "feedback and revises the same PR. (A comment alone does nothing — "
                "removing the label is the 'go again' signal.) Leave `in-progress` on "
                "and the issue rests until you merge or clear it.")
            self.audit("completed", "rc=0")
        else:
            # Failed: release the claim so the issue is not stranded + cooldown.
            self.release_claim()
            self.comment(
                f"Dispatch FAILED (exit {rc}) — released `in-progress` for retry (1h "
                f"cooldown). Tail:\n"
                f"\n"
                f"```\n"
                f"{text.clean_run(out)}\n"
                f"```\n"
                f"Fix the cause, then re-assign or wait for the cooldown.")
            self.audit("failed", f"rc={rc}")

        # --- trace link -------------------------------------------------------
        # Render (--wait rides out LiteLLM's batched spend-log writes) and post
        # the link as its own comment. The summary names tools and durations
        # only, never command text: every tenant can read coordination.
        if trace:
            tdir = trace_dir(out)
            if tdir and (self.root / tdir).is_dir() and self.host.run_quiet(
                    self.host.script("trace-render.py") + [tdir, "--wait", "300"]) == 0:
                try:
                    summary = text.capture((self.root / tdir / "summary.md").read_text())
                except OSError:
                    summary = ""
                self.comment(f"**Run trace:** https://traces.{self.domain}/"
                             f"{tdir[len('traces/'):]}/trace.html\n\n{summary}")
                self.audit("traced", tdir[len("traces/"):])
            else:
                self.comment(
                    f"Tracing was requested (`trace` label) but no trace was rendered "
                    f"for this run ({tdir or 'run-task.sh reported no trace directory'}). "
                    f"Render by hand on the host: `./scripts/trace-render.py traces/<run>`.")
                self.audit("trace_failed", tdir or "no trace directory")
        return 0


def main(argv, repo_root=REPO_ROOT, base_env=None, host=None,
         transport=forgejo.pinned_transport) -> int:
    try:
        env = envfile.load(Path(repo_root) / ".env",
                           dict(os.environ if base_env is None else base_env))
    except (OSError, envfile.EnvFileError) as exc:
        sys.stderr.write(f"dispatch-run: {exc}\n")
        return 1
    if len(argv) < 2 or argv[1] == "":
        sys.stderr.write("dispatch-run: usage: dispatch-run.sh <issue-number>\n")
        return 1
    num = argv[1]
    if not re.fullmatch(r"[0-9]+", num):
        say("dispatch-run: issue must be an integer")
        return 2
    missing = [k for k in ("NODE_DOMAIN", "AGENT_FORGEJO_TOKEN", "COORDINATION_REPO")
               if k not in env]
    if missing:
        sys.stderr.write(f"dispatch-run: {', '.join(missing)}: unbound variable "
                         f"(set it in .env)\n")
        return 1
    os.chdir(repo_root)           # `cd "$(dirname "$0")/.."`
    return Run(repo_root, env, host or Host(repo_root, env), num, transport).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
