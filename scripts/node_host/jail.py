"""
jail — the "Jail summary" both dispatch paths post, so the issue thread records
WHICH jail actually ran: model and budget, harness, image fingerprint, and the
skills library as shipped in this checkout.

The task-request path (task_dispatcher.py) and the assigned-issue path
(dispatch_run.py) each carried their own copy of this block in bash; the
shared lines are here once. Every field degrades to a placeholder — a missing
image is "?", an empty skills library is "(empty)" — and nothing here can
block a launch.
"""

import glob
import os

DEFAULT_IMAGE = "sovereign-node/agent:local"


def image_name(env) -> str:
    return env.get("AGENT_IMAGE") or DEFAULT_IMAGE


def short_id(image_id: str) -> str:
    """`${IMG_ID#sha256:}` cut to 12 chars, or "?" when docker had nothing."""
    short = image_id[len("sha256:"):] if image_id.startswith("sha256:") else image_id
    return short[:12] or "?"


def skills(repo_root) -> tuple:
    """(comma list, count) of skills/*/SKILL.md directory names.

    The count is `awk -F',' '{print NF}'` over the joined list, as it was — the
    two only disagree for a directory name containing a comma.
    """
    pattern = os.path.join(str(repo_root), "skills", "*", "SKILL.md")
    names = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(pattern))
    joined = ",".join(names)
    if not joined:
        return "(empty)", 0
    return joined, len(joined.split(","))


def summary_tail(harness: str, image: str, img_short: str, skill_list: str,
                 skill_count: int) -> str:
    """The three lines after **Model:**, shared by both comments."""
    return (f"- **Harness:** `{harness}`\n"
            f"- **Image:** `{image}` (sha256: `{img_short}`)\n"
            f"- **Skills available:** {skill_list} ({skill_count})")
