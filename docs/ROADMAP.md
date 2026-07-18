# Roadmap

Sequencing principle: build the middle rung first (Tier 3, this repo), extract the
placement manifest from what was actually built, then generalize outward. Tiers 1
and 5 are simplifications of Tier 3; Tiers 2 and 4 need hardware and come last.
Nothing ships to a second user until node #1 has survived a month and a restore
drill.

## M0 — Node #1 (this repository)

The operator's own node runs the MVP stack: Caddy, LiteLLM with virtual keys,
Forgejo holding `node-config`, Radicale, daily restic backups. Exit criteria: a
config change deployed via git rather than by hand; one virtual key minted and one
revoked; one successful restore drill with the date recorded in the manifest;
thirty days of unattended uptime. This milestone also produces the build-log —
written in public, because for this audience the diary *is* the marketing.

## M1 — The installer-agent and the contracts

The interview replaces the README. A CLI agent reads and writes the placement
manifest, detects reachability (public IP, IPv6, CGNAT) and proposes front-door
options, drives DNS via registrar API tokens the operator grants, provisions the
guided-VPS anchor with the operator approving the two payment moments, and renders
compose plus Caddyfile from the manifests. The app manifest v0 lands here — the
first app is installed from its manifest, with `needs`-declared credentials minted
automatically — along with the service registry endpoint. Exit criteria: ten
strangers from the build-log audience reach a running node in under thirty
minutes; every stumble becomes a roadmap item. The agent prepares everything and
hands the human three clicks — account signups and payments stay human by design.

## M2 — The front door grows up, and changes get a pipeline

The guided-VPS anchor becomes a first-class placement: stateless cloud-init,
outbound WireGuard from the node, an L4 SNI passthrough so TLS terminates on the
node and the VPS never sees plaintext, node-run CoreDNS issuing wildcard certs
via DNS-01 so registrar tokens are never lent out. The identity layer lands here, fully
specified: Pocket ID as the node-resident IdP (passkey-only by construction;
the IdP is deferred until the second human — M0–M1 use app-native logins),
oauth2-proxy forward-auth at Caddy for non-OIDC apps, per-caller scoped tokens
for machines inside, deny by default. The policy loop lands (agent-drafted rule commits,
operator approval, deterministic enforcement), and so does the change pipeline:
staging project, manifest-declared tests, promotion refused on red, deploys
pinned by digest and git tag. Exit criteria: destroying and re-provisioning the
anchor in under twenty minutes with zero data loss, demonstrated on video; the
anchor rebuilt as L4 passthrough, demonstrated; one app update flowing branch →
staging → tests → PR → prod with no hand-edits; one full recovery drill — fresh
VM, restore from snapshots via the QR/bundle flow, family passkey logins working,
in thirty minutes or less.

## M3 — The Google bridge and the agent slot

The gog/gws bridge container ships with scoped OAuth, read-only-by-default MCP,
send disabled at the door, and the continuous local mail mirror that makes the
Gmail dependency exitable. The agent runtime slot ships as a jailed rootless
profile honoring the contract in DESIGN.md — per-caller credentials from its
manifest, discovery via the registry — with OpenClaw as the reference tenant and
a first ambient agent that is deliberately low-stakes: the feed digester
(Miniflux lands here too). Ambient tasks run in the slot's ephemeral mode
(DESIGN.md): one container per task, per-run virtual key with budget and
expiry, workspace destroyed at teardown, all state in git artifacts rather
than agent memory. Because that state is git-native, agent coordination rides
the same rail: resident and ephemeral tenants track work and leave notes for
each other as Forgejo issues and project boards — the tracker the node already
runs, on the wire the agents already reach — rather than a new service. Enabling
it is a scope bump, not a container: the agent token gains `write:issue` (widened
by a reviewed diff, per DESIGN.md's jail principle), a `coordination` surface
(org board or a dedicated repo) becomes the shared notebook, and AGENTS.md
specifies the hand-off protocol — what an ephemeral tenant records before
teardown so the next run, or a human, can pick up. A gitea-compatible MCP shim
can front the REST API later; it is optional, since the jail can already call it.
Exit criteria: an agent summarizes the operator's
morning — mail subjects, calendar, feeds — using only virtual keys and read-only
surfaces; that digest runs as an ephemeral tenant whose per-run key is dead
(expired or revoked) by teardown; a simulated prompt-injection in a test
email demonstrably fails to escalate; and one ephemeral tenant leaves a note
(issue or board card) that a subsequent run reads and acts on.

## M4 — Ring 1: other people

Family accounts across services via the door's SSO, calendar ACLs (edit for
family, busy-only feed public), the QR onboarding flow that configures a phone in
one scan — dashboard → Pocket ID API one-time link → passkey enrollment on the
family phone; no overlay client, no passwords — and shared photos or files as
the anchor app. Exit criteria: one
non-technical household member uses the node daily for a month without asking the
operator for help. This milestone makes the operator a service provider; the UPS
and the monitoring agent stop being optional here.

## M5 — Placement as a product

The manifest's placement fields become live: migrations between local and cloud
compute executed by the ops agent as backup-restore-repoint, the Tier 2 secrets
box (hardware vault holding keys, injected at runtime) and the Tier 4 local
inference entry (an OpenAI-compatible endpoint added to LiteLLM, with
sensitivity-based routing so mail-reading agents can run local). Exit criteria:
one migration in each direction performed by the agent with the operator only
approving.

## M6 — Federation

Node-to-node: alice.example grants bob.example a busy-calendar read, enforced by
mTLS or signed requests at the front doors, no shared platform anywhere. Prior
art to steal from: Matrix server-to-server, IndieAuth, ActivityPub. Publishing
lands here too — POSSE with agents doing the syndication labor. This milestone is
deliberately last: it is the largest unbuilt idea and it deserves the foundation
under it.

## Upstream policy: contribute commodity, keep moat

openhost (Imbue) validates the substrate thesis and overlaps the plumbing; it does
not build the identity layer, the trust architecture, the ladder, or federation.
Policy: file issues and small PRs upstream where shared substrate is touched
anyway (podman hardening, DNS edge cases, manifest interop) — cheap, authentic,
keeps a relationship and a vantage point — while this repository owns the
differentiated layers. The app-manifest format tracks openhost.toml as a
compatibility target so their app ecosystem can run on these nodes; their repo is
a prospective app catalog, not a rival. The same rule generalizes: for any
adjacent project, contribute where the work is commodity, keep what is moat, and
never donate the trust architecture upstream. Ideas from evaluated platforms
(Foundation/Boq: typed contracts, environments, ephemeral tests) are strip-mined
into our manifests rather than adopted as frameworks — see the decision-log
principles on agent legibility and owning the contract.

## Distribution plan

The motion is family-and-friends propagation, not mass launch. Phase one: the
public build-log of node #1 attracts the operator cohort — people with domains and
opinions — and M1's installer converts ten of them; their stumbles are the
backlog. Phase two: each operator onboards a household via M4's QR flow; every
node is its own tiny distribution network, and some family members become the
next operators. Phase three: hardware kits (pre-flashed mini for Tier 3, the Pi
vault for Tier 2) for people who want the box to arrive working. Phase four, only
when support demand proves it: the managed anchor — domain registered with the
customer as legal registrant, stateless VPS operable by us and fireable in one
click — convenience with a guaranteed exit, never a hostage. Revenue follows the
same order: nothing, then hardware margin, then managed-anchor subscription and
support; the software stays open-core throughout because the buyers of
sovereignty audit their landlords.

## Risks worth naming

Month-six abandonment is the historical killer; the ops agent is the mitigation
and M0's thirty-day criterion is the test. The frontier-API dependency inside the
sysadmin-agent is real until M5's local inference matures. Google can change API
terms under the bridge; the mirror and the owned domain bound the damage. Agent
frameworks will churn; the slot contract, not any tenant, is the stable interface.
And the operator becoming a single point of failure for a family is mitigated by
boring things — the UPS, the backups, the drill — not by optimism.
