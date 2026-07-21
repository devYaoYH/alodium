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

Run `scripts/ensure-tier-labels.sh` once to create the four `difficulty:*`
labels in the coordination repo. The operator's token must have `write` on
coordination (the same scope the agent holds). Safe to re-run — Forgejo
deduplicates label creation by name.

### Test it

- `./scripts/task-dispatcher.sh --dry-run` — reports what it *would* launch,
  launches nothing. Assign an issue to agent-dev as the operator, run dry-run,
  confirm it says "would launch". Have a non-operator assign one, confirm it
  refuses with the actor mismatch.
- Apply a `difficulty:hard` label as the operator, run dry-run, confirm the
  dispatch log shows the resolved model and budget. Apply an agent label,
  confirm it's ignored.
