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

Local run:

    python3 app.py &
    APP_URL=http://localhost:8080 python3 tests/smoke.py
