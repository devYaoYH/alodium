---
name: register-service
description: Register an app on the node — compose service, Caddy route in the right ring, manifest, backups — as one node-config PR. Use after new-app or wrap-upstream, or when changing an existing service's exposure or resources.
---

# Register a service in node-config

One PR, one concern: this app's presence on the node. Checklist:

- **Compose service** (`docker-compose.yml`): image pinned by digest
  (`docker buildx imagetools inspect <image>` for the digest; the
  scripts/pin-images.sh pattern), named volumes only, `restart:
  unless-stopped`, no docker socket, no host mounts.
- **Networks, minimal**: `edge` only if Caddy must route to it; the
  `agents` spur only if agent tenants call its declared surface; a private
  network for its database with exactly one client. Never grant a network
  "just in case" — the wire mirrors the manifest.
- **Caddy route** (`caddy/Caddyfile`): the right ring — `import ring0`
  (operator), `import ring1` (trusted people; add the forward-auth snippet
  if the app lacks native OIDC), ring 2 rare and justified in the PR body.
  Reference `{$NODE_DOMAIN}` only; a literal domain in tracked config is a
  bug.
- **Manifest** (`manifest/<name>.toml`): present and truthful; every volume
  that must survive the box listed under `[lifecycle] backup` — the restic
  include list is generated from it.
- **Dashboard** (`config/homepage/services.yaml`): add it if ring 1 humans
  should see it.
- **Secrets by name only**: new env vars go in the compose file as
  `${VAR_NAME}` with a note; the operator fills `.env` on the host. Never
  place a value.

PR body per the propose-change skill: blast radius, rollback, credentials
required. Deploy is not yours — after merge, the operator runs
`scripts/deploy.sh`.
