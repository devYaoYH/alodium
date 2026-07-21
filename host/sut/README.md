# Isolated PR SUT gate

This is the deterministic development-plane gate for `agent-dev` pull
requests. It is deliberately **not** a Forgejo Actions workflow in
`node-config`: agent-dev can push a PR, and therefore could push workflow YAML.
Instead, a host-owned poller re-derives the PR head from Forgejo, checks it out
using the operator token, and sends a secret-free archive into a dedicated
Docker VM.

```
agent-dev PR → host SUT watcher → Colima/KVM worker → Compose + smoke tests
                                      └────────────→ result comment on PR
```

The worker has its own Docker daemon. On macOS, `sutctl.sh init` creates a
Colima profile with host mounts and port forwarding disabled. Production's
Docker context, source checkout, secrets, and containers are never provided to
the candidate. Since a candidate Compose file could compromise its own Docker
VM, workers are single-use by default: the controller destroys the complete VM
and its data after every PR result. The current implementation provisions
macOS; the `sutctl` interface is intentionally provider-neutral for the Linux
KVM worker.

## Install on macOS

1. On a dedicated development machine, opt into the development plane with
   `ENABLE_SUT=1 ./scripts/bootstrap-forgejo.sh`. This supplies the local-only
   `SUT_FORGEJO_TOKEN`, `NODE_DOMAIN`, and `NODE_CONFIG_REPO` used by the
   watcher. The stable/default node does not provision this token. It is
   limited to source/PR reads and PR evidence comments, separate from the
   node-operations token.
2. Install Colima, then run:

   ```sh
   ./host/sut/sutctl.sh init
   ./host/sut/install-launchd.sh
   ```

3. Check it without changing anything:

   ```sh
   ./host/sut/sutctl.sh doctor
   ./host/sut/sutctl.sh watch
   ```

`watch` tests every open PR whose head owner is `agent-dev`, once per head SHA.
The launchd job invokes it every two minutes. Evidence lives only under the
gitignored `.task-sut/results/` directory (JSON result, controller log, and
worker Compose/test log) and as a compact Forgejo PR comment.

`doctor` also verifies the dedicated SUT token against both the repository and
PR-read APIs. Bootstrap mints that token with the additional `write:issue`
scope used solely to attach evidence comments; a failed comment remains
fail-closed and the PR SHA is not marked as tested.

For a manual reproduction from a clean checkout, pass the three node values as
process environment instead of creating an `.env` there:

```sh
NODE_DOMAIN=localhost NODE_CONFIG_REPO=operator/node-config SUT_FORGEJO_TOKEN=... \
  ./host/sut/sutctl.sh run 42 <head-sha>
```

To exercise the full Forgejo-comment loop for one `agent-dev` PR without
marking any other open PR as seen, use `SUT_PR=42 ./host/sut/sutctl.sh watch`.
To intentionally repeat an already-recorded head SHA—for example after fixing
the host SUT controller—add `SUT_FORCE=1`. This posts a new evidence comment;
normal launchd runs never repeat an unchanged head.

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

Some node services use locally-built app images rather than public registry
images. Their source is listed in the reviewed [source allowlist](sources.toml)
with an exact Forgejo ref. The host fetches only those allowed repositories,
strips their Git metadata, and transfers the source snapshots to the worker for
local builds. A PR cannot name an arbitrary private repository for the host to
clone.

`PASS` is test evidence, not approval. The operator still decides whether to
merge. The model-based reviewer is a later layer, after this deterministic gate
is proven reliable.

## Operating controls

```sh
./host/sut/sutctl.sh start                  # warm the one worker
./host/sut/sutctl.sh run 42 <head-sha>      # manually reproduce a PR result
./host/sut/sutctl.sh stop                   # release VM memory
launchctl unload ~/Library/LaunchAgents/node.sutwatch.plist
```

Start with one profile and one job at a time. A second worker is a separate,
operator-created profile and requires scheduler support; it must never be
created from PR input.
