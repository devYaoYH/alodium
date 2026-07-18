---
name: propose-change
description: Open a well-formed PR against node-config or apps/* — branch discipline, blast radius, rollback plan. Use for every change you push on this node; other skills reference this one for the final step.
---

# Propose a change

Your single write path is branch → push → PR. The operator's merge is the
authorization moment; nothing you do is applied until then.

- Branch from current `main`; never push to `main`, never push to
  `mirrors/*`.
- One concern per PR. If a task needs an app change and a node-config
  change, that is two PRs that reference each other.
- The PR body states, in this order:
  1. **What changes** — one paragraph, plain language.
  2. **Blast radius** — every service, volume, route, and ring touched;
     "none" is a valid and common answer, say it explicitly.
  3. **Rollback** — normally `git revert` + redeploy; if anything more is
     needed (a volume restore, a re-mint), say so — that is a smell worth
     flagging.
  4. **Credentials required** — each scope and why, minted by the operator
     on merge. Missing credential? Name it here; do not work around it.
- Destructive operations (deletions, migrations, anything touching user
  data) ship as reviewable scripts with a dry-run mode inside the PR,
  never as actions taken during your session.
- After merge, deploy belongs to the operator (`scripts/deploy.sh`). Do
  not simulate deploy access, and do not ask for it.
