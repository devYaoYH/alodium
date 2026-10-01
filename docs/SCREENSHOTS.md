# README screenshot notes

Captured October 1, 2026 with the Codex in-app browser against the local
deployment. Images are JPEG browser captures stored in `docs/images/`; they
contain no fabricated conversations or seeded personal data.

| Image | Source | Visible example / privacy treatment |
|---|---|---|
| `home.jpg` | `https://home.localhost/#home` | App shortcuts and shared prompt bar. The Today section was collapsed to exclude calendar, feed, and handoff summaries, then restored after capture. |
| `workshop.jpg` | `https://home.localhost/#workshop` | Navigation and proposal, handoff, and co-pilot controls. Cropped above live proposal/handoff titles and outside the spending widgets. |
| `operations.jpg` | `https://home.localhost/#operations` | Operator service shortcuts and the start of the container status view. Cropped to omit the deployment footer and keep the table clear of the floating prompt bar. |
| `tenant-boundaries.jpg` | `https://home.localhost/#security` | Role descriptions only. Cropped before identity and authentication events; the displayed resident-tenant allowance is part of the configured role description. |
| `floor-pathways.jpg` | `https://floor.localhost/` | The LLM gateway room selected, with declared connections and service status. No task logs or agent histories were opened. |

Each saved image was visually inspected for names, email addresses, account
avatars, credentials, personal calendar/note/chat content, private work titles,
and request identifiers. Crops omit private content rather than blurring it.
Generic service names and local service health remain visible.

The search audit, notes, and chat entry endpoints were also inspected. The
audit contains real request history and was excluded; no search detail,
personal note, or conversation capture is included. No application data,
credentials, or security settings were changed for these illustrations.

These images document one running node, not the health of every installation.
Stopped, unknown, or unwired services retain their real labels. The Floor's
declared paths visualize manifest relationships; they are not packet traces
or a separate verification of network enforcement.

When refreshing the gallery, inspect each view after its widgets have loaded,
exclude live personal content before capture, and review the saved image
itself. Prefer app controls and direct browser screenshot crops; keep raw
captures containing private content out of the repository. Use relative image
links in the root README so the gallery renders on GitHub.
