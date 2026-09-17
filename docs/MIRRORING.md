# Caching upstreams: Forgejo mirrors and local forks

The node keeps its own synced copy of every external repository it depends
on — apps it runs, tools it builds against. Upstream deletion, rename, or
license rug-pull must never break a rebuild. Two structures, two rules:

| Structure | Where | Rule |
|---|---|---|
| Pull mirror | `mirrors/<name>` | read-only cache of upstream; Forgejo re-syncs on an interval; never push here |
| Local fork | `apps/<name>` | your modifiable copy; upstream flows IN via merge; you push freely |

## Prerequisites (once)

Run `./scripts/bootstrap-forgejo.sh`. It creates the operator admin, the
`node-config` repo (history pushed), and writes `FORGEJO_TOKEN` and
`AGENT_FORGEJO_TOKEN` into `.env` — no web installer, no manual token
clicking.

## Caching an upstream (the common case)

    ./scripts/mirror.sh https://github.com/usememos/memos
    ./scripts/mirror.sh https://github.com/dani-garcia/vaultwarden vaultwarden 12h0m0s

This creates `mirrors/<name>` as a pull mirror (default re-sync: daily).
Force a sync immediately:

    curl -X POST -H "Authorization: token $FORGEJO_TOKEN" \
      https://git.<domain>/api/v1/repos/mirrors/<name>/mirror-sync

## Agent-requested mirrors: proposal, human approval, deterministic import

An agent must not invoke `mirror.sh` itself. It files an issue in the private
coordination repository with the `mirror-request` label and this exact shape:

```text
title: mirror: useful-package

upstream: https://github.com/owner/useful-package.git
name: useful-package
interval: 24h0m0s
rationale: why the node needs this upstream
```

Run `./scripts/mirror-dispatcher.sh` from cron (or once by hand). It validates
the URL against `MIRROR_ALLOWED_HOSTS` (default: GitHub, GitLab, Codeberg),
then posts a SHA-256 request digest. The operator inspects the issue and
comments exactly `mirror: approve <digest>`. Only a comment authored by the
operator account, matching the current request digest, permits the host-side
runner to create the Forgejo pull mirror. Changing the issue body invalidates
the approval. The issue becomes the durable paper trail: requester, rationale,
operator approval, import output, and final `mirrors/<name>` location.

Suggested cron schedule:

```cron
*/10 * * * * cd /path/to/sovereign-node && ./scripts/mirror-dispatcher.sh >> /var/log/node-mirror-dispatch.log 2>&1
```

## Browser-side dependencies (npm packages, no build toolchain)

A git mirror is the wrong shape for a JavaScript library: the upstream repo
usually does not contain its own build output — xterm.js's `lib/xterm.js` is
404 at every tag — so mirroring the source would mean giving an app image a
Node stage and an `npm ci` over the upstream's whole dev toolchain, which is a
far bigger dependency than the library.

For these, the node holds the published tarball in its own **Forgejo package
registry** instead:

    # once, as operator (needs a token with write:package)
    curl -sL https://registry.npmjs.org/@xterm/xterm/-/xterm-6.0.0.tgz -o pkg.tgz
    curl -H "Authorization: token $FORGEJO_TOKEN" \
      --upload-file pkg.tgz \
      "https://git.$NODE_DOMAIN/api/packages/mirrors/npm/@xterm%2Fxterm/-/xterm-6.0.0.tgz"

The app declares what it needs in its manifest, pinned by version AND by the
package's sha512 integrity hash:

    [[vendor]]
    package   = "@xterm/xterm"
    version   = "6.0.0"
    integrity = "sha512-TQwDdQ..."
    registry  = "https://git.${NODE_DOMAIN}/api/packages/mirrors/npm"
    dest      = "apps/floor/site/vendor"
    files     = ["lib/xterm.js:xterm.js", "css/xterm.css:xterm.css"]

`scripts/fetch-vendor.sh` (run by `deploy.sh` before any image build) fetches
it, refuses to proceed on an integrity mismatch, and unpacks the named files
into the app's source tree. The staged files are gitignored: pinned and
verifiable in the manifest, never committed as a blob nobody can review.

The integrity hash, not the transport, is what secures this — which is why the
same script can bootstrap from upstream with `VENDOR_REGISTRY` before the node
holds its own copy, and get a byte-identical result.

## Working on a cached repo (download-direction, merge locally)

When you (or the agent) need to modify an app, fork the mirror into a
normal repo and track upstream as a remote:

    # one-time: create apps/<name> in Forgejo (non-mirror), then locally:
    git clone https://git.<domain>/apps/memos && cd memos
    git remote add upstream https://git.<domain>/mirrors/memos

    # pulling upstream updates (the typical flow):
    git fetch upstream
    git merge upstream/main        # resolve conflicts here, locally
    git push origin main

Conflict resolution happens on your machine (or in the agent's jail), never
on the mirror. The mirror stays a pristine image of upstream precisely so
merges have a clean base.

## Contributing back (rare, but on the horizon)

The mirror can't push. When a change is worth sending upstream:

    git checkout -b fix-thing upstream/main   # branch from clean upstream
    git cherry-pick <commits>                  # just the upstreamable part
    git push github fix-thing                  # your fork on the upstream's host
    # open the PR there

Keep the upstreamable branch free of node-specific patches; per the roadmap
policy — contribute commodity, keep moat.
