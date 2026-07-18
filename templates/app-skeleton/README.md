# __APP_NAME__

Seeded from the node's app skeleton (`templates/app-skeleton` in
`node-config`) by `scripts/new-app.sh`. This is the bare-minimum service
that already honors the node's contracts — develop by replacing the stubs,
never by deleting the contract files.

## The contract, as files

| File             | Contract it satisfies                                      |
|------------------|------------------------------------------------------------|
| `app.toml`       | app manifest v0 — ring, exposes, `needs`, resources, tests |
| `openapi.yaml`   | the service-to-service surface (`[service].api`)           |
| `mcp-tools.json` | the typed tool surface for agent consumers (`[service].mcp`) |
| `app.py`         | the service; `/healthz` must stay                          |
| `tests/smoke.py` | what the change pipeline runs against staging (`[tests]`)  |

## Developer checklist (agent: this is your definition of done)

1. Rename nothing; fill everything: `app.toml` fields, real endpoints in
   `openapi.yaml`, real tools in `mcp-tools.json` (or delete the `mcp` line
   from `app.toml` if this app has no agent surface — deliberately).
2. Declare `needs` minimally. Every entry mints a credential; an empty
   table is a feature.
3. Keep `/healthz` cheap and honest — the dashboard and the change
   pipeline both poll it.
4. Extend `tests/smoke.py` with every endpoint you add; the pipeline
   refuses promotion on red.
5. Ship the node-config side as a separate PR: compose service (image
   pinned by digest), Caddy route in the right ring, backup volumes
   declared in `app.toml [lifecycle]`.
