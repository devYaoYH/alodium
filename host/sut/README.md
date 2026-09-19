# Isolated PR SUT gate

This is the deterministic full-stack test for node-config pull requests. It is
deliberately **not** a Forgejo Actions workflow in `node-config`: agents can
push to a PR, and therefore could push workflow YAML. Instead, a host-owned
dispatcher re-derives the PR head from Forgejo, checks it out using its own
token, and sends a secret-free archive into a dedicated Docker VM.

```
operator adds `requires-sut` ─▶ host SUT dispatcher ─▶ queue ─▶ pool of Colima/KVM workers
         to a node-config PR        (launchd, 2 min)      FIFO     └─ Compose + smoke tests
                                                                        └─▶ result comment on PR
```

A test is **requested**, not automatic: a full bring-up takes several minutes
and a worker VM is real host capacity, so the operator picks the PRs that need
one. Only the operator's label counts. Agents hold write on node-config and can
label their own PRs; the dispatcher reads the label's author from the PR
timeline and answers anyone else with a "not queued" comment.

- Every head pushed while the label is on gets one test.
- Remove and re-add the label to test the same head again (for example after
  fixing the SUT controller itself).
- Requests wait in FIFO order of labeling. The pool size, `SUT_POOL_SIZE` in
  `.task-sut/config.env`, is 1 by default; more slots mean more Colima
  profiles (`geth-sut-01`, `geth-sut-02`, ...) and more host memory.

The worker has its own Docker daemon. On macOS the Colima profile is created
with host mounts and port forwarding disabled. Production's Docker context,
source checkout, secrets, and containers are never provided to the candidate.
Since a candidate Compose file could compromise its own Docker VM, workers are
single-use by default: the controller destroys the complete VM and its data
after every result, and recreates it before the next one. The current
implementation provisions macOS; the `sutctl` interface is intentionally
provider-neutral for the Linux KVM worker.

## Install on macOS

One command, safe to re-run to repair an install:

```sh
brew install colima                          # once
ENABLE_SUT=1 ./scripts/bootstrap-forgejo.sh  # once: mints SUT_FORGEJO_TOKEN into .env
./host/sut/setup.sh
```

`setup.sh` checks the prerequisites, initializes the worker profile, creates
the `requires-sut` label on node-config, clears state left by the old watcher,
installs the launchd dispatcher, and finishes with `sutctl.sh doctor`.

The SUT token belongs to a dedicated development machine; the stable/default
node does not provision it. Its scopes are `read:repository` (clone PR heads
and allowlisted build sources) plus `read:issue`/`write:issue` (read the label
timeline, create the label, post evidence comments). It is separate from the
node-operations token. Evidence lives under the gitignored
`.task-sut/results/` directory (JSON result, controller log, worker
Compose/test log) and as PR comments: queued, started, and PASS/FAIL with the
tail of the worker log on failure.

## What a test does

For each PR head, the controller clones the exact SHA on the host, strips
`.git`, `.env`, `secrets/`, and host state, transfers the remaining tree over
the VM's SSH channel, then runs a trusted worker helper. That helper creates
synthetic configuration, validates the candidate Compose graph, starts the
staging overlay using throwaway volumes, builds the candidate's local app
images in the worker, rejects containers that crash-loop during the initial
settling window, and executes manifest-declared smoke tests. It captures
output, tears the stack down, deletes the candidate tree, and returns a small
JSON result.

Some node services use images built from a mirrored repository rather than
public registry images. The host reads their `[build]` blocks (image, repo,
ref, args, patch) from the candidate's own manifests, the same source deploy's
`build-mirrored.sh` uses, so the gate cannot drift from deploy. It clones a
repository only if it is on the reviewed [repository allowlist](sources.toml),
strips Git metadata, and transfers the snapshots to the worker, where the
candidate's node-config patch is applied and the image is built. A PR can move
a ref, but it cannot name another private repository for the host to clone.

Browser-side dependencies (`[[vendor]]` in manifests) are fetched inside the
worker from the public npm registry and checked against the manifest's pinned
sha512. The node's own package mirror would need a credential the worker must
not hold.

`PASS` is test evidence, not approval. The operator still decides whether to
merge. The model-based reviewer is a later layer, after this deterministic gate
is proven reliable.

## Operating controls

```sh
./host/sut/sutctl.sh status             # pool slots and the latest results
./host/sut/sutctl.sh dispatch           # one pass now (launchd runs this every 2 minutes)
./host/sut/sutctl.sh test 42            # test PR #42's current head now, label or not
./host/sut/sutctl.sh run 42 <head-sha>  # reproduce a result without commenting
./host/sut/sutctl.sh stop               # release a warm worker's memory
launchctl unload ~/Library/LaunchAgents/node.sutwatch.plist
```

For a manual reproduction from a clean checkout, pass the three node values as
process environment instead of creating an `.env` there:

```sh
NODE_DOMAIN=localhost NODE_CONFIG_REPO=operator/node-config SUT_FORGEJO_TOKEN=... \
  ./host/sut/sutctl.sh run 42 <head-sha>
```

Pool slots are never created from PR input: the size comes only from the
host-only config file.
