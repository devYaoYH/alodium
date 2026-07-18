# sovereign-node

A personal sovereign cloud: self-hosted identity, services, and (eventually) agents,
running on hardware you own, behind a front door you control.

This is the **Tier 3 MVP** — the compute-box profile for people who already have a
domain and are comfortable with Docker. See `docs/PHILOSOPHY.md` for why this exists,
`docs/DESIGN.md` for how it fits together, and `docs/ROADMAP.md` for where it goes.

## What's in the box (MVP)

| Service   | Role                                        | Ring    |
|-----------|---------------------------------------------|---------|
| Caddy     | Reverse proxy, TLS, the only exposed thing  | door    |
| Pocket ID | Identity provider — passkey-only OIDC, single container | door / ring 0 admin |
| LiteLLM   | LLM gateway — virtual keys, budgets, audit  | ring 0  |
| Postgres  | LiteLLM's database (isolated network)       | ring 0  |
| Forgejo   | Git platform, config-as-code source of truth, upstream mirror cache | ring 1 |
| Homepage  | Trusted-people dashboard, health checks, config in git | ring 1 |
| Radicale  | CalDAV/CardDAV calendar + contacts (optional profile) | ring 1 |
| Registry  | Service registry: what exists here, what can I call — discovery for humans and agents | ring 0 |
| Miniflux  | Pull-based feeds, the algorithm-free timeline (profile `feeds`) | ring 1 |
| gog-bridge| Google mail/cal/drive as read-only MCP + local mirror (profile `bridge`, agents spur only) | ring 0 |
| agent     | Claude Code in a jail — the resident dev-agent (profile `agent`, see docs/AGENT.md) | ring 0 session |

Ephemeral agent tenants (one container, one task, one budgeted key) run via
`scripts/run-task.sh` from briefs in `tasks/` — see docs/AGENT.md. Photos,
memos, and more tenants arrive in later milestones — see the roadmap.

## Quickstart

0. Or let the installer do 1–3 and validate the result (idempotent —
   also safe to re-run on an existing node as a config check):

       ./scripts/install.sh

1. Copy env and fill it in:

       cp .env.example .env
       # generate strong values:  openssl rand -hex 32

2. Point DNS at this box (or your front-door anchor):
   `git.yourdomain`, `llm.yourdomain`, `cal.yourdomain`, `auth.yourdomain`,
   `home.yourdomain` → your public IP / VPS.
   No public exposure wanted? Leave DNS unset and use the `lan` Caddyfile variant.

3. Review `manifest/node.example.yaml` and copy it to `manifest/node.yaml`.
   The manifest records *where each concern lives*. The MVP reads it as
   documentation; the installer-agent (M1) will read it as instructions.

4. Bring it up:

       docker compose up -d
       docker compose --profile apps up -d     # include Radicale

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
    config/homepage/        trusted-people dashboard (config-as-code)
    manifest/               placement manifest + app manifests (the contracts)
    registry/               the service registry: manifests -> one discovery endpoint
    agent/                  the dev-agent jail (Dockerfile + operating rules)
    tasks/                  ephemeral-tenant briefs (+ the injection-drill fixture)
    anchor/                 the disposable VPS front door (cloud-init, WG, CoreDNS)
    templates/app-skeleton/ the bare-minimum service every new app starts from
    skills/                 the agent skill library — procedures for working this node
                            (framework-agnostic; .claude/skills symlinks here)
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
- Agents, when they arrive, get LiteLLM *virtual* keys — never provider keys.
- Internal calls are deny-by-default: apps receive only the scoped credentials
  their manifest declares (see manifest/app.example.toml).
