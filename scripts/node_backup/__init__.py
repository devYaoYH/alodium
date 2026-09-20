"""
node_backup — the node's backup, split so the decisions can be tested.

Everything load-bearing in a backup is a decision: does this volume get
included, skipped or is its absence a failure; is this database dumped,
degraded or legitimately absent; which retention pool does the resulting
snapshot belong to. Those live in `plan` and `policy` as pure functions over
plain dicts, so `test_plan.py` and `test_policy.py` check them in milliseconds
with no Docker daemon and no fault injection.

`runner` is the only module that shells out. `config` resolves where the
repository and the passphrase come from.

Stdlib only — the node's python3 is what runs this, with no pip.
"""
