"""
assigned — the assigned-issue path: the operator assigns a coordination issue
to agent-dev, and the host launches a tenant to work it.

The trigger (the Actions doorbell, or just the heartbeat) is only a HINT. Every
fact is re-derived from the Forgejo API, and each gate is independently
sufficient to refuse:

  1. agent-dev is actually an assignee                      -> `eligible`
  2. no `in-progress` label (the durable per-issue claim)   -> `eligible`
  3. the LATEST agent-dev assignment was made BY THE
     OPERATOR. Agent tokens hold write on coordination and
     can self-assign, so "assigned" alone is not
     authorization; "assigned by the operator" is.          -> `assign_actor`
  4. only the issue NUMBER reaches the tenant, never the body.

Operator-gated labels (difficulty:*, trace) use the same idea for labels:
`label_add_actor` says who made the most recent ADD of a label, and a label an
agent put on its own issue is ignored.

These functions reproduce the bash's inline python exactly — including which
failures raise (the bash ran them under `set -e`, so a bad body ended the
pass) and which quietly yield "" (dispatch-run.sh had no `-e`).
"""

import json

IN_PROGRESS = "in-progress"


def eligible(issues_text: str, agent: str) -> list:
    """Numbers (as strings) of open issues assigned to `agent` and unclaimed,
    in API order. Raises on a body that is not a list of issues."""
    out = []
    for issue in json.loads(issues_text):
        assignees = {(a or {}).get("login") for a in (issue.get("assignees") or [])}
        labels = {(lab or {}).get("name") for lab in (issue.get("labels") or [])}
        if agent in assignees and IN_PROGRESS not in labels:
            out.append(str(issue["number"]))
    return out


def assign_actor(timeline_text: str, agent: str) -> str:
    """Who made the latest (by created_at) non-removed assignment of `agent`,
    or "" if nobody did. Raises on a body that is not a list of events."""
    events = [e for e in json.loads(timeline_text)
              if e.get("type") == "assignees" and not e.get("removed")
              and (e.get("assignee") or {}).get("login") == agent]
    events.sort(key=lambda e: e.get("created_at", ""))
    return str((events[-1].get("user") or {}).get("login", "")) if events else ""


def label_add_actor(timeline_text: str, label: str) -> str:
    """Who made the most recent ADD of `label` ("" if none, or if the body is
    unusable). Forgejo label events: add -> body '1', remove -> body ''."""
    try:
        events = json.loads(timeline_text)
        for e in reversed(events):
            if e.get("type") == "label" and e.get("body") == "1" \
                    and (e.get("label") or {}).get("name") == label:
                return str((e.get("user") or {}).get("login", ""))
    except Exception:                                        # noqa: BLE001
        return ""
    return ""


def label_names(issue_text: str) -> list:
    """The issue's current label names, as the lines of the bash's
    `print("\\n".join(l.get("name", "") ...))`; [] if the body is unusable."""
    try:
        text = "\n".join(lab.get("name", "")
                         for lab in json.loads(issue_text).get("labels", []))
    except Exception:                                        # noqa: BLE001
        return []
    return text.split("\n") if text else []


def difficulty_label(names: list) -> str:
    """`grep -m1 '^difficulty:'` — the first one, or ""."""
    return next((n for n in names if n.startswith("difficulty:")), "")


def refusal_comment(agent: str, actor: str) -> str:
    who = actor or "someone other than the operator"
    return (f"Not dispatched: this issue was assigned to `{agent}` by `{who}`, not "
            f"the operator. Auto-dispatch only honors operator assignments — an "
            f"agent cannot task another agent by self-assigning. If this is real "
            f"work, the operator should (re)assign it.")


SPAWN_FAILED_COMMENT = (
    "Dispatch FAILED before the tenant started (host-side spawn error — see "
    ".task-dispatch/dispatch-run.log). Claim released; re-assign or remove/re-add "
    "a label to retry once the host is fixed.")
