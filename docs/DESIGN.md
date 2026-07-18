# Design

## Overview

The system is three zones connected by one tunnel:

    ┌─────────────────────────────────────────────────────┐
    │  THE INTERNET (untrusted)                           │
    │  strangers · friend nodes · family devices · APIs   │
    └───────────────────────┬─────────────────────────────┘
                            │ https to your domain
    ┌───────────────────────▼─────────────────────────────┐
    │  FRONT DOOR (pluggable: this box, or a disposable   │
    │  VPS anchor). Caddy TLS + deterministic policy.     │
    │  Holds ZERO data, ZERO secrets.                     │
    └───────────────────────┬─────────────────────────────┘
                            │ WireGuard tunnel, dialed
                            │ OUTBOUND by the node
    ┌───────────────────────▼─────────────────────────────┐
    │  THE NODE (owned)                                   │
    │  secrets vault · LiteLLM · Forgejo · apps ·         │
    │  google bridge · agent runtime · backups            │
    └─────────────────────────────────────────────────────┘

Inbound traffic must pass the front door and its policy. Outbound traffic (calls
to Google APIs, model providers, GitHub sync, feed pulls) leaves the node directly
and never touches the front door. The node accepts no inbound connections from its
local network's perspective when an anchor is used — the tunnel is outbound, so
zero ports are opened at home, and CGNAT is irrelevant.

## The front door is pluggable

The placement manifest's `front_door` field selects among: `direct` (this box has
a reachable IP and Caddy binds here — the tier-3 MVP default for people who have
one), `byo-anchor` (the operator already runs a VPS or tunnel and wires into it),
`guided-vps` (the installer-agent provisions a disposable anchor with the operator
approving the two payment moments), and `none` (off-grid: no public routes, all
access via LAN or private overlay). The anchor, when present, is stateless by
construction: config is re-derivable, WireGuard keys are re-mintable, and
destroying it loses nothing.

The anchor is an L4 SNI passthrough (Caddy's layer4 module or nginx `stream`),
not a TLS terminator: TLS terminates ON THE NODE. The anchor reads the SNI,
forwards the still-encrypted stream down the WireGuard tunnel the node dialed
outbound, and never holds certificates or sees plaintext. Its trust level is
"dumb pipe" — compromise yields traffic metadata only: no sessions, no identity,
no content. Node-side Caddy owns ACME and TLS for all public hostnames, using
DNS-01 via the node's own CoreDNS (M2) since the node may not be directly
reachable for HTTP-01. Identity traffic (`auth.<domain>`) flows through the same
passthrough; the VPS holds zero identity state.

## Containment: networks stop the wire, namespaces stop the kernel

Enforcement is structural, not behavioral. Caddy is the sole member of the host's
port namespace (plus Forgejo SSH if deliberately enabled). Services join the `edge`
network only to the extent Caddy must reach them; databases live on private
networks with exactly one client. Agent runtimes never join `edge`: they live on
the `agents` spur, a network whose only other members are the services their
manifests declared — the wire itself mirrors the manifest, so an injected agent
cannot even route a packet to a service it was never granted.

The spur is also `internal`: agents have no direct internet. This kills the
lethal combination — untrusted content in, private data held, arbitrary
egress out — at the network layer rather than in a prompt. An agent's only
paths out of the box are LiteLLM (logged, budgeted inference) and whatever
write scopes its manifest declared; upstream code arrives through
operator-run pull mirrors, never direct fetch. A fooled agent that has read
your mail has nowhere to send it.

The node is single-human but multi-tenant, and the tenants are programs that must
not trust each other: third-party images one supply-chain compromise from hostile,
vibe-coded apps of varying quality, Ring 2 surfaces exposed to the internet, and
agent runtimes presumed injectable. Network isolation stops lateral movement over
the wire; it does nothing about container escape. So the execution target is
rootless podman with a distinct user-namespace range per app: "root" inside any
container maps to a different unprivileged host UID per app, there is no
root-owned daemon to pivot through, and an escaped attacker lands on the host as
a nobody that cannot read any other app's files, the vault, or the socket. The
MVP runs on Docker Compose for ubiquity; the compose file is written to remain
podman-compatible, and the agent runtime slot adopts rootless podman first.

## Identity: the node is the identity provider

Humans get one identity at the door; machines get many identities inside; nothing
is trusted for being on the network.

Human plane (Rings 0/1): the node runs the identity provider — Pocket ID,
node-resident, behind the front door at `auth.<domain>`: passkey-only OIDC in a
single container with SQLite. The chain of authority is: passkey in the user's
phone secure enclave → node Pocket ID (OIDC) → everything federates. "No
passwords anywhere in Rings 0/1" is enforced by the product, not maintained by
configuration — Pocket ID has no password mode to misconfigure. Apps integrate
one of two ways: (a) native OIDC (Forgejo, Miniflux, and most self-hostable
apps) against Pocket ID; (b) oauth2-proxy forward-auth at Caddy for apps
without OIDC (Radicale) — the proxy asserts the authenticated identity and the
app trusts it. User lifecycle UI (invites, offboarding) is our dashboard's job,
driving the IdP API; the IdP itself stays a small, boring OIDC core. Family
access needs no overlay client: public front door + passkey covers Ring 1.

"Node-resident" is a placement statement, not "on-prem": in Tier 1
(`compute: cloud`, e.g. a GCP VM) the IdP runs on that VM — same stack, same
manifest. The root of trust is the phone passkey regardless of placement, and a
migration cloud→mini carries the identity plane as ordinary volumes: same
domain, same users, no re-enrollment.

There is no third-party in the identity chain. A private overlay (Tailscale et
al.) is at most an optional operator convenience, never a dependency; if one is
ever added it must be Headscale using Pocket ID as its OIDC source, because no
third-party service may be an identity root.

The credential taxonomy, in full:

| Class          | Examples                                              | Held by                | Humans see it?     |
|----------------|-------------------------------------------------------|------------------------|--------------------|
| Daily / human  | one passkey per person                                | phone secure enclave   | it *is* them       |
| Machine-held   | LiteLLM virtual keys, per-caller service tokens, deploy keys | vault, injected as secrets | never          |
| Cold upstream  | Google OAuth (gog bridge), registrar, VPS, backup storage, model providers | vault; touched at setup/billing/token-refresh only | rarely |

Cold-upstream accounts are operated day-to-day by agents via scoped API tokens.
The rule that binds the taxonomy: no upstream account may be an identity ROOT —
upstream holds delegated tokens, never the reverse.

Machine plane (services and agents): zero-trust inside the box. Every internal
caller — an app calling another app, an agent calling anything — holds a distinct,
per-target, per-scope credential: the digest agent a read-only CalDAV token plus a
budgeted LiteLLM virtual key; the meeting-notes app a calendar events.write token
and nothing else. Credentials are minted at install time from the app manifest's
declared `needs` (deny by default: declare nothing, receive nothing), injected as
secrets, revocable individually, and every internal call is logged with its
caller's identity. Compromise of one caller reveals exactly what its manifest
declared. The MVP implements this with boring per-caller tokens (Forgejo scoped
tokens, LiteLLM virtual keys, CalDAV credentials, header-auth on internal routes);
an internal CA with mTLS is the later upgrade if cryptographic caller identity is
ever warranted — token-per-caller-per-scope delivers most of the value at a
fraction of the complexity.

## Secrets

There is exactly one place secrets live: the node (in the MVP, the `.env` file and
LiteLLM's encrypted key store; in Tier 2, a dedicated hardware vault). Provider
API keys enter LiteLLM and never leave it — every consumer, human or agent, holds
a *virtual* key with its own budget, rate limit, model allowlist, and revocation
switch. Google access flows through the gog/gws bridge holding scoped OAuth
tokens, read-only by default, with send/write escalated per-consumer. The anchor
holds only its own WireGuard key and TLS certificates. Backups are encrypted
client-side with a passphrase that must exist somewhere physical, off the box.

## App manifests and the service registry

Every app ships an app manifest (see `manifest/app.example.toml`): what it exposes
(port, OpenAPI contract, optional MCP tool surface, health endpoint), what it
needs (scoped internal calls, LLM budget), what resources it consumes, how it is
tested, and what must be backed up. The placement manifest says where things live;
app manifests say what things are and how they may be called. Together they are
the node's stable interface — apps target these contracts, and the executor
underneath (compose today, anything tomorrow) is a replaceable detail.

The node aggregates manifests into a service registry: one endpoint answering
"what services exist here and what can I call," serving humans, the installer,
and agents alike. For agent consumers the registry is effectively MCP discovery;
for service-to-service calls it points at OpenAPI contracts. The manifest format
tracks openhost.toml as a compatibility target, not a dependency — their app
ecosystem should run here with minimal translation.

New services start from the skeleton (`templates/app-skeleton`): a runnable
stdlib service that already satisfies every contract — manifest, OpenAPI
stub, MCP tool stub, health endpoint, smoke tests the change pipeline can
run. `scripts/new-app.sh <name>` seeds it as `apps/<name>` in Forgejo with
the dev-agent as collaborator, so "up a service of kind X" is: operator runs
one command, agent clones and fills the contracts, two PRs come back (the
app itself, and its node-config registration — compose service, route,
backups). Repo creation stays an operator moment; everything after is the
agent's. The skeleton lives in node-config, so improving it is an ordinary
agent PR — the scaffold itself is under version control like everything
else.

The know-how around these mechanisms is packaged the same way. `scripts/`
holds the deterministic, operator-run tools (they carry tokens and touch
docker — ring 0 by nature); `skills/` holds the agent-facing procedure
layer that wraps them: new-app, wrap-upstream, register-service,
propose-change. Skills are the division of labor made explicit — each one
says what the agent does, what it asks the operator to run, and what ships
as a PR. Because the library lives in node-config it travels into every
agent workspace clone, any tenant occupying the slot inherits the same
procedures, and improving a skill is an ordinary PR. Scripts enforce;
skills instruct; neither substitutes for the other.

The library is framework-agnostic on purpose: the operating contract is
`AGENTS.md` (the cross-tool convention Codex, Gemini CLI, and most newer
runtimes read natively) and skills are plain `skills/<name>/SKILL.md`
files following the open Agent Skills format. Framework-specific wiring is
kept at the edges, one line each: a tracked `.claude/skills` symlink gives
Claude Code its discovery path, and each tenant's Dockerfile copies
`AGENTS.md` wherever its framework expects instructions. Swapping or
adding a tenant runtime touches the wiring, never the library.

## Environments and the change pipeline

The Boq-derived capabilities — representative environments, ephemeral testing,
versioned deploys — are implemented at compose scale rather than platform scale.
Staging is the same stack under a second compose project name with throwaway
volumes. The change pipeline, which is the ops agent's primary workflow, is:
branch `node-config` → spin staging → run every affected app's manifest-declared
tests against it → open a PR the operator approves → redeploy prod → tear staging
down. Deploys are versioned by construction: images pinned by digest, config
pinned by git tag, rollback is `git revert` plus redeploy. Promotion on red tests
is refused mechanically, not by convention.

## The policy loop

Gateway rules — which routes exist, in which ring, with which guards — are files
in the `node-config` repository. The intended lifecycle: the ops agent drafts a
change as a commit, the operator approves it (a merge, eventually a tap on a
phone), the front door pulls and enforces it. The agent proposes; git records;
deterministic code enforces. An LLM is never in the request-authorization path,
because unauthenticated internet traffic must never be able to talk its way in.

## The Google bridge instead of a mail server

Self-hosted SMTP is a reputation game measured in years and mostly lost from
residential and cloud IPs. The design accepts Gmail (or any provider) as a dumb
public mail edge while the node holds the intelligence and a continuously synced
local mirror — sovereignty as credible exit rather than self-operation. The bridge
container (gog or gws) exposes mail, calendar, drive and contacts to agents as a
typed MCP surface that is read-only by default, with untrusted-content wrapping as
a first defense against injection-by-email. If Google must someday be exited, the
mirror plus the owned domain make it a migration, not a loss.

## The agent runtime slot (reserved, not yet built)

The slot is a jailed container profile with the following contract: rootless
podman with its own user-namespace range; network access to the `agents`
spur only (a network whose other members are exactly the services the
tenant's manifest `needs` declared — never `edge`, never a database network);
inference exclusively via an injected LiteLLM virtual key; service access
exclusively via per-caller scoped credentials minted from its manifest's `needs`,
discovered through the registry; no docker socket, no volume mounts outside its
own workspace, no vault visibility; every action logged with its identity;
destructive operations queued for human approval. Any runtime satisfying the
contract may occupy the slot — OpenClaw included, which turns "run the viral
agent without becoming a breach statistic" into this project's beachhead use
case.

The slot supports two tenancy modes. The **resident** tenant is the
interactive dev-agent: a session the operator opens, talks to, and closes,
with a workspace that persists because its job is a continuing conversation
about the node. Everything else — scheduled jobs, ambient tasks, one-shot
migrations — runs as an **ephemeral** tenant: a container minted for one
task under the same contract, started clean, destroyed with its workspace
when the task ends.

Ephemeral tenancy is the deliberate answer to context rot. Long-running
agents accumulate conversational state until quality degrades, cost
balloons, and the accumulated context itself becomes the thing a prompt
injection exfiltrates. The node refuses to let state live in an agent at
all: whatever a task needs arrives as files (a task brief in git, the
read-only surfaces its manifest declares), and whatever it produces leaves
as files (a PR, a digest, a report committed somewhere reviewable). Memory
belongs to git, not to a process; a successor picks up from artifacts,
never from a transcript.

Credentials follow the same lifecycle. An ephemeral tenant's LiteLLM
virtual key is minted per run with a per-task budget and an expiry; its
service tokens are the narrowest its manifest allows; teardown revokes
whatever expiry has not already killed. A prompt-injected ephemeral tenant
holds one task's budget, one task's scopes, and no history — the blast
radius of a mayfly.

Agent tasks that read from many services and answer or write back follow a
triage rule with three tiers. **Reads** may be broad: read scopes across
mail, calendar, notes, feeds are what make an assistant useful, and the
egress lockdown plus read-only defaults bound the damage. **Writes** must be
enumerable: each one a named manifest scope (`events.write`,
`notes.append`), minted individually, so the audit log answers "what can
change my data" by listing scopes, not reading code. **Destructive
operations** (deletions, migrations, sends to third parties) are never a
scope — they queue for human approval, always. Answering a question is a
read plus an artifact; changing your calendar is a declared scope;
deleting anything is an approval moment.

## Disaster recovery: the bootstrap chain and the Recovery Kit

The node is reproducible from two things: the `node-config` repository and the
restic snapshots (whose inclusion list is generated from app manifests' `backup`
fields). Since the snapshots contain Forgejo — and therefore `node-config` —
the node rebuilds from snapshots plus the credentials to reach them. Exactly
three things must survive the box; place them deliberately:

1. **restic passphrase + repo location** → phone secure enclave AND a printed
   card;
2. **passkeys** (private halves) → already phone-resident; the restored
   `pocketid_data` volume (Pocket ID's SQLite) holds the public halves, so
   every login works immediately post-restore;
3. **domain + anchor** → survive independently of the box; the anchor serves a
   "node restoring" page and waits for a new box to dial in.

The recovery flow is a product requirement written for a non-technical
operator: new box → scan QR → phone releases the recovery bundle (repo location
+ keys) → agent restores volumes, re-mints tunnel keys, dials the anchor →
done. The human does three things: plug in, scan, approve. Maximum loss is one
backup interval; the mail-mirror and calendar volumes get hourly snapshots,
everything else daily.

The Recovery Kit ships in the box: a printed card carrying the restic
passphrase and recovery codes as QR. The card alone MUST be sufficient — it
covers the case where phone and box are lost together. A family-quorum reset
covers the operator-incapacitated case.

The ops agent runs a quarterly automated restore-to-staging drill and surfaces
a "backups verified <date>" status; the manifest's `backups.tested_restore`
field is updated automatically from drill results, truthfully. Migration
between placements (laptop to mini to cloud and back) is the same procedure as
disaster recovery, which means every migration doubles as a tested backup.

## Decision log

Caddy over Traefik/nginx: automatic TLS and a config file short enough to audit by
eye. Forgejo over GitLab/Gitea: community-governed, light, native GitHub mirroring
via deploy keys. LiteLLM as the inference chokepoint: virtual keys give
per-consumer budgets and one revocation point, and local inference later becomes a
config line rather than an architecture change. WireGuard outbound-dial over port
forwarding: CGNAT immunity and zero open home ports. Postgres 16 over anything
exotic: it is Postgres. Docker Compose over Kubernetes: one node, one operator, no
cluster — complexity is the enemy of month six. Manifest as files-in-git over a
settings UI: diffable, revertible, readable by installer-agent and operator alike.
Rootless podman as execution target over root-daemon Docker: per-app user
namespaces contain container escape, and there is no root daemon to own.
Per-caller tokens over mTLS-everywhere: boring, legible, individually revocable;
cryptographic caller identity can arrive later without redesign.
Point-to-point tool calls over an MCP gateway: agents connect directly to
each service's declared surface, presenting that service's per-caller token,
with network membership derived from the manifest — same containment as a
gateway without a new high-value chokepoint or a framework-shaped
dependency. The registry stays discovery-and-audit only; a gateway can
arrive later, like mTLS, if centralized enforcement is ever warranted.
AGENTS.md and `skills/` over framework-specific paths: agent frameworks
churn (the roadmap names this risk), so instructions and procedures live
at framework-neutral paths in open formats, and each runtime gets one line
of wiring — a symlink or a Dockerfile COPY — never ownership of the
contract.
Pocket ID over Authentik: an invariant enforced by the product beats an
invariant maintained by configuration — Pocket ID cannot do passwords, so
"no passwords in Rings 0/1" is structural; family lifecycle UI belongs to our
dashboard driving the IdP API, not to the IdP; and four heavy containers for
an admin UI we intentionally hide failed agent-legibility and
boring-technology review. OIDC keeps the IdP swappable if needs outgrow it.
No third-party identity roots — ever. The anchor is an L4 passthrough: rented
hardware gets metadata, never plaintext.

Two principles govern future choices. Agent legibility: the sysadmin is a language
model, so mainstream formats (compose, Caddyfiles, systemd, TOML/YAML) are a
feature — training corpora are saturated with them — and elegant-but-exotic
abstractions degrade the ops agent that keeps this node alive. Own the contract,
rent the orchestrator: the manifests are the stable interface apps and agents
target; frameworks and executors underneath must remain swappable, because
depending on open contracts preserves exit while depending on singular frameworks
does not — regardless of license.
