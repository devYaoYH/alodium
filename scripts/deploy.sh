#!/usr/bin/env bash
# The deterministic deploy step — deliberately host-side, deliberately dumb.
# The agent proposes (PR), you merge on Forgejo, THIS applies to the running
# node. Operator-triggered by design: merge = authorization, this = apply.
#
#   ./scripts/deploy.sh
#
# Why it pulls FORGEJO, not origin: agent PRs merge on the Forgejo node-config
# repo (the tree the jail clones). GitHub `origin` is the public template and
# lags until we mirror to it. The old `git pull` pulled origin and so deployed
# NOTHING after an agent PR merged — the merge was never on the branch it pulled.
set -euo pipefail
cd "$(dirname "$0")/.."

# 1. Bring the merged tree into the working checkout FROM FORGEJO (where the PR
#    merged), fast-forward only — a divergence is an operator decision, not a
#    silent merge commit from a deploy script. Remember where we started: the
#    OLD_HEAD..HEAD diff drives the config-restart pass in step 6.
OLD_HEAD=$(git rev-parse HEAD)
git fetch forgejo main
if ! git merge --ff-only forgejo/main; then
  echo "deploy: local main and forgejo/main have diverged — reconcile by hand, then re-run." >&2
  exit 1
fi

# 2. Mirror the now-merged main back to GitHub origin, IF an `origin` remote is
#    configured. The public GitHub repo is frozen for now (hackathon rules), so
#    `origin` has been removed from local tracking and this step no-ops quietly
#    rather than WARN-ing on every deploy. Re-add the remote to resume mirroring:
#    `git remote add origin git@github.com:devYaoYH/alodium.git`.
#    (sync-node-config pushes the other way; forgejo remains the deploy source.)
if git remote get-url origin >/dev/null 2>&1; then
  git push origin main || echo "deploy: WARN could not push origin (continuing; node is already at merged main)"
else
  echo "   skipping origin mirror (no 'origin' remote configured)"
fi

# 3. Refresh derived secrets (e.g. RADICALE_WEB_AUTH) before compose reads .env.
./scripts/derive-secrets.sh

# 3b. Mint per-app credentials the merged tree now expects: scaffold any missing
#     secrets/<app>.env (a missing env_file aborts the WHOLE compose up before
#     any container starts), auto-generate blank `# mint:` secrets, and flag any
#     operator-owed `# require:` ones. Idempotent — only ever fills blanks.
./scripts/mint-secrets.sh

# 3c. Ensure difficulty:* labels exist in the coordination repo (idempotent).
#     These are the per-issue model-routing knobs for issue-work dispatch.
#     Safe to re-run: the script checks for label existence via the API
#     before creating (Forgejo does NOT deduplicate by name).
./scripts/ensure-tier-labels.sh || echo "deploy: WARN ensure-tier-labels.sh failed (non-fatal; labels may need manual creation)"

# 4. Build any mirrored images that are missing (idempotent: existing images
#    are skipped). This happens before compose up so the image reference in
#    the compose fragment resolves. Only touches apps with a [build] section
#    in their manifest.
./scripts/build-mirrored.sh

# 4b. Rebuild locally-built images whose build inputs changed in this merge.
#     'docker compose up -d' (step 5) does NOT rebuild an existing image, so a
#     merged Dockerfile or build-context change would otherwise never reach the
#     running container — exactly what stranded the launcher compose-plugin fix
#     (image kept the pre-fix build; `docker compose` was absent, so every
#     launch 500'd). Diff-driven so deploys stay fast: only apps whose baked-in
#     files changed get rebuilt; step 5 then recreates them (compose recreates
#     on image-id change, so no --build needed there). CHANGED is computed once
#     here and reused by the restart pass in step 6 — OLD_HEAD..HEAD is fixed
#     after the ff-merge above.
CHANGED=$(git diff --name-only "$OLD_HEAD" HEAD)
# Enumerate services across ALL declared profiles, not just the enabled ones.
# `docker compose config --services` filters to active profiles, so a
# profile-gated app (e.g. on-demand snake) is INVISIBLE to the gate below —
# its rebuild gets skipped, and step 5 then recreates it from the STALE image
# (recreate with no new image = no change; exactly what stranded snake's fixes).
# `--profiles` lists every profile defined anywhere; feeding them all back via
# COMPOSE_PROFILES makes both `config --services` and `build` see the whole set.
# This only affects the BUILD pass — naming a service builds just that image and
# starts nothing, so the operator still owns which profiles actually run (step 5
# is unchanged and only recreates services that are already running).
ALL_PROFILES=$(docker compose config --profiles 2>/dev/null | paste -sd, -)
BUILDABLE=$(COMPOSE_PROFILES="$ALL_PROFILES" docker compose config --services 2>/dev/null)
for app in $(printf '%s\n' "$CHANGED" | sed -n 's#^apps/\([^/]*\)/.*#\1#p' | sort -u); do
  # Build inputs = context files baked into the image; exclude compose/proxy
  # metadata (same exclusion the step-6 restart pass uses). No baked-in file
  # changed -> nothing to rebuild.
  printf '%s\n' "$CHANGED" | grep "^apps/$app/" \
    | grep -qvE "^apps/$app/(compose\.yaml|route\.caddy|env\.example)$" || continue
  # Only services that actually build from a context; compose build errors on
  # image-only services, so gate on the app being a known compose service
  # (across all profiles, per BUILDABLE above).
  if printf '%s\n' "$BUILDABLE" | grep -qx "$app"; then
    echo "   rebuilding $app image (build inputs changed)"
    COMPOSE_PROFILES="$ALL_PROFILES" docker compose build "$app" \
      || echo "deploy: WARN build failed for $app (continuing; step 5 uses existing image)"
  fi
done

# 5. Apply: recreate any service whose spec changed (env/image/etc).
#    Two passes, because "which profiles are enabled" is the OPERATOR's call,
#    not this script's: first the core plane (default profile), then every
#    profile-gated service that is CURRENTLY RUNNING — naming a service
#    explicitly auto-enables its profile, and `ps --services` lists running
#    project containers regardless of profile flags. Deploy recreates what
#    runs; it never starts a profile the operator hasn't enabled. (The old
#    `--profile apps` here silently skipped feeds/chat/authshim services —
#    miniflux kept stale env across deploys.)
docker compose up -d --remove-orphans
RUNNING=$(docker compose ps --services)
if [[ -n "$RUNNING" ]]; then
  # shellcheck disable=SC2086  # word-splitting the service list is the point
  docker compose up -d $RUNNING
fi

# SSO has one host-side source of truth: Pocket ID's client callbacks and the
# local-dev compose override are derived by sso-setup.sh.  A merged browser
# surface or door/proxy change therefore must refresh that state before its
# first visit.  This is deliberately conditional: the setup touches the IdP,
# so unrelated deploys do not perform external configuration work.
if printf '%s\n' "$CHANGED" | grep -qE '^(scripts/sso-setup\.sh|docker-compose\.yml|caddy/Caddyfile|apps/[^/]+/(compose\.yaml|route\.caddy))$'; then
  echo "   refreshing SSO wiring (callback or proxy configuration changed)"
  ./scripts/sso-setup.sh
fi

# 6. Bind-mounted CONTENT changes don't recreate containers — compose only
#    diffs the service spec. Caddy gets a validated reload (routes are its
#    config); any app whose apps/<name>/ files changed beyond compose.yaml/
#    route.caddy (e.g. radicale's `config` file, read once at startup) gets a
#    restart so the process actually re-reads what the merge changed.
if docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile && echo "   caddy reloaded"
else
  echo "deploy: WARN caddy config failed validation — NOT reloading (fix the route, re-run)"
fi

# CHANGED was computed in step 4b (OLD_HEAD..HEAD is fixed after the ff-merge).
for app in $(printf '%s\n' "$CHANGED" | sed -n 's#^apps/\([^/]*\)/.*#\1#p' | sort -u); do
  if printf '%s\n' "$CHANGED" | grep -q "^apps/$app/" \
     && printf '%s\n' "$CHANGED" | grep "^apps/$app/" | grep -qvE "^apps/$app/(compose\.yaml|route\.caddy|env\.example)$"; then
    for svc in $(printf '%s\n' "$RUNNING" | grep -E "^$app(-|$)" || true); do
      echo "   restarting $svc (mounted config changed in apps/$app/)"
      docker compose restart "$svc"
    done
  fi
done
# Same class, core plane: litellm reads config/litellm* only at startup.
if printf '%s\n' "$CHANGED" | grep -q "^config/litellm"; then
  echo "   restarting litellm (config/litellm* changed)"
  docker compose restart litellm
fi
# Same class, core plane: homepage reads config/homepage/* (custom.css/js,
# services.yaml, etc.) at startup only.
if printf '%s\n' "$CHANGED" | grep -q "^config/homepage/"; then
  echo "   restarting homepage (config/homepage/* changed)"
  docker compose restart homepage
fi

docker compose ps --format 'table {{.Name}}\t{{.Status}}'

# 7. Record deployment info for the homepage deploy-info widget.
#     Writes a JSON artifact that the homepage serves at /static/deploy-info.json
#     containing the current timestamp and deployed commit hash, hyperlinked to
#     the commit in the node-config Forgejo repo.
DEPLOY_INFO="config/homepage/static/deploy-info.json"
mkdir -p "$(dirname "$DEPLOY_INFO")"
NODE_DOMAIN="${NODE_DOMAIN:-localhost}"
NODE_CONFIG_REPO="${NODE_CONFIG_REPO:-operator/node-config}"
COMMIT=$(git rev-parse HEAD)
SHORT=$(git rev-parse --short HEAD)
cat > "$DEPLOY_INFO" <<DEPLOY_EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "commit": "${COMMIT}",
  "short_hash": "${SHORT}",
  "url": "https://git.${NODE_DOMAIN}/${NODE_CONFIG_REPO}/commit/${COMMIT}"
}
DEPLOY_EOF
echo "   deploy-info recorded (${SHORT} at $(date -u +%Y-%m-%dT%H:%M:%SZ))"
