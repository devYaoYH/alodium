# floor

The factory floor: a live isometric view of this node. Every service is a
room in a wing (gate / core plane / operations / agent bay / apps), every
declared data path is a conveyor line, and observed data movement — git
pushes, LLM inference spend, issue traffic, container lifecycle — walks
the corridors as small workers.

Read-only by construction. Sources and what they light up:

| source        | credential            | signal                              |
|---------------|-----------------------|-------------------------------------|
| registry      | none                  | which rooms exist, their `needs` edges |
| docker-proxy  | none                  | which rooms are lit, lifecycle events |
| forgejo       | `FLOOR_GIT_TOKEN`     | commit / PR / issue couriers        |
| litellm       | `FLOOR_*_LLM_KEY`     | inference sparks from the tenants   |

Missing credentials degrade to "signal absent", shown honestly in the
sources panel — never an error page.

- `app.py` — aggregator + static server, stdlib only.
- `site/` — canvas renderer (`iso.js`) + generated sprite kit.
- `tools/gen_assets.py` — regenerates every SVG; see
  `site/assets/ASSETS.md` for the projection/palette grammar and the
  three-line recipe for giving a future service a bespoke room.

## The log panel

Clicking an agent-bay or task-yard room streams that container's logs from
docker-proxy. `app.py` forwards the container's bytes unchanged except for
collapsing repeated status-line repaints; the panel renders them with
**xterm.js**, so escape handling, colour, scrollback and reflow are a
maintained upstream's problem rather than ours.

The response is framed by connection close, not `Transfer-Encoding: chunked`:
`BaseHTTPRequestHandler` speaks HTTP/1.0, where chunked encoding does not
exist, so hand-written length prefixes land in the panel as literal text.

xterm.js is **not committed here**. `scripts/fetch-vendor.sh` stages it into
`site/vendor/` on the host from the version + integrity pins in
`manifest/floor.toml`, pulling from this node's own Forgejo package registry;
`scripts/deploy.sh` runs it before any image build. The Dockerfile only copies
what is already there and fails the build if it is missing, which keeps this
image an offline build with no registry credential in any layer.

To stage it by hand (and, before the node holds its own copy, to bootstrap
from upstream):

    ./scripts/fetch-vendor.sh floor
    VENDOR_REGISTRY=https://registry.npmjs.org ./scripts/fetch-vendor.sh floor

Local run:

    python3 app.py &
    APP_URL=http://localhost:8080 python3 tests/smoke.py

Tests:

    python3 tests/smoke.py       # the HTTP surface, against a running app
    python3 tests/stream.py      # log-stream filters, offline
