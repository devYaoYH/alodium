"""
node_backup — the node's backup, split so the decisions can be tested.

Everything load-bearing in a backup is a decision: does this volume get
included, skipped or is its absence a failure; is this database dumped,
degraded or legitimately absent; which retention pool does the resulting
snapshot belong to. Those live in `plan` and `policy` as pure functions over
plain dicts, so `test_plan.py` and `test_policy.py` check them in milliseconds
with no Docker daemon and no fault injection.

`runner` is the only module that shells out. `config` resolves where the
repository and the passphrase come from. `keyring_store` is the only module
that touches the platform secret store.

Stdlib only, with one exception: `keyring_store` imports the `keyring` package
(pinned in scripts/requirements-host.txt), lazily and only when the platform
keyring is the chosen passphrase source. Nothing else may import a
third-party package, and the tests never do.
"""
