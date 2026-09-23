"""
node_deploy — the node's deploy, split so the decisions can be tested.

A deploy is a sequence of side effects, but every side effect is chosen by a
decision: this merge changed these files, so these app images get rebuilt,
these running services get restarted, this compose profile set is visible,
these newly added services should have started and did not, and the result is
ok / warning / failed. Those live in `changes`, `compose` and `info` as pure
functions over plain data — a list of paths, a parsed compose config, a set of
service names — so `test_*.py` checks them in milliseconds against fixtures
recorded from the real node, with no Docker daemon and nothing deployed.

`runner` is the only module that shells out: git, docker, docker compose and
the sibling scripts. Nothing else in this package may start a subprocess.

This is a BEHAVIOR-PRESERVING port of scripts/deploy.sh. Three known defects
are reproduced deliberately and are documented at their sites:

  1. `changes.changed_files` is computed from OLD_HEAD..HEAD, where OLD_HEAD is
     read before the fast-forward merge — so an operator who pulled main first
     deploys nothing while the run reports ok.
  2. The agent jail image is built out-of-band (deploy.py step 4d) and never
     through `docker compose build agent`; a failed smoke test keeps the stale
     image behind a WARN.
  3. A newly added profile-gated service is WARNed about, never started.

Each is a separate follow-up PR with its own test. Fixing one here would make
equivalence with the bash unprovable, which is the only evidence this port has.

Stdlib only — the node's python3 is what runs this, with no pip.
"""
