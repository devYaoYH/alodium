# Auto-deploy — merge is the button

The operator's flow used to be "merge the PR in Forgejo, then run
`./scripts/deploy.sh` on the host." The second step is now automatic:
a launchd heartbeat runs `scripts/deploy-watch.sh` every 2 minutes; when
`forgejo/main` is ahead of the checkout, it runs the exact same
`scripts/deploy.sh` the operator would have. Merge = authorize AND apply.

## Why a poll, not a push

- **No Actions runner on node-config.** Agents push PR branches (and thus
  workflow files) to that repo; a runner there would execute agent-authored
  YAML. The dispatch runner is deliberately scoped to coordination only
  (`host/dispatch/README.md`) — this keeps it that way.
- **No webhook listener.** An inbound HTTP endpoint on the host is a new
  always-on attack surface. Forgejo is on this same host; polling it every
  2 minutes costs nothing and moves no trust boundary.

## What it will and won't do

- Deploys only from a **clean checkout parked on `main`** — on a branch or
  with tracked edits it skips (you're mid-work) and catches up later.
- Divergence between local and forgejo main deploys **nothing** and files a
  `blocked` coordination issue (fast-forward-only, same as deploy.sh).
- A failed deploy files **one** `blocked` issue per merged tip (stamp in
  `.task-dispatch/deploy-fail-<sha>`), then waits for a fix or new commits —
  no two-minute spam. Log: `.task-dispatch/deploy-watch.log`.
- `deploy.sh`'s GitHub mirror push may warn under launchd (no ssh agent);
  it already continues past that, and `sync-node-config.sh` reconciles.

## Install

Done by `./scripts/up.sh` (step 7). By hand on darwin:

    ./host/deploy-watch/install-launchd.sh

Elsewhere, cron:

    */2 * * * *  cd /path/to/node && ./scripts/deploy-watch.sh >> .task-dispatch/deploy-watch.log 2>&1

Disable: `ENABLE_AUTODEPLOY=0` in the environment before running up.sh, or
`launchctl unload ~/Library/LaunchAgents/node.deploywatch.plist`.

## Test it

    ./scripts/deploy-watch.sh --dry-run   # reports what it would do, runs nothing

Merge a trivial PR and watch `.task-dispatch/deploy-watch.log` — the deploy
should appear within 2 minutes.
