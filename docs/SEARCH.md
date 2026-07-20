# Audited agent web search

`search-broker` is a deliberately small capability, not a general outbound
proxy:

```text
agent-dev --agents--> search-broker --search-private--> search-egress --edge--> api.exa.ai
```

The broker has no `edge` network. It accepts only `POST /v1/search`, requires
the revocable `AGENT_SEARCH_TOKEN`, limits the request shape, and records a
JSONL audit event with a query hash, caller capability, Exa request ID, result
URLs, status, byte count, duration, and reported cost. It does not record the
raw query or any bearer token. The egress process is the only holder of
`EXA_API_KEY`; it has a hard-coded `POST https://api.exa.ai/search` upstream
and is not a reusable HTTP proxy.

## Provision and run

The app source lives in the private Forgejo repo `apps/search-broker`. Build
the reviewed revision into the local image used by node-config, then provision
and start the capability:

```sh
git clone https://git.<domain>/apps/search-broker /srv/sovereign-apps/search-broker
docker build -t sovereign-node/search-broker:local /srv/sovereign-apps/search-broker
./scripts/search-setup.sh
docker compose --profile apps up -d search-broker search-egress caddy
```

`search-setup.sh` copies the `EXA_API_KEY` you placed in `.env` into the
host-only `secrets/search-broker.env`, mints the broker-to-egress token there,
and mints a separate caller token in `.env`. It never prints those values.
After verifying the service, remove the legacy `EXA_API_KEY` line from `.env`;
the per-app secret file is the authoritative location.

`search-setup.sh` also mints `SEARCH_AUDIT_TOKEN`. Only Caddy and the broker
receive that value; Caddy injects it over the dedicated internal
`search-admin` network for `https://search.<domain>`. This Ring 0 dashboard
lists safe, newest-first search-event summaries. It intentionally excludes raw
queries, API keys, and bearer tokens, and agents cannot reach its network.

An agent calls `http://search-broker:8080/v1/search` with
`Authorization: Bearer $AGENT_SEARCH_TOKEN`. Search results are untrusted
external content: they are evidence to assess, never instructions or authority
to exfiltrate data, alter policy, or fetch arbitrary URLs.

## What this does and does not enforce

Compose enforces the meaningful caller boundary: agents cannot resolve or
connect to `search-egress`, and neither agent receives the Exa key. The egress
service logs each Exa request and only its fixed API path is implemented.

A compromised egress container still has Docker's ordinary `edge` connectivity.
For a cryptographic/L3 destination allowlist, put `search-egress` behind a
host-managed egress firewall or authenticated CONNECT proxy whose policy allows
only `api.exa.ai:443`, and export its connection logs to the same audit store.
That host-level control is intentionally a separate deployment change: it is
the enforcement point for a container compromise, not just normal application
behavior.
