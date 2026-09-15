# Assigned-issue dispatch — the doorbell + the host gate

Operator assigns a coordination issue to `agent-dev` (web UI, passkey) →
an ephemeral tenant spins up, works it, opens a node-config PR, and dies.
No agent touches docker; no agent can task another agent.

## The trust split (why this is safe)

```
  operator assigns issue ──▶ Forgejo emits `issues: assigned`
                                   │
                     ┌─────────────▼─────────────┐
                     │  DOORBELL (Actions runner) │   powerless: host-mode,
                     │  writes /spool/issue-N.nudge│   no socket, no secrets,
                     └─────────────┬─────────────┘   forgejo-only network
                                   │ (untrusted hint)
                     ┌─────────────▼─────────────┐
                     │  HOST DISPATCHER (launchd) │   ring-0: master key, socket
                     │  re-derives ALL trust:     │
                     │   • assignee == agent-dev  │
                     │   • NOT already in-progress│
                     │   • assigned BY OPERATOR    │  ← reads timeline actor
                     │  then run-task.sh --issue N │
                     └────────────────────────────┘
```

The doorbell is a **doorbell**: its only output is the integer N. The
dispatcher trusts none of it — it re-reads assignee, claim state, and the
**assigning actor** straight from the Forgejo API before anything launches.
So a forged marker, a compromised runner, or an agent self-assignment all
converge on the same outcome: the dispatcher looks, finds no operator
assignment, and refuses. The convenience layer can fail wide open without
moving the security boundary.

Ring-0 (master key, docker socket) lives ONLY in the host dispatcher +
`run-task.sh`, exactly as before. Actions adds a notification plane, not a
new trust surface.

## Residual blast radius (stated honestly)

- The runner can execute workflow YAML an agent pushed to coordination (agents
  hold `write`; Forgejo perms are single-level so we can't drop them to
  issues-only without breaking label writes). Contained by design: host-mode +
  no socket means the worst a hostile workflow does is write junk markers
  (rejected) and use its **repo-scoped job token** on coordination — mischief
  already inside any agent's existing authority, never ring-0.  Set the
  workflow `permissions:` to read-only to shrink even that.
- `scripts/*` becoming host execution on merge is a pre-existing property
  (the dispatcher itself is in the repo). Operator merge review is, as always,
  the gate for `scripts/` and `host/`.

## Install (operator, one time — all steps need host/root you hold)

1. **Label** (done): coordination has an `in-progress` label (the claim lock).
2. **Workflow**: copy `coordination-doorbell.yml` into the coordination repo at
   `.forgejo/workflows/dispatch-doorbell.yml` and commit. Protect that repo's
   default branch to operator-only so the *installed* copy is yours.
3. **Runner**: mint a **repo-scoped** registration token for coordination
   (Forgejo → coordination → Settings → Actions → Runners → Create), then:
   ```
   ./host/dispatch/register.sh <REPO_SCOPED_TOKEN>
   docker compose -f host/dispatch/runner.compose.yml up -d
   ```
   Never an instance/org token — that would let the runner serve node-config's
   workflows too.
4. **Dispatcher**: edit the paths in `node.dispatch.plist`, then
   ```
   cp host/dispatch/node.dispatch.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/node.dispatch.plist
   ```
   Set `DISPATCH_SPOOL` in the environment if you bind the spool to a host dir
   (lets the pass clear consumed markers).

## Difficulty tiers — model routing per issue

Every issue-work run carries a **difficulty estimate** (a label, not a model
name) that the dispatcher resolves to a model + budget. This keeps the model
choice as *policy* that can be retuned without changing labels or wiring.

### How it works

1. The operator applies a `difficulty:trivial|easy|moderate|hard` label to a
   coordination issue.
2. `dispatch-run.sh` reads the issue timeline, finds the label, and verifies
   the actor is the operator (agent self-labeling is **ignored** — same
   anti-escalation as assignment).
3. The label is resolved through `config/dispatch-tiers.yaml`:
   - `trivial`  → `deepseek-flash` @ $0.50
   - `easy`     → `deepseek-flash` @ $1.00
   - `moderate` → `claude-haiku`   @ $2.00 (this is the **default** — no label = moderate)
   - `hard`     → `claude-sonnet`  @ $4.00
4. Before launch, `dispatch-run.sh` checks that the resolved model is live in
   LiteLLM. If not, it falls back to `deepseek-flash` + a loud comment.
5. At PR time, `verify-config.sh` enforces that every tier model exists in
   `config/litellm.yaml` — catches table/LiteLLM drift before it can reach a run.

### One-time setup

The four `difficulty:*` labels are created automatically by `deploy.sh`
(via `scripts/ensure-tier-labels.sh`) on every deploy — no manual setup
needed. The script is safe to re-run: it checks for label existence via the
API before creating, avoiding the triplication problem (Forgejo does **not**
deduplicate label creation by name — see the `in-progress` note in
`task-dispatcher.sh`).

The operator's token must have `write` on coordination (the same scope the
agent holds).

### Test it

- `./scripts/task-dispatcher.sh --dry-run` — reports what it *would* launch,
  launches nothing. Assign an issue to agent-dev as the operator, run dry-run,
  confirm it says "would launch". Have a non-operator assign one, confirm it
  refuses with the actor mismatch.
- Apply a `difficulty:hard` label as the operator, run dry-run, confirm the
  dispatch log shows the resolved model and budget. Apply an agent label,
  confirm it's ignored.

## Tracing a dispatched run — the `trace` label

Want to see where an issue's runs spend their time (model wait vs. slow
tools)? Add the `trace` label, then assign the issue to agent-dev.

### How it works

1. **Order matters.** The doorbell fires on `assigned` and `unlabeled`, not
   `labeled`. Add `trace` first, then assign — or add it at any point and it
   applies from the next revision run (removing `in-progress`).
2. `dispatch-run.sh` honours `trace` only if the operator added it: the label
   must be current on the issue, and its latest add in the timeline must be by
   the operator — the same gate as `difficulty:*`. An agent labeling its own
   issue is logged and ignored: tracing grants no privilege, but it keeps the
   container until teardown and writes to the host's `traces/`.
3. It passes `--trace` to `run-task.sh` (the `AGENT_TRACE=1` path in
   docs/AGENT.md, "Tracing a run"). The run name includes the issue number:
   `task-issue-work-<N>-<timestamp>`.
4. When the run ends, `dispatch-run.sh` posts its usual completion comment,
   then renders the trace — waiting up to 5 minutes for LiteLLM's batched
   spend-log writes — and posts a second comment:

       **Run trace:** https://traces.<domain>/task-issue-work-52-20260915-101500/trace.html
       wall 13m25s · model wait 92.9% · unmeasured tools (inferred) 7.1% · …

   The summary names tools and durations only, never command text: every
   tenant can read coordination, and a command line holds whatever the model
   typed. Failed runs are traced and linked too.
5. The label stays on the issue, so every revision run is traced and linked.
   Remove it to stop.

### The traces.<domain> door

Caddy serves `./traces` (read-only mount) at `traces.<domain>`: **ring 0 +
passkey SSO + operator email**, the same door as copilot. Traces hold commands
and tool arguments from real runs. Only `/`, `/<run>/`, `trace.html` and
`trace.json` are served — never `forge.db` (the whole conversation) or the raw
jsonl. Agents can't reach it: tenants live on the `agents` network, Caddy
doesn't.

Merging wires it: `deploy.sh` re-runs `sso-setup.sh` on Caddyfile/compose
changes, which registers the `traces.<domain>/oauth2/callback` in Pocket ID;
oauth2-proxy's redirect allow-list names the host. On a real domain, add DNS
(or a tunnel route) for `traces.<domain>` like any other subdomain.

### Test it

- Add `trace` to an issue as the operator, assign it, and watch
  `.task-dispatch/` logs for `operator-applied label 'trace' -> tracing this run`,
  then for the `Run trace:` comment on the issue.
- Add `trace` as agent-dev (API) and confirm the log says it was ignored.
- Render by hand any time: `./scripts/trace-render.py traces/<run> --wait 300`.
