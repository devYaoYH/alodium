# The infra co-pilot

A capable, human-paired Claude Code session that sits beside you in the browser
(`copilot.<domain>`, off the workshop page's **Ask your engineering org** entry).
You talk to it to plan work, file issues, review PRs, and direct the cheaper
implementation swarm (agent-dev). It is the *orchestrator*; agent-dev is the
*labour*.

## Why it exists — and why it is cheap

The swarm runs on cheap OpenRouter models through LiteLLM (metered). The co-pilot
instead runs on **your Anthropic subscription** — flat-rate Opus/Sonnet, not
per-token API billing — so the more capable planning/reviewing brain does not
run up an API bill. It authenticates with a subscription OAuth token
(`claude setup-token`), never a LiteLLM virtual key or an API key.

## How it stays contained

The co-pilot is more privileged than a jail tenant (it directs the swarm), so it
gets a genuinely capable model — but it is boxed in by construction:

- **Code plane only, never the data plane.** It is on `front` (its door),
  `agents` (the spur: forgejo, registry), and `copilot-egress` (its proxy) — and
  **no data-plane network**. It has no route to and no credential for memos,
  radicale/calino, assistant chat logs, or any database. `scripts/verify-config.sh`
  asserts this on every config change, so a PR that tries to widen its reach
  fails the gate before it can be merged.
- **One sanctioned egress.** Its only internet path is `copilot-egress`, a
  deny-by-default CONNECT proxy that tunnels TLS to Anthropic's own domains
  (`*.anthropic.com` = the API, `*.claude.com` = the subscription-auth/console
  plane) and refuses everything else. No direct internet, no arbitrary fetch.
- **Proposer, not applier.** Its Forgejo token opens PRs and files issues; branch
  protection on `main` keeps merge — the deploy authorization moment — with you.
  It never runs `scripts/deploy.sh` and never holds the docker socket.
- **Operator-only door.** `ring0` (LAN/VPN) + passkey SSO + operator-email-only.
  ttyd itself is unauthenticated; the Caddy door is the authentication boundary.

## Deploying it — operator steps

The app fragment auto-builds on deploy, but a few things need you (they involve
real credentials and the IdP, which no agent may touch):

1. **Mint the subscription token** on a trusted machine:
   ```
   claude setup-token
   ```
   Copy the token into `secrets/copilot.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`.
2. **Create the `copilot` Forgejo user** and a token with scopes
   `write:issue, write:repository` (NOT org/admin); put it in `secrets/copilot.env`
   as `COPILOT_FORGEJO_TOKEN=...`. Then:
   - **Grant repo access** so it can propose: add `copilot` as a **write**
     collaborator on `node-config` and `coordination` (token scope alone is not
     access).
   - **Protect `main`** on both repos so the token stays a proposer: block direct
     push and restrict merge to the operator. This is the apply-gap. Example:
     ```
     POST /api/v1/repos/operator/<repo>/branch_protections
       {"rule_name":"main","enable_push":false,
        "enable_merge_whitelist":true,"merge_whitelist_usernames":["operator"]}
     ```
   (`mint-secrets.sh` scaffolds `secrets/copilot.env` from `apps/copilot/env.example`
   on deploy and will flag both credentials as operator-owed if left blank.)
3. **Enable the profiles.** The door needs the auth shim, and the seat is
   profile-gated so it is opt-in:
   ```
   docker compose --profile authshim up -d      # oauth2-proxy (if not already up)
   docker compose --profile copilot  up -d       # copilot + copilot-egress
   ```
   `scripts/sso-setup.sh` (run automatically by `deploy.sh` when the compose/route
   change lands) mints the `copilot.<domain>` SSO callback.

After that, `scripts/deploy.sh` keeps it current on every merge like any other
app: it rebuilds the images when their build inputs change and recreates the
running containers.

## Known limits / follow-ups

- The co-pilot's `$HOME` (Claude state, tmux session) is ephemeral — a container
  recreate ends the running session. Persisting it behind a volume is a
  follow-up.
- The ttyd binary is version-pinned, not yet sha256-pinned (see
  `apps/copilot/Dockerfile`).
- General web lookups are out of scope for now (Anthropic-only egress). Wiring
  the co-pilot to the existing `search-broker` for mediated fetch is the natural
  next step (capability, not raw connectivity).
