# Brokered agent egress

`egress-broker` is the single, deterministic choke point through which agent
tenants reach the outside world. Like `search-broker`, it is a narrow
capability, not a general outbound proxy: an agent never opens a raw socket to
the internet, and the broker — not the agent — holds the authority to decide
what a tenant may reach.

```text
agent tenant --agents--> egress-broker --egress-private--> egress-out --edge--> approved host
                              |--egress-data--> egress-audit-db <-- durable request rows
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

## Agent profiles: security analysis at build time, not request time

A profile is a reviewed, version-controlled description of a *shape* of agent
container: its toolset, its default egress allowlist, and its data-plane
authorizations. Profiles are approved a-priori — each one gets a static security
analysis of exactly what egress points and capabilities it combines — and only
approved profiles can be loaded into a container at start.

This inverts the cost. Instead of adjudicating an open-ended host request at
task time, the operator picks from a small set of pre-analyzed shapes. A
"research" profile may *always* reach `arxiv.org` and `claude.com` because that
combination was already reviewed; a task using it needs only a start approval,
not a fresh egress adjudication. New shapes require a new profile review — a
deliberate, auditable, infrequent event — not a runtime hole.

## Minting authority

The **egress-broker mints**, never the agent. The flow mirrors the
task-difficulty model-selection dispatch:

1. A tenant (or its brief) raises an egress exception as an **issue** — a
   request, nothing more. The agent has no authority to mint anywhere.
2. The operator flips a flag / approves the exception list on that issue. This
   human `yes` is a durable, auditable artifact: *who approved which host for
   which tenant, and when.*
3. Deterministic code mints a **short-lived key** scoped to the approved
   host(s) and prepares it for the target container.
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

## Open questions

- Broker replication topology and how paused tenants resume cleanly after a
  broker restart.
- Profile review workflow: where profile definitions live, who signs off, and
  how the a-priori security analysis is recorded alongside the approval.
- Whether standing per-profile allowlists need periodic re-review (expiry on
  the *profile*, not just the key).
