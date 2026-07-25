# Brokered agent egress — as-built

`egress-broker` is the single, deterministic choke point through which agent
tenants reach the outside world. Like `search-broker`, it is a narrow
capability, not a general outbound proxy: an agent never opens a raw socket to
the internet, and the broker — not the agent — holds the authority to decide
what a tenant may reach.

```text
agent tenant --agents--> egress-broker --egress-private--> egress-out --edge--> approved host
                              |--egress-data--> egress-audit-db <-- durable request rows
                                              egress-audit-api <--egress-admin-- Caddy (Ring 0)
```

The design follows the project's core rule: **agents advise, deterministic code
enforces.** An agent may *request* an egress exception; only operator-approved,
deterministic code ever *mints* the credential that grants it. No agent sits in
its own authorization path.

## The problem this solves

Agent tenants sometimes legitimately need to reach a host outside their default
allowlist — `claude.com` for subscription auth, `arxiv.org` for a research
task, a specific vendor API. Two naive answers both fail:

- **Open egress** removes the boundary entirely; a socially-engineered agent
  exfiltrates freely.
- **Per-run operator approval of every host** is secure but wakes the operator
  for every task and stalls tenants while they sleep.

The combinatorial space that makes per-run approval expensive is
`egress endpoints × toolset` per task. We collapse it *before* runtime with
pre-approved **agent profiles**, then approve tasks cheaply against that small,
already-analyzed set.

## Architecture

### Service roles (single image, `EGRESS_ROLE` env switch)

| Role | Service | Networks | Purpose |
|------|---------|----------|---------|
| `broker` | `egress-broker` | `agents`, `egress-private`, `egress-data` | Authenticates tenant, validates key + host, writes audit row, forwards to egress-out |
| `egress` | `egress-out` | `egress-private`, `edge` | Only service on `edge`; makes the actual outbound HTTP call |
| `audit-api` | `egress-audit-api` | `egress-admin`, `egress-data` | Ring 0 read-only dashboard; Caddy injects credential server-side |
| `audit-db` | `egress-audit-db` | `egress-data` | PostgreSQL audit store; no ingress, no egress, no agents spur |

### Network isolation

```
agents (internal: true)     — the agent spur: only egress-broker is reachable here
egress-private (internal)   — broker <-> egress-out: agents cannot join
egress-admin (internal)     — Caddy <-> audit-api: operator dashboard only
egress-data (internal)      — broker/audit-api <-> database: database has no network routes elsewhere
edge                         — egress-out only: the single point of egress
```

## Agent profiles: security analysis at build time, not request time

A profile is a reviewed, version-controlled description of a *shape* of agent
container: its toolset, its default egress allowlist, and its data-plane
authorizations. Profiles are approved a-priori — each one gets a static security
analysis of exactly what egress points and capabilities it combines — and only
approved profiles can be loaded into a container at start.

Profile definitions live in `manifest/egress-profiles/<name>.toml`. Only
profiles that exist in the repo (i.e. were reviewed/merged) may be loaded. An
unknown profile name → container refuses to start.

The default profile (`manifest/egress-profiles/default.toml`) has an empty
allowlist — no standing egress. Any egress requires an operator-approved
exception.

## Minting authority

The **egress-broker mints**, never the agent. The flow mirrors the
task-difficulty model-selection dispatch:

1. A tenant (or its brief) raises an egress exception as an **issue** — a
   request, nothing more. The agent has no authority to mint anywhere.
2. The operator flips a flag / approves the exception list on that issue. This
   human `yes` is a durable, auditable artifact: *who approved which host for
   which tenant, and when.*
3. **Deterministic code** (`scripts/mint-egress-key.sh`) mints a **short-lived
   key** scoped to the approved host(s) and prepares it for the target
   container. This script is NEVER reachable from the `agents` network.
4. Egress physically routes **through the broker**, exactly as `search-broker`
   is wired. The key is load-bearing, not decorative: revoke it and the network
   path is gone, because the tenant never had a route to the outside world that
   did not pass the broker.

## Revocation

Keys auto-revoke on **expiry OR task-end, whichever is earlier** — reusing the
existing LiteLLM virtual-key cleanup path rather than inventing a second
revocation mechanism that could drift out of sync. Expiry is a timer; task-end
is a state; taking the earlier of the two closes the window between a task
dying and its key aging out.

## Failure posture: fail closed

When the broker is unavailable, egress **fails to nothing.** Agent containers
pause and task progress is delayed; that is recoverable. Data egress is not:
once a byte leaves, even for a moment, it is irreversible. Availability is
engineered around — replicate the broker, give it higher CPU/resource priority
— but confidentiality is never traded for uptime. HA is the answer to
"broker down"; it is *not* the answer to "secret leaked," which has no undo.

## Blast radius and layered defense

Centralizing enforcement makes the broker a high-value target — it holds real
credentials and the egress policy — so it is treated as Ring 0 and its own
blast radius is scoped to match. Crucially, it is not the only layer:
**data-plane / execution-plane separation** means a tenant with network access
still has no real data-retrieval capability inside the network unless
*separately* and explicitly authorized. A compromised broker yields a pipe;
it does not yield the private data on the other side of a different boundary.
Each layer assumes the one in front of it has already failed.

## Audit

Every brokered request writes a durable row to `egress-audit-db` **before** the
outbound call (write-then-call ordering so retries cannot lose evidence), with:
- Tenant/capability
- Target host
- Key ID (never the key itself)
- Request method + path
- Status, bytes, duration

## Provision and run

The app source lives in the private Forgejo repo `apps/egress-broker`. Build
the reviewed revision into the local image used by node-config, then provision
and start the capability:

```sh
git clone https://git.<domain>/apps/egress-broker /srv/sovereign-apps/egress-broker
docker build -t sovereign-node/egress-broker:local /srv/sovereign-apps/egress-broker
./scripts/egress-setup.sh
docker compose --profile apps up -d egress-audit-db egress-out egress-broker egress-audit-api caddy
```

## Drills

- `scripts/drill-egress-containment.sh` — proves (a) agents cannot reach the
  internet directly, (b) agents cannot mint/widen their own allowlist, (d)
  killing the broker removes all egress.
- `scripts/drill-egress-revocation.sh` — proves (c) a revoked/expired key is
  refused.

## Open questions (resolved)

The three open questions from the design phase have been resolved as follows:

1. **Broker replication topology**: The initial implementation uses a single
   broker instance. Replication (multiple broker instances behind a shared
   network) is deferred to a follow-up. The broker is stateless (state lives
   in the audit database), so replication is a compose change, not a code one.

2. **Profile review workflow**: Profile definitions live in
   `manifest/egress-profiles/<name>.toml` and are reviewed via the standard
   node-config PR process. The a-priori security analysis is recorded in the
   PR description and review comments. Only merged profiles may be loaded.

3. **Standing per-profile allowlist re-review**: Profile allowlists are
   version-controlled and reviewed at merge time. Periodic re-review is
   handled by the existing node-config maintenance cadence (no additional
   expiry mechanism on profiles themselves).