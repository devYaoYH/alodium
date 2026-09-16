# The resident dev-agent

The first tenant of the agent runtime slot: Claude Code running in a jailed
container **inside the node**, which you talk to in a terminal, whose
inference routes through LiteLLM on a budgeted virtual key, and whose only
write path to the node's configuration is a pull request you approve. It
develops the node from within — but it cannot deploy, cannot see secrets,
and cannot spend without a ceiling.

## How it operates, end to end

    you ──(terminal)── agent jail ──(virtual key)── LiteLLM ──> model provider
                          │
                          ├─(scoped token)── Forgejo: clone/branch/push/PR
                          │                     │  operator merges (the approval)
                          │                     ▼
                          └─ NO deploy access   host deploy step pulls main,
                                                `docker compose up -d`

The loop, named by DESIGN.md's policy section: **the agent proposes; git
records; deterministic code enforces; the human approves.** Concretely:

1. You start a session:  `docker compose run --rm agent`  (drops you into
   Claude Code, workspace = a clone of `node-config`).
2. You talk. The agent edits its clone — compose changes, Caddy routes,
   app manifests, migration scripts.
3. It pushes a branch and opens a PR in Forgejo (its Forgejo token allows
   exactly that).
4. You review the diff in Forgejo and merge. That merge IS the
   authorization moment.
5. Deploy is a separate, deterministic step the agent cannot trigger: the
   host deploy watcher (`host/deploy-watch/`) notices the merge within ~2
   minutes and runs `scripts/deploy.sh` — the same script you can still run
   by hand. The M2 change pipeline adds staging + tests before promote.

## Credentials at bring-up (and what it never holds)

Minted per the manifest `needs` pattern — each one individually revocable:

| Credential | Scope | Kill switch |
|---|---|---|
| LiteLLM virtual key | `claude-sonnet` + `claude-haiku`, monthly budget (start: $25), rate-limited | `/key/delete` on LiteLLM |
| Forgejo token | read/write on `node-config` + `apps/*`, read on `mirrors/*`; no admin scope | revoke in Forgejo settings |
| `AGENT_SEARCH_TOKEN` | audited Exa search via `search-broker` only (docs/SEARCH.md); never an Exa key | rotate in `.env`, `scripts/search-setup.sh` remints |
| Workspace volume | its own named volume; nothing else mounted | `docker volume rm` |

Never, structurally (not policy — absence):

- **No provider API keys** — it sees `ANTHROPIC_BASE_URL=http://litellm:4000`
  and a virtual key; the real key lives in LiteLLM's encrypted store.
- **No `LITELLM_MASTER_KEY`, no `.env`, no vault** — not mounted, not in env.
- **No docker socket** — it cannot start, stop, or inspect containers.
- **No host filesystem** — only its workspace volume.
- **Network: the `agents` spur only** — a dedicated network whose only other
  members are the services its manifest `needs` declared: LiteLLM (inference),
  Forgejo (git), and `search-broker` (audited web search, gated by its own
  revocable token — see "Web search" below). It is not on `edge`; radicale,
  homepage, and the IdP are unreachable at the wire level, and databases are
  on networks it isn't in.

One harness default is switched off by config rather than absence: forge
uploads every file it edits to its `services_url` (api.forgecode.dev by
default) for a remote syntax check. The jail can't reach it — every edit used
to stall ~5s on DNS instead — and `agent/.forge.toml` now points it at a
loopback port that refuses instantly, so edited files never leave the
container. `scripts/test-jail-image.sh` fails if edits start waiting on it
again.

Compromise analysis: a fully hostile agent (prompt-injected via a mirrored
repo's README, say) can burn its LLM budget and open ugly PRs. It cannot
merge them, deploy anything, read a secret, or touch another service's data.

## Inference routing

Claude Code speaks the Anthropic API natively; LiteLLM exposes an
Anthropic-compatible `/v1/messages` endpoint. So the jail just sets:

    ANTHROPIC_BASE_URL=http://litellm:4000
    ANTHROPIC_AUTH_TOKEN=<virtual key>           # minted for this tenant
    ANTHROPIC_MODEL=claude-sonnet                # LiteLLM alias, not a real id

Every call is logged, budgeted, and attributable to the agent's key. Local
inference later (Tier 4) means repointing the LiteLLM alias — the agent
never knows.

## Web search

The jail's third `agents`-network peer is `search-broker` (docs/SEARCH.md):
an audited, revocable path to Exa search that never hands the agent an Exa
key. `AGENT_SEARCH_TOKEN` in its env authorizes `POST
http://search-broker:8080/v1/search`; the broker records a durable audit
row (query hash, caller, Exa request ID, result snapshot) before calling
Exa, and the real `EXA_API_KEY` lives only in a separate egress process the
jail cannot reach. `agent/AGENTS.md` documents the call for the harness
itself, so this isn't a capability the operator has to explain by hand each
session.

## Self-modification, precisely bounded

"The agent develops the node" means: everything in `node-config` is fair
game to *propose* — including its own service definition, its own
AGENTS.md operating instructions, even this file. The boundary is that
every self-modification travels the same PR path as any other change, and
credential escalation is structurally outside its reach: budgets, token
scopes, and mounts are set on the host side of the merge boundary.
Widening its own jail requires a diff you read and merge with your own
eyes. That property — legible self-modification — is the entire design.

## Worked example: the Keep-clone (Memos)

The first real task, exercising every mechanism above:

1. **Cache upstream:**  `./scripts/mirror.sh https://github.com/usememos/memos`
2. **Agent session:** ask it to add Memos. It writes `manifest/memos.toml`
   (ring 1, volume `memos-data`, backup declared, no LLM needs), a pinned
   compose service, and a `notes.<domain>` Caddy route behind the
   forward-auth snippet; opens the PR.
3. **You merge; deploy runs.** Memos is up, behind your passkey.
4. **Data migration:** export Google Keep via Takeout, drop the archive
   into the agent's workspace. It writes a converter (Takeout JSON →
   Memos API), runs it against `http://memos:5230` with a Memos API token
   you mint for it, shows you the count, deletes the archive.
5. `backup.sh` picks up `memos-data` from the manifest. Restore drill
   covers your notes from then on.

## Interaction surface, staged

- **Resident (M0.5):** terminal — `docker compose run --rm agent`. You're on
  the node (or SSH'd in); the session is the operator ring by definition.
  Persistent workspace, because the job is a continuing conversation about
  the node.
- **Ephemeral (M3, here):** ambient tasks as one-shot tenants —
  `./scripts/run-task.sh tasks/<brief>.md`. One container per task, per-run
  virtual key with budget and expiry, no workspace volume at all, state in
  git artifacts only (a `digest`/`handoff` issue in the coordination repo —
  see skills/coordination). The first ambient task is the morning digest
  (`tasks/morning-digest.md`, cron it); the injection drill
  (`scripts/drill-injection.sh`) exercises hostile instructions, while the
  deterministic boundary drill (`scripts/drill-boundary-access.sh`) executes
  real filesystem and network probes inside an ephemeral jail. Neither uses
  production personal data; synthetic-data canary testing belongs in the
  isolated SUT lane.
  Tenants coordinate through Forgejo issues, never through shared memory.
  Agents can *request* ephemeral runs without touching docker: a
  `task-request` issue naming a tracked `dispatch: auto` brief, executed
  by the host-side cron `scripts/task-dispatcher.sh` (skills/request-task).
- **Conversational (the front door):** `docker compose run --rm assistant`
  — a second resident tenant with a deliberately weaker hand
  (agent/ASSISTANT.md): read on node-config, write on issues, its own
  budgeted key, no PR path. You ask it for things; work needing changes
  becomes a `handoff` issue the dev-agent picks up. Division of labor:
  the assistant converses, agent-dev builds, ephemeral tenants run
  errands — all meeting in the coordination repo.
- **Later:** a chat bridge behind the node IdP (Pocket ID) putting the
  assistant on your phone, still on virtual keys and read-only surfaces.
- **Never:** an LLM in the request-authorization path. Unauthenticated
  internet traffic must not be able to talk its way in.

## Tracing a run

Where does an ephemeral run's wall-clock go — waiting on the model, or in a
slow tool? Opt in per run, then render:

    AGENT_TRACE=1 ./scripts/run-task.sh tasks/<brief>.md
    ./scripts/trace-render.py traces/<run>      # writes trace.html + trace.json

`run-task.sh` keeps the stopped container just long enough to copy
`/tmp/trace` and forge's conversation store into `traces/<run>/`, then
removes it. The renderer joins the sources on the run name (= key alias):

| Lane | Source | Timing |
|---|---|---|
| Model requests | LiteLLM spend logs (`session_id = key:<run>`): start, first token, end, tokens, cost | measured |
| Shell tools | `agent/trace-sh.py` as forge's `$SHELL`: wall time, exit, CPU, peak RSS, block IO | measured |
| Built-in tools (`read`, `write`, `patch`, `fs_search`, …) | `agent/ui-trace.py` runs forge under a pty and timestamps the status line forge prints as each tool starts (`ui.jsonl`); a tool ends at the next status line or model request | measured start |
| Unmeasured tools: `task` sub-agents (no status line), untraced shell calls | the gap before the next model request | inferred |
| Container setup | container start + entrypoint marks (`events.jsonl`) | measured |

The jail does not widen: no capability, mount, or network is added, and the
host pulls the trace out after exit. Forge only for now — Claude Code's route
is OpenTelemetry, not wired yet. The shim adds ~9 ms of startup per shell
command, excluded from recorded durations. Traces contain commands, tool
arguments and forge's status lines (file paths, commands) from real runs; `traces/` is gitignored and 0700 — treat it like the
spend logs. LiteLLM writes spend logs in batches, so a render straight after a
run can miss the model lane; pass `--wait 300`, or re-render a minute later.
Offline (tests, mock models), pass `--requests-json <file>` instead of querying
LiteLLM.

**Dispatched issues:** add the `trace` label to a coordination issue (as the
operator) before assigning it to agent-dev. Every run of that issue is then
traced, and `dispatch-run.sh` comments a link —
`https://traces.<domain>/<run>/trace.html` — with a short summary. That door
is ring 0 + passkey + operator email, and serves only `trace.html` /
`trace.json`. Details: host/dispatch/README.md, "Tracing a dispatched run".

## Bring-up

The jail is three files in `agent/` (Dockerfile, entrypoint, AGENTS.md
operating instructions, .gitignore for the workspace) plus a compose
service under `profiles: [agent]`. Steps:

1. Put a provider key in `.env` (LiteLLM needs at least one upstream).
2. Mint the agent's virtual key:
   `curl https://llm.<domain>/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" -d '{"key_alias":"agent-dev","models":["claude-sonnet","claude-haiku"],"max_budget":25}'`
   → put the result in `.env` as `AGENT_LLM_KEY`.
3. `./scripts/bootstrap-forgejo.sh` (if not already run) — creates the
   `agent-dev` user and writes `AGENT_FORGEJO_TOKEN` + `NODE_CONFIG_REPO`
   into `.env`.
4. `docker compose --profile agent build && docker compose run --rm agent`
