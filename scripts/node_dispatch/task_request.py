"""
task_request — the `run: <brief>` path: an agent files a coordination issue
with the `task-request` label, and the host decides whether to run a brief.

The rules that keep this safe to automate, each tested in test_task_request.py:

  - The issue can only NAME a brief. The name is reduced to [a-z0-9_-], so it
    cannot climb out of tasks/ or point at anything but tasks/<name>.md.
  - The brief must EXIST in the merged checkout. An agent cannot author a new
    prompt into execution; new briefs arrive as a reviewed PR.
  - The brief's own frontmatter must say `dispatch: auto`. The flag ships
    through review like any capability.
  - The issue BODY is never read. The brief is the whole prompt.
"""

import re

AUTO = "auto"

MISSING = "missing"          # reject + close: no tracked brief by that name
NOT_AUTO = "not-auto"        # reject + close: brief exists, not dispatchable
ELIGIBLE = "eligible"

_RUN_PREFIX = re.compile(r"^run:[ \t\n\v\f\r]*", re.IGNORECASE)
_KEEP = re.compile(r"[^a-z0-9_-]")
_IFS = " \t\n"


def read_title(title: str) -> str:
    """TITLE as `read -r NUM TITLE` split it off the line "<num> <title>":
    surrounding blanks trimmed, interior kept.

    Only the title's FIRST line is used. The bash printed one issue per line
    and read them back, so a title containing a newline became a second,
    phantom request whose number and brief were whatever the title said. The
    port iterates the API's issue list instead, and a title's later lines are
    simply not part of the brief name.
    """
    first = title.split("\n", 1)[0]
    return first.strip(_IFS)


def brief_name(title: str) -> str:
    """`sed 's/^run:[[:space:]]*//i' <<<"$TITLE" | tr -cd 'a-z0-9_-'`.

    Uppercase letters are DELETED, not lowercased — "run: Digest" names
    "igest". That is the bash's behavior and it fails closed (a wrong name is
    a rejected request), so it is kept.
    """
    return _KEEP.sub("", _RUN_PREFIX.sub("", read_title(title), count=1))


def verdict(name: str, brief_is_file: bool, dispatch_value: str) -> str:
    if not name or not brief_is_file:
        return MISSING
    if dispatch_value != AUTO:
        return NOT_AUTO
    return ELIGIBLE


def rejection(kind: str, name: str) -> str:
    if kind == MISSING:
        return (f"Rejected: no tracked brief `tasks/{name}.md`. New capabilities "
                f"are a `handoff` to agent-dev (brief ships as a PR), not a request.")
    return (f"Rejected: `tasks/{name}.md` is not marked `dispatch: auto`. Flipping "
            f"that flag is a reviewed PR — ask agent-dev via `handoff`.")


def dry_run_comment(name: str, budget: str, model: str) -> str:
    return (f"Dry-run: request is valid; `{name}` would run now (budget ${budget}, "
            f"model {model}). Leaving open.")


def ran_comment(name, model, budget, summary_tail, out) -> str:
    return (f"Ran `{name}` as an ephemeral tenant.\n"
            f"\n"
            f"**Jail summary**\n"
            f"- **Model:** `{model}` (budget ${budget}, from brief frontmatter)\n"
            f"{summary_tail}\n"
            f"\n"
            f"Tail of the run:\n"
            f"```\n"
            f"{out}\n"
            f"```\n"
            f"The deliverable, if any, is its own issue (label `digest`/`handoff`).")
