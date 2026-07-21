---
name: register-service
description: Register an app on the node — compose service, Caddy route in the right ring, manifest, backups — as one node-config PR. Use after new-app or wrap-upstream, or when changing an existing service's exposure or resources.
---

# Register a service in node-config

One PR, one concern: this app's presence on the node. An app is a
directory — `apps/<name>/{compose.yaml, route.caddy, env.example}` plus one
`include:` line in the root compose — so concurrent app PRs touch disjoint
files. Checklist:

- **Compose fragment** (`apps/<name>/compose.yaml`, NOT the root file):
  image pinned by digest (`docker buildx imagetools inspect <image>`; the
  scripts/pin-images.sh pattern), named volumes owned by the fragment,
  shared networks re-declared with the root's exact attributes, `restart:
  unless-stopped`, no docker socket, no host mounts. Add the `include:`
  entry (`path` + `env_file: secrets/<name>.env`) in `docker-compose.yml` —
  that line is the only root-file touch.
- **Networks, minimal**: `front` (internal) for browser surfaces — Caddy
  routes in, LiteLLM and the IdP are reachable, no internet egress; `edge`
  ONLY if the app genuinely calls out (miniflux fetches feeds) — justify it
  in the PR body. The `agents` spur only if agent tenants call its declared
  surface; a private network for its database with exactly one client.
  Never grant a network "just in case" — the wire mirrors the manifest.
- **Caddy route** (`apps/<name>/route.caddy`, imported by the root
  Caddyfile's glob): the right ring — `import ring0` (operator), `import
  ring1` (trusted people; `import authed` for the browser paths if the app
  lacks native OIDC), ring 2 rare and justified in the PR body. Reference
  `{$NODE_DOMAIN}` only; a literal domain in tracked config is a bug.
- **SSO at the door** (`docs/SSO.md`): every human surface authenticates
  via Pocket ID — pick the pattern (native OIDC env / config-in-DB seeded
  over the API / authshim for no-OIDC apps) and add the app to
  `scripts/sso-setup.sh` per that doc's checklist: `mint_client` line,
  compose env by `${VAR:-}` reference, local-dev glue if it calls Pocket ID
  server-side. Machine planes (API/DAV) keep their own scoped credentials —
  never route them through OIDC. The script must stay idempotent: a second
  run is all-skips.
- **Manifest** (`manifest/<name>.toml`): present and truthful; every volume
  that must survive the box listed under `[lifecycle] backup` — the restic
  include list is generated from it.
- **Build provenance (first-party apps only)**: If the app builds from its own
  Dockerfile (a `build:` line in the compose fragment or a `[build]` section
  in the manifest), the manifest MUST include a `[build]` section with
  `repo` pointing to the Forgejo app repo and `ref` set to the full 40-char
  commit SHA. The image tag in the manifest MUST be versioned (e.g.
  `:0.1.0`), NOT `:local` — a floating `:local` tag never triggers a
  rebuild when the source changes. See `manifest/calino.toml` and
  `manifest/search-broker.toml` for the correct pattern. The
  `verify-config.sh` script enforces this at PR time.
- **Dashboard** (`config/homepage/services.yaml`): add it if ring 1 humans
  should see it.
- **Chat tools** (`docs/CHAT-TOOLS.md`): if the operator's chat should be
  able to read/act on this app, declare `[expose.chat] tools = "<url>"` in
  the manifest and ship the shim at `apps/<name>/toolshim/` — read-only by
  default; every write endpoint is a deliberate policy decision, argued in
  the PR body. The operator runs `scripts/chat-tools-setup.sh` after merge.
- **Secrets by name only**: new env vars go in the compose fragment as
  `${VAR_NAME:-}` with a note, and in `apps/<name>/env.example` (names, no
  values). Host-side tooling mints the values into `secrets/<name>.env`
  after merge (sso-setup.sh for OIDC clients; install.sh scaffolds the
  file). You never place a value and never read one — declaring by name is
  the whole credential interface an agent has, by design (docs/SSO.md).

PR body per the propose-change skill: blast radius, rollback, credentials
required. Deploy is not yours — after merge, the operator runs
`scripts/deploy.sh`.
