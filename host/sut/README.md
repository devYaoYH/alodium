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

1. Bring up the node and run `./scripts/bootstrap-forgejo.sh`; this supplies
   the local-only `FORGEJO_TOKEN`, `NODE_DOMAIN`, and `NODE_CONFIG_REPO` used by
   the watcher.
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
gitignored `.task-sut/results/` directory and as a compact Forgejo PR comment.

## What a test does

For each PR head, the controller clones the exact SHA on the host, strips
`.git`, `.env`, `secrets/`, and host state, transfers the remaining tree over
the VM's SSH channel, then runs a trusted worker helper. That helper creates
synthetic configuration, validates the candidate Compose graph, starts the
staging overlay using throwaway volumes, and executes manifest-declared smoke
tests. It captures output, tears the stack down, deletes the candidate tree,
and returns a small JSON result.

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
