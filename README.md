# Alodium — Personal sovereign cloud assistant

Alodium is a personal cloud that works for you instead of extracting from you:
your data, work, and digital life stay on hardware you control, while an
assistant helps you pull in only what you choose and shape software around how
you actually live. It makes personal tools cheap to adapt or create, without
asking you to trade away ownership or trust an agent with the keys to your
house—security comes from explicit identity, scoped credentials, and hard
network boundaries.

> [!NOTE]
> An [alodium](https://www.merriam-webster.com/dictionary/alodium) is land held in absolute ownership:
> free of a superior lord, and freely saleable or inheritable. The name expresses
> this project’s premise—a personal cloud should be yours outright, not a tenancy
> contingent on a platform’s terms.

This is the **Tier 3 MVP** — the compute-box profile for people who already have a
domain and are comfortable with Docker. See `docs/PHILOSOPHY.md` for why this exists,
`docs/DESIGN.md` for how it fits together, and `docs/ROADMAP.md` for where it goes.

> [!TIP]
> **New here?** Start with the [Quickstart](#quickstart) below to stand up a node,
> then `docs/ONBOARDING.md` for the full operator-and-trusted-user journey (identity,
> the development plane, and the reviewed-change workflow). Curious how it was built?
> Jump to [Built with GPT‑5.6 & Codex](#built-with-gpt56--codex).

> [!NOTE]
> This project is a submission to the [OpenAI Build Week hackathon](https://openai.devpost.com/).
> The [Built with GPT‑5.6 & Codex](#built-with-gpt56--codex) section at the bottom
> describes, honestly, where the models fit in how it was made.

## What is here now

| Plane | Services | Purpose |
|---|---|---|
| Front door | Caddy, Pocket ID | TLS, trusted-network rings, and passkey-backed OIDC |
| Operator workspace | Homepage, Forgejo, LiteLLM | Home dashboard, change ledger, model budgets and request audit |
| Everyday apps | Radicale, Memos, Miniflux, Snake | Calendar/contacts, notes, feeds, and a small break |
| Assistant & agents | Open WebUI, `assistant`, `agent` | Front-door conversation plus a constrained development tenant |
| Controlled integrations | search-broker, gog-bridge | Audited Exa search and read-only Google bridge access |
| Node internals | Registry, private Postgres services, docker-proxy | Discovery, isolated state, and read-only dashboard health |

Ephemeral agent tenants (one container, one task, one budgeted key) run via
`scripts/run-task.sh` from briefs in `tasks/` — see docs/AGENT.md. Every tenant
gets its own scoped credentials, budget, workspace, and expiry.

## Everyday entry points

After setup, `https://home.<domain>` is the daily landing page: Home, Workshop,
Operations, and Security all share the assistant prompt bar. The most useful
doors are `chat.<domain>` for full conversations, `git.<domain>` for proposals
and mirrors, and `search.<domain>` for the Ring 0 agent-search audit. See
`docs/SEARCH.md` for the current search boundary and retention model.

## Quickstart

0. Or let the installer do 1–3 and validate the result (idempotent —
   also safe to re-run on an existing node as a config check):

       ./scripts/install.sh

1. Copy env and fill it in:

       cp .env.example .env
       # generate strong values:  openssl rand -hex 32

2. Point DNS at this box (or your front-door anchor):
   `home.yourdomain`, `chat.yourdomain`, `git.yourdomain`, `llm.yourdomain`,
   `cal.yourdomain`, `notes.yourdomain`, `feeds.yourdomain`, and
   `auth.yourdomain` → your public IP / VPS.
   No public exposure wanted? Leave DNS unset and use the `lan` Caddyfile variant.

3. Review `manifest/node.example.yaml` and copy it to `manifest/node.yaml`.
   The manifest records *where each concern lives*. The MVP reads it as
   documentation; the installer-agent (M1) will read it as instructions.

4. Bring it up:

       docker compose up -d
       docker compose --profile apps up -d     # calendar, notes, Snake, search
       docker compose --profile chat up -d     # Open WebUI
       docker compose --profile feeds up -d    # Miniflux

   Optional profiles are deliberately explicit: `bridge` enables the Google
   bridge, `authshim` enables forward-auth for apps that lack OIDC, and `agent`
   or `assistant` start the respective tenant runtime.

5. First-run:

       ./scripts/bootstrap-forgejo.sh

   One idempotent script, no web installer: operator admin (password into
   `.env` as break-glass), `agent-dev` user with a repo-scoped token, API
   tokens, and a private `node-config` repo with this directory's history
   pushed. From now on, config changes flow through git. Hand-edits on the
   box are considered migration debt.

6. Identity: initialize Pocket ID at `https://auth.yourdomain/setup` — enrolling
   your passkey IS the setup; there are no passwords. Then invite trusted users
   with `./scripts/invite.sh` (see docs/ONBOARDING.md).

7. Backups (not optional — this box is your identity):

       cp scripts/backup.env.example scripts/backup.env   # fill in restic/B2 creds
       ./scripts/backup.sh                                 # then cron it daily

8. Optional but the point of it all — the resident dev-agent (docs/AGENT.md):

       docker compose --profile agent build
       docker compose run --rm agent

## Layout

    docker-compose.yml      the stack — every image pinned by digest
    docker-compose.staging.yml  the one-file diff that makes the staging twin
    .env.example            secrets template (never commit .env)
    caddy/Caddyfile         routes, annotated by trust ring
    config/litellm.yaml     model list + router settings
    config/homepage/        daily dashboard, tabbed operations view, shared chat bar
    manifest/               placement manifest + app manifests (the contracts)
    registry/               the service registry: manifests -> one discovery endpoint
    agent/                  the dev-agent jail (Dockerfile + operating rules)
    tasks/                  ephemeral-tenant briefs (+ the injection-drill fixture)
    anchor/                 the disposable VPS front door (cloud-init, WG, CoreDNS)
    templates/app-skeleton/ the bare-minimum service every new app starts from
    .agents/skills/         the node's working procedures for resident tenants
    scripts/install.sh      the interview: manifest, reachability, validation
    scripts/backup.sh       restic backup; include list generated from manifests
    scripts/mirror.sh       cache an upstream repo in Forgejo (docs/MIRRORING.md)
    scripts/new-app.sh      seed apps/<name> in Forgejo from the skeleton
    scripts/pin-images.sh   re-pin compose images to current digests
    scripts/staging.sh      the staging twin: same stack, throwaway volumes
    scripts/run-tests.sh    manifest-declared tests against staging
    scripts/promote.sh      staging -> tests -> prod; refuses promotion on red
    scripts/deploy.sh       the deterministic deploy step (promote's last move)
    scripts/run-task.sh     ephemeral agent tenancy: per-run key, one task, teardown
    scripts/task-dispatcher.sh  cron: executes agents' task-request issues (tracked briefs only)
    scripts/drill-injection.sh  prove a prompt-injection cannot escalate
    docs/                   PHILOSOPHY, DESIGN, ROADMAP, ONBOARDING, MIRRORING, AGENT

## Configuration layering — what's tracked vs. what's yours

This repository is a public template: everything tracked in git is generic
to any deployment. Everything particular to YOUR node lives in files git
never sees:

| Local-only (gitignored)   | Holds                                          |
|---------------------------|------------------------------------------------|
| `.env`                    | your domain, email, every secret and minted key |
| `manifest/node.yaml`      | your placement manifest (copied from the example) |
| `caddy/local/*.caddy`     | your extra routes/snippets, auto-imported by the Caddyfile |
| `scripts/backup.env`      | restic repo + storage credentials              |

Tracked config files reference the local layer only through `${VARS}` and
the `import local/*.caddy` glob — if you find yourself typing your domain
or an IP range into a tracked file, stop: it goes in `.env`
(`EXTRA_TRUSTED_RANGES` covers unusual LAN addressing) or `caddy/local/`.

## Non-negotiables

- `.env` and `scripts/backup.env` never enter git.
- The box accepts no inbound connections except through Caddy (and Forgejo SSH if
  you enable it deliberately).
- Agents get LiteLLM *virtual* keys — never provider keys — and no deploy path.
- Internal calls are deny-by-default: apps receive only the scoped credentials
  their manifest declares (see manifest/app.example.toml).
- Search is a capability, not internet access: the Exa key stays behind the
  search egress companion; agents receive only a revocable broker token.

## Built with GPT‑5.6 & Codex

**Where Codex/GPT‑5.6 fits — honestly:** Codex is the reviewing agent in my
development loop. The resident **agent-dev** drafts a pull request; I bring Codex
in to review that PR and leave feedback; agent-dev iterates on that feedback in
the next pass; then a human merges. Codex is a real second set of eyes in how
this got built — and, by design, it is *not* wired in as an automated gate inside
the node, because doing so would put an LLM back in the authorization path and
break the entire thesis. So the division is deliberate: **Codex reviews, humans
authorize, deterministic code enforces.** (Closing that review-and-iterate loop
into a tracked automation is what I'm building next.)
