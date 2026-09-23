"""
node_dispatch — the host dispatcher's decisions, split so they can be tested.

The dispatcher is the bridge between "an agent wants something run" and "no
agent may touch docker". Everything that keeps that bridge safe is a decision
over data the dispatcher fetched, and each now lives here as a pure function:

  task_request  which brief a `run: <brief>` issue names, and whether it may
                run: the brief must be TRACKED in tasks/ and say
                `dispatch: auto`, and the issue body is never read
  assigned      which assigned issues are eligible, and who actually made the
                assignment — only the operator's assignment authorizes a run
  labels        whether an operator-gated label (difficulty:*, trace) was
                added by the operator, or by an agent labelling itself
  tiers         a difficulty label -> model + budget, and the LiteLLM
                liveness answer that can veto it

The wiring is scripts/task_dispatcher.py (one pass) and scripts/dispatch_run.py
(one detached issue run); node_host is what they share with deploy-watch.

Stdlib only, except `tiers.load`, which reads YAML exactly as the bash did —
with PyYAML if the host python has it, and as a recorded error if not.
"""
