"""
node_verify — the pre-push config gate, split so the checks can be tested.

`verify-config.sh` is the gate every agent must pass before pushing
(tasks/issue-work.md). It was 554 lines of bash wrapping seven heredoc'd
Python programs, which meant the only way to ask "does this check still bite?"
was to break the repo on purpose and look.

So the checks are functions over parsed data here, and their pass AND fail
branches are asserted from fixtures instead:

  - `strict_yaml` — the duplicate-key loader, with the self-checks that run
    before any real file. It has been got wrong twice.
  - `checks`      — yaml parse, dispatch tiers vs litellm, model pricing.
  - `containment` — the copilot's code/data-plane split.
  - `provenance`  — every first-party image must be reproducible.
  - `discovery`   — which files a section looks at (pure list logic).
  - `report`      — the OK/SKIP/FAIL vocabulary and the exit code.

`runner` is the only module that shells out: caddy, shellcheck, git,
ensure-tier-labels.sh and the unit-test subprocesses live there and nowhere
else. Nothing in this package may mutate the node — it reads config text.

PyYAML (already required by the gate this replaces) plus the stdlib. No pip.
"""
