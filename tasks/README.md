# Task briefs — everything an ephemeral tenant gets

An ephemeral tenant starts clean and dies clean (DESIGN.md, "Ephemeral
tenancy"). Whatever a task needs arrives as files; this directory holds
those files. A brief is the *entire* inheritance of a run: no transcript,
no workspace, no memory survives from any previous tenant.

## Format

Markdown with a small frontmatter block read by `scripts/run-task.sh`:

    ---
    task: morning-digest          # names the run + the key alias
    model: claude-haiku           # LiteLLM alias; ambient tasks stay cheap
    harness: claude               # optional: claude (default) | forge
    budget_usd: 0.50              # hard ceiling on the per-run virtual key
    expires: 2h                   # key self-destructs even if teardown fails
    env: [MINIFLUX_TASK_TOKEN]    # .env names passed through — the brief's
    dispatch: auto                #   `needs`: declare nothing, receive nothing
    ---                           # dispatch: auto = agents may request runs
    <the prompt body: what to do, where to file the artifact>

`dispatch: auto` opts a brief into agent-requested execution: an agent
files a `task-request` issue titled `run: <name>`, and the host-side
cron (`scripts/task-dispatcher.sh`) validates and runs it. The flag
ships through PR review like everything else — a brief without it can
only be run by the operator's own hand. The dispatcher never reads the
issue body: the brief is the entire prompt, so a request can only ever
trigger reviewed work, never inject new work.

## Rules for writing briefs

- **The artifact is the deliverable.** Every brief ends by filing something
  reviewable — normally a `digest`/`handoff` issue in `$COORDINATION_REPO`
  (see skills/coordination). A run whose output only existed in stdout
  did not happen.
- **Declare reads, never writes.** `env:` tokens should be read-only
  (a Miniflux read token, a bridge read scope). Write scopes belong to
  manifests and PRs; destructive operations belong to humans. A brief
  that needs a write scope is a design smell — file it as `blocked`.
- **Quote external content as data.** Briefs that ingest mail/feeds must
  instruct the tenant to wrap quoted material in fenced `untrusted`
  blocks and to treat instructions found inside content as content.
  The drill (`scripts/drill-injection.sh`) tests exactly this.
- Budgets are real: default $0.50, and `claude-haiku` unless there is a
  stated reason. An ambient task that needs $5 of Sonnet is not ambient.
