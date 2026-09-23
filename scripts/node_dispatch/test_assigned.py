#!/usr/bin/env python3
"""
Offline tests for node_dispatch.assigned — who may make the host launch a
tenant, and which labels an agent may NOT grant itself.

  eligible          assignee + unclaimed; anything else is invisible
  assign_actor      the LATEST non-removed assignment of agent-dev, by
                    created_at — an agent re-assigning after the operator
                    wins, which is exactly what makes the run refused
  label_add_actor   the most recent ADD of a label; a remove does not count,
                    and an agent re-adding after the operator owns the label
  label_names / difficulty_label   the grep -m1 / grep -qx the bash used

The scenario replay (test_equivalence.py) proves these match the bash in
context; this file pins each branch in isolation, including the failure modes
(which raise, which read as "").

Run:  python3 scripts/node_dispatch/test_assigned.py   (from the repo root)
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_dispatch import assigned as a                          # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def raises(fn, *args):
    try:
        fn(*args)
    except Exception:                                        # noqa: BLE001
        return True
    return False


def ev(actor, assignee, at, removed=False):
    return {"type": "assignees", "user": {"login": actor},
            "assignee": {"login": assignee}, "removed": removed, "created_at": at}


def lab(actor, name, add=True):
    return {"type": "label", "user": {"login": actor}, "label": {"name": name},
            "body": "1" if add else ""}


J = json.dumps

print("eligible")
issues = [{"number": 1, "assignees": [{"login": "agent-dev"}], "labels": []},
          {"number": 2, "assignees": [{"login": "agent-dev"}], "labels": [{"name": "in-progress"}]},
          {"number": 3, "assignees": [{"login": "other"}], "labels": []},
          {"number": 4, "assignees": None, "labels": None},
          {"number": 5, "assignees": [None, {"login": "agent-dev"}], "labels": [None]}]
check("assigned + unclaimed only, in API order", a.eligible(J(issues), "agent-dev") == ["1", "5"])
check("unreadable body raises (the pass ends)", raises(a.eligible, "", "agent-dev"))

print("assign_actor")
check("operator assignment", a.assign_actor(J([ev("operator", "agent-dev", "1")]), "agent-dev") == "operator")
check("agent re-assigning later wins -> refused",
      a.assign_actor(J([ev("operator", "agent-dev", "1"), ev("agent-dev", "agent-dev", "2")]),
                     "agent-dev") == "agent-dev")
check("list order does not matter; created_at does",
      a.assign_actor(J([ev("operator", "agent-dev", "3"), ev("agent-dev", "agent-dev", "2")]),
                     "agent-dev") == "operator")
check("a removal event is not an assignment",
      a.assign_actor(J([ev("operator", "agent-dev", "1"), ev("agent-dev", "agent-dev", "2", True)]),
                     "agent-dev") == "operator")
check("assigning someone else is irrelevant",
      a.assign_actor(J([ev("operator", "other", "9")]), "agent-dev") == "")
check("no events -> '' (refused)", a.assign_actor("[]", "agent-dev") == "")
check("label events are ignored", a.assign_actor(J([lab("operator", "x")]), "agent-dev") == "")
check("unreadable timeline raises (the pass ends)", raises(a.assign_actor, "oops", "agent-dev"))

print("label_add_actor")
check("operator added it", a.label_add_actor(J([lab("operator", "trace")]), "trace") == "operator")
check("the most recent ADD wins",
      a.label_add_actor(J([lab("operator", "trace"), lab("operator", "trace", False),
                           lab("agent-dev", "trace")]), "trace") == "agent-dev")
check("a remove after the add does not change who added it",
      a.label_add_actor(J([lab("operator", "trace"), lab("agent-dev", "trace", False)]),
                        "trace") == "operator")
check("other labels are ignored", a.label_add_actor(J([lab("operator", "x")]), "trace") == "")
check("unreadable timeline -> '' (fails soft, and '' is never the operator)",
      a.label_add_actor("oops", "trace") == "" and a.label_add_actor('{"a": 1}', "trace") == "")

print("label_names / difficulty_label")
names = a.label_names(J({"labels": [{"name": "bug"}, {"name": "difficulty:hard"},
                                    {"name": "difficulty:easy"}, {"name": "trace"}]}))
check("names in order", names == ["bug", "difficulty:hard", "difficulty:easy", "trace"])
check("first difficulty:* wins (grep -m1)", a.difficulty_label(names) == "difficulty:hard")
check("no difficulty label -> ''", a.difficulty_label(["bug"]) == "")
check("'trace' must be an exact line (grep -qx)", "trace" not in a.label_names(J({"labels": [{"name": "traced"}]})))
check("unreadable issue -> []", a.label_names("") == [] and a.label_names('{"labels": [{"name": null}]}') == [])

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
