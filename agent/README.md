# The agent jail — structure

One image, two harnesses, four ways to start it. This file is the layout; the
contract it implements (what the agent may do, what it structurally cannot)
is docs/AGENT.md, and the dispatch path is host/dispatch/README.md.

## Files here

| File | Role |
|---|---|
| `Dockerfile` | the image: harnesses, offline validators, the operating contract |
| `entrypoint.sh` | per-session setup, then `exec` into the chosen harness |
| `AGENTS.md` | the dev-agent's operating contract (forge reads it as `AGENTS.md`, Claude Code as user memory) |
| `ASSISTANT.md` | the conversational tenant's weaker contract; compose mounts it over the dev one |
| `.forge.toml` | forge's global config, shipped at `~/forge/.forge.toml` |
| `trace-sh.py` | `$SHELL` shim: times every shell tool call when `AGENT_TRACE=1` |
| `ui-trace.py` | pty wrapper: timestamps forge's per-tool status lines when `AGENT_TRACE=1` |
| `forgejo.py` | stdlib CLI for the coordination + node-config Forgejo API; the default way agents read the board, file notes, label issues, and open PRs. Reads `AGENT_FORGEJO_TOKEN` from env, never from argv |
| `test_forgejo.py` | offline unit tests for `forgejo.py` — body-file round-trips, error paths, mock HTTP; run from the repo root, no docker required |

## What is in the image

- **Base** `node:22-slim`, plus `git curl ca-certificates python3 python3-yaml ripgrep jq shellcheck`.
- **Harnesses**, both installed globally at build time (the jail has no internet):
  `forgecode` (pinned, the default) and `@anthropic-ai/claude-code` (the backup).
- **The same `caddy` binary prod runs** (digest-pinned, copied from the caddy image), so
  the agent can `caddy validate` a Caddyfile offline before pushing.
- **The contract**: `AGENTS.md` lands at `~/AGENTS.md` and `~/.claude/CLAUDE.md`.
- **`~/forge`** is created *and chowned* to `agent` before `.forge.toml` is copied in.
  It is forge's writable state dir; root-owned, forge blocks forever with no output.
- **User** `agent` (uid 10001), `WORKDIR /workspace`, `ENTRYPOINT entrypoint.sh`.

## `.forge.toml`

Every key is top-level: forge hard-fails on malformed TOML but silently ignores
an unknown key — or a real key nested in a table it does not use.

- `max_requests_per_turn` — raised from forge's default of 100 so long autonomous
  turns do not hit the cap mid-task.
- `services_url` — pointed at a loopback port with no listener. By default forge
  uploads **the full contents of every file it edits** to `api.forgecode.dev` for a
  remote syntax check; the jail cannot reach it, so each edit used to stall ~5s on
  DNS. Refusing instantly costs ~9 ms and keeps edited files inside the container.

## Startup (`entrypoint.sh`)

1. Marks `entrypoint_start` (tracing only, see below).
2. With `AGENT_FORGEJO_TOKEN`: writes git credentials for `forgejo:3000` as
   `$AGENT_GIT_USER`, and clones `$NODE_CONFIG_REPO` into `/workspace/node-config`
   if it is not there yet. Without it: read-only sandbox, no PR path.
3. `cd` into the checkout, symlink `~/AGENTS.md` in (untracked, via `.git/info/exclude`);
   marks `workspace_ready`.
4. Translates `AGENT_MODEL` into each harness's own variables
   (`FORGE_SESSION__PROVIDER_ID` / `FORGE_SESSION__MODEL_ID`, `ANTHROPIC_MODEL`).
5. Marks `harness_exec`, then `exec`s the harness — through `ui-trace` when tracing
   a forge run, directly otherwise.

Forge runs as a small node launcher that execs the platform binary, so inside a
running container the chain is:

```
[ui-trace →] node forge.js → forge-<arch>-unknown-linux-musl → [trace-sh →] /bin/sh -c <tool command>
```

## Environment

| Variable | Meaning |
|---|---|
| `AGENT_HARNESS` | `forge` (default) or `claude` |
| `AGENT_MODEL` | LiteLLM alias; the key's allowlist is the authority |
| `AGENT_TRACE` | `1` turns on wall-clock tracing (below); off by default |
| `OPENAI_URL` / `OPENAI_API_KEY` | forge → LiteLLM, with the tenant's virtual key |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | same for Claude Code |
| `AGENT_FORGEJO_TOKEN`, `AGENT_GIT_USER` | git identity and scope |
| `NODE_CONFIG_REPO`, `COORDINATION_REPO` | what to clone, where to file notes |
| `AGENT_SEARCH_TOKEN` | revocable bearer for `http://search-broker:8080/v1/search`; never an Exa key (docs/SEARCH.md) |

Never present: provider API keys, `LITELLM_MASTER_KEY`, `.env`, the docker socket.

## How it is started

| Way | Command | Shape |
|---|---|---|
| Resident dev-agent | `docker compose run --rm agent` | interactive (`stdin_open` + `tty`), persistent `agent_workspace` volume |
| Conversational tenant | `docker compose run --rm assistant` | same image, `ASSISTANT.md` mounted over the contract, own key and token |
| Ephemeral task | `./scripts/run-task.sh tasks/<brief>.md` | per-run virtual key, **no** workspace volume, `--rm`, `-t` but no `-i` |
| Dispatched issue | operator assigns the issue; `dispatch-run.sh` → `run-task.sh --issue N` | as above, plus tier model/budget and the completion comment |

All of them join only the internal `agents` network: LiteLLM and Forgejo are the
only listeners, and there is no route to the internet.

## Tracing (`AGENT_TRACE=1`)

Off unless asked for, per run: `AGENT_TRACE=1 ./scripts/run-task.sh …`, or the
operator-applied `trace` label on a dispatched issue. Forge only; Claude Code runs
record the entrypoint phases alone. Three files land in `/tmp/trace/`:

| File | Written by | Holds |
|---|---|---|
| `events.jsonl` | `entrypoint.sh` | container start, workspace clone, harness exec |
| `tools.jsonl` | `trace-sh.py` (as `$SHELL`) | one record per shell command: wall time, exit status, CPU, peak RSS, block IO |
| `ui.jsonl` | `ui-trace.py` | the moment forge printed each built-in tool's status line (`Read …`, `Create …`, `Replace …`) |

`ui-trace` gives forge its own pty, relays every byte through unchanged, preserves
exit status and signals, and only takes over the terminal when its process group
owns it — a background group touching the tty is stopped by SIGTTOU, which would
leave forge's output unread. With `FORGE_UI_TRACE` unset it is a plain `exec`.

`run-task.sh` keeps the stopped container just long enough to copy `/tmp/trace` and
forge's conversation db into `traces/<run>/`, then removes it.
`scripts/trace-render.py` turns that into `trace.html`: shell tools measured,
built-in tools with a measured start (ending at the next status line or model
request), and anything without a status line — `task` sub-agents — left inferred.
The ring-0 `traces.<domain>` door serves the result. Details: docs/AGENT.md.

Trace files hold commands, tool arguments and file paths: `traces/` is gitignored,
0700, operator-only.

## Looking inside a live run

`docker exec -it <container> sh` gives a shell as `agent` in the same container:
the workspace, the process chain and `/tmp/trace/*.jsonl` as they are being written.

You cannot type at the harness of a dispatched run: `run-task.sh` starts it with
`-t` and no `-i`, so no stdin is attached (`docker attach` shows output with
nowhere to type). The resident session is the interactive one.

## Building and testing

```
docker compose --profile agent build          # or: docker build -t sovereign-node/agent:local ./agent
./scripts/test-jail-image.sh                  # builds :test, then exercises the real image
```

`test-jail-image.sh` is behavioural, not textual: every path under `$HOME` is
agent-writable; `.forge.toml` parses *and* forge recognises every key in it; forge
boots and reaches a provider instead of hanging; edits do not wait on
`services_url`; Claude Code runs; the shell shim records exactly one entry per
command and stays invisible when off; and a scripted read → write → shell run
(plain and under `docker run -t`) lands in `ui.jsonl` in order, each record
matching the clock forge printed.

On deploy, `deploy.sh` rebuilds the image as `:candidate`, runs that script against
it, and only then tags `sovereign-node/agent:local`.
