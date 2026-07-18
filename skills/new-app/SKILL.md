---
name: new-app
description: Stand up a brand-new service on this node from the app skeleton. Use when the operator asks to build or "up" a new service from scratch. For packaging an existing open-source project, use wrap-upstream instead.
---

# New app from the skeleton

Repo creation is an operator moment; everything after it is yours.

1. Ask the operator to run `./scripts/new-app.sh <name>` on the host
   (name: lowercase alnum + dashes). Your token cannot create repos — that
   is by design, not an obstacle to work around.
2. Clone it beside your node-config clone:
   `git clone http://forgejo:3000/apps/<name> /workspace/<name>`
3. Work the checklist in the app repo's README. The skeleton already
   satisfies every contract — fill it, don't fight it:
   - `app.toml`: every TODO; `[needs]` minimal — empty is a feature.
   - `openapi.yaml`: the real endpoints; delete the echo example.
   - `mcp-tools.json`: the agent-facing tools, or deliberately remove the
     `mcp` line from `app.toml` if this app has no agent surface.
   - `/healthz` stays, cheap and honest.
   - `tests/smoke.py`: one check per endpoint you add.
4. Verify locally before pushing:
   `python3 app.py & APP_URL=http://localhost:8080 python3 tests/smoke.py`
5. Open the app PR on `apps/<name>` (see the propose-change skill).
6. Register it on the node with the register-service skill — that is a
   separate PR against node-config, one concern each.
7. Any `[needs]` you declared: state the scope and why in the PR body.
   The operator mints credentials on merge, never before.
