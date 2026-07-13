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
| agent     | Claude Code in a jail — the resident dev-agent (optional profile, see docs/AGENT.md) | ring 0 session |

Everything else (gog/gws Google bridge, feeds, photos, more agent tenants)
arrives in later milestones — see the roadmap.

## Quickstart

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

5. First-run: create your Forgejo admin at `https://git.yourdomain`, then create a
   repo named `node-config` and push this directory to it. From now on, config
   changes flow through git. Hand-edits on the box are considered migration debt.

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
    .env.example            secrets template (never commit .env)
    caddy/Caddyfile         routes, annotated by trust ring
    config/litellm.yaml     model list + router settings
    config/homepage/        trusted-people dashboard (config-as-code)
    manifest/               placement manifest + app manifest v0 (the contracts)
    agent/                  the dev-agent jail (Dockerfile + operating rules)
    scripts/backup.sh       restic volume backup
    scripts/mirror.sh       cache an upstream repo in Forgejo (docs/MIRRORING.md)
    scripts/pin-images.sh   re-pin compose images to current digests
    scripts/deploy.sh       the deterministic deploy step (post-merge)
    docs/                   PHILOSOPHY, DESIGN, ROADMAP, ONBOARDING, MIRRORING, AGENT

## Non-negotiables

- `.env` and `scripts/backup.env` never enter git.
- The box accepts no inbound connections except through Caddy (and Forgejo SSH if
  you enable it deliberately).
- Agents, when they arrive, get LiteLLM *virtual* keys — never provider keys.
- Internal calls are deny-by-default: apps receive only the scoped credentials
  their manifest declares (see manifest/app.example.toml).
