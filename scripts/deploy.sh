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

# --- deploy-info surfacing ---------------------------------------------------
# The homepage lower-left "deployed" stamp reads config/homepage/static/
# deploy-info.json. We record not just the deployed commit but the OUTCOME:
# a `status` ("ok" | "warning" | "failed") and any WARN/error `messages`, so a
# broken deploy shows a clickable badge on the dashboard instead of failing
# silently. Warnings are collected via record_msg (below, replacing the old
# `|| echo "deploy: WARN ..."` sites); hard aborts under `set -e` are caught by
# the ERR trap, which flags the deploy failed with the failing command.
#
# Two commits live in that file: `commit` is what THIS run deployed or tried
# to (the widget links it), and `deployed_commit` is the last commit that
# deployed successfully (ok or warning). scripts/deploy-watch.sh keys off
# `deployed_commit`, so a failed run is retried instead of looking deployed.
DEPLOY_INFO="config/homepage/static/deploy-info.json"
DEPLOY_MSGS="$(mktemp)"
trap 'rm -f "$DEPLOY_MSGS"' EXIT

# record_msg <LEVEL> <text...> — echo to the console AND stash for deploy-info.
record_msg() {
  local level="$1"; shift
  echo "deploy: ${level} $*" >&2
  printf '%s\t%s\n' "$level" "$*" >> "$DEPLOY_MSGS"
}

# write_deploy_info <status> — (re)write the JSON artifact the homepage reads.
# Renders the collected messages as a JSON array via python3 (safe escaping).
write_deploy_info() {
  local status="$1"
  mkdir -p "$(dirname "$DEPLOY_INFO")"
  local domain="${NODE_DOMAIN:-localhost}"
  local repo="${NODE_CONFIG_REPO:-operator/node-config}"
  local commit short
  commit=$(git rev-parse HEAD)
  short=$(git rev-parse --short HEAD)
  DEPLOY_STATUS="$status" \
  DEPLOY_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  DEPLOY_COMMIT="$commit" DEPLOY_SHORT="$short" \
  DEPLOY_URL="https://git.${domain}/${repo}/commit/${commit}" \
  DEPLOY_MSGS_FILE="$DEPLOY_MSGS" \
  DEPLOY_PREV_FILE="$DEPLOY_INFO" \
  python3 - > "$DEPLOY_INFO.tmp" <<'PY'
import json, os
status = os.environ["DEPLOY_STATUS"]
if status == "failed":
    # Carry the last successful deploy forward. Files written before
    # `deployed_commit` existed count their `commit` only if that run succeeded.
    try:
        with open(os.environ["DEPLOY_PREV_FILE"]) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    if "deployed_commit" in prev:
        deployed = prev["deployed_commit"] or ""
    else:
        deployed = prev.get("commit", "") if prev.get("status") != "failed" else ""
else:
    deployed = os.environ["DEPLOY_COMMIT"]
msgs = []
try:
    with open(os.environ["DEPLOY_MSGS_FILE"]) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            level, _, text = line.partition("\t")
            msgs.append({"level": level, "text": text})
except FileNotFoundError:
    pass
print(json.dumps({
    "timestamp": os.environ["DEPLOY_TS"],
    "commit": os.environ["DEPLOY_COMMIT"],
    "short_hash": os.environ["DEPLOY_SHORT"],
    "url": os.environ["DEPLOY_URL"],
    "status": status,
    "deployed_commit": deployed,
    "messages": msgs,
}, indent=2))
PY
  mv "$DEPLOY_INFO.tmp" "$DEPLOY_INFO"
}

# On any unhandled failure under `set -e`, flag the deploy failed and record the
# failing command before the shell exits. `trap - ERR` first so a failure inside
# write_deploy_info can't re-enter this handler.
on_error() {
  local ec=$?
  trap - ERR
  record_msg ERROR "step failed (exit ${ec}): ${BASH_COMMAND}"
  write_deploy_info failed || true
}
trap on_error ERR

# 1. Bring the merged tree into the working checkout FROM FORGEJO (where the PR
#    merged), fast-forward only — a divergence is an operator decision, not a
#    silent merge commit from a deploy script. Remember where we started: the
#    OLD_HEAD..HEAD diff drives the config-restart pass in step 6.
OLD_HEAD=$(git rev-parse HEAD)
git fetch forgejo main
if ! git merge --ff-only forgejo/main; then
  trap - ERR
  record_msg ERROR "local main and forgejo/main have diverged — reconcile by hand, then re-run."
  write_deploy_info failed || true
  exit 1
fi

# 2. Mirror the now-merged main back to GitHub origin, IF an `origin` remote is
#    configured. The public GitHub repo is frozen for now (hackathon rules), so
#    `origin` has been removed from local tracking and this step no-ops quietly
#    rather than WARN-ing on every deploy. Re-add the remote to resume mirroring:
#    `git remote add origin git@github.com:devYaoYH/alodium.git`.
#    (sync-node-config pushes the other way; forgejo remains the deploy source.)
if git remote get-url origin >/dev/null 2>&1; then
  git push origin main || record_msg WARN "could not push origin (continuing; node is already at merged main)"
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
./scripts/ensure-tier-labels.sh || record_msg WARN "ensure-tier-labels.sh failed (non-fatal; labels may need manual creation)"

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
# Extract all profiles from compose files (apps/*/compose.yaml + root) and pass
# them to config/build so profile-gated services are visible. Fallback to a
# default set if extraction fails (silent parse errors are not acceptable here).
# This only affects the BUILD pass — naming a service builds just that image and
# starts nothing, so the operator still owns which profiles actually run (step 5
# is unchanged and only recreates services that are already running).
get_all_profiles() {
  # Extract all unique profile names from compose files (including on-demand services)
  (grep -h "profiles:" docker-compose.yml apps/*/compose.yaml 2>/dev/null || true) \
    | grep -o '\[.*\]' \
    | tr -d '[]' \
    | tr ',' '\n' \
    | tr -d ' "' \
    | sort -u \
    | paste -sd, -
}
ALL_PROFILES=$(get_all_profiles)
if [[ -z "$ALL_PROFILES" ]]; then
  # Fallback: if profile extraction failed, try docker compose (may miss on-demand)
  ALL_PROFILES=$(docker compose config --profiles 2>/dev/null | paste -sd, - || echo "on-demand")
fi
echo "   rebuilding with profiles: $ALL_PROFILES"
# This is also the first full parse of the merged compose tree, so a broken
# file stops the deploy HERE — with compose's own error in the log and in
# deploy-info, not a bare "step failed (exit 1)" (#93's duplicate keys hid
# behind a 2>/dev/null at exactly this line).
COMPOSE_ERR=$(mktemp)
if ! BUILDABLE=$(COMPOSE_PROFILES="$ALL_PROFILES" docker compose config --services 2>"$COMPOSE_ERR"); then
  cat "$COMPOSE_ERR" >&2
  record_msg ERROR "docker compose config failed; no containers were changed: $(grep -v '^[[:space:]]*$' "$COMPOSE_ERR" | tail -1)"
  rm -f "$COMPOSE_ERR"
  write_deploy_info failed || true
  exit 1
fi
rm -f "$COMPOSE_ERR"
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
      || record_msg WARN "build failed for $app (continuing; step 5 uses existing image)"
  else
    echo "   skipping $app rebuild (not found in BUILDABLE services)"
  fi
done

# 4c. Build locally-built images that don't exist yet (across all profiles).
#     Mirrors the idempotent-missing-image pattern from build-mirrored.sh
#     (step 4). Catches new on-demand apps whose image was never built
#     (e.g. first deploy after the PR that added them) and recovers from
#     pruned images. Only processes services that have a build context;
#     `docker compose build` is a no-op when the image already exists.
COMPOSE_JSON=$(COMPOSE_PROFILES="$ALL_PROFILES" docker compose config --format json 2>/dev/null || true)
if [[ -n "$COMPOSE_JSON" ]]; then
  python3 -c "
import json, os, sys
cfg = json.loads(sys.stdin.read())
for name, svc in cfg.get('services', {}).items():
    build = svc.get('build')
    if not build:
        continue
    image = (svc.get('image') or '').strip()
    if not image:
        continue
    if os.system(f'docker image inspect {image} >/dev/null 2>&1') == 0:
        continue
    print(name)
" <<<"$COMPOSE_JSON" 2>/dev/null | while read -r app; do
    echo "   building $app (missing local image — step 4c)"
    COMPOSE_PROFILES="$ALL_PROFILES" docker compose build "$app" \
      || record_msg WARN "build failed for $app (continuing; launcher will build on demand)"
  done
fi

# 4d. Rebuild the agent jail image when agent/ changed. Tenants run from
#     sovereign-node/agent:local (scripts/run-task.sh; the agent/assistant
#     compose services), but nothing above rebuilds it — 4b only covers apps/*
#     and 4c only builds a MISSING image — so merged jail fixes never reached
#     dispatched runs (the image once sat two months stale). Build a candidate,
#     gate it on the jail smoke test, and only then move :local. A failed build
#     or test keeps the current :local and flags the deploy "warning".
if printf '%s\n' "$CHANGED" | grep -q '^agent/'; then
  AGENT_CANDIDATE="sovereign-node/agent:candidate"
  AGENT_BUILD_LOG=$(mktemp)
  echo "   rebuilding agent jail image (agent/ changed) -> $AGENT_CANDIDATE"
  if docker build -t "$AGENT_CANDIDATE" ./agent >"$AGENT_BUILD_LOG" 2>&1 \
     && JAIL_IMAGE="$AGENT_CANDIDATE" ./scripts/test-jail-image.sh --no-build; then
    docker tag "$AGENT_CANDIDATE" sovereign-node/agent:local
    echo "   agent jail image passed its smoke test -> sovereign-node/agent:local"
  else
    tail -20 "$AGENT_BUILD_LOG" >&2
    record_msg WARN "agent jail image build or smoke test failed — kept the existing sovereign-node/agent:local (see deploy log)"
  fi
  # Drops the candidate tag; a promoted image lives on as :local.
  docker image rm "$AGENT_CANDIDATE" >/dev/null 2>&1 || true
  rm -f "$AGENT_BUILD_LOG"
fi

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
# On-demand apps (restart: "no") don't appear in RUNNING, so they won't be
# recreated above even if their image changed. Track which apps were rebuilt in
# step 4b and explicitly recreate them here (up -d is idempotent, but on-demand
# apps won't auto-launch; this just ensures the container spec is refreshed).
REBUILT_APPS=""
for rebuilt_app in $(printf '%s\n' "$CHANGED" | sed -n 's#^apps/\([^/]*\)/.*#\1#p' | sort -u); do
  printf '%s\n' "$CHANGED" | grep "^apps/$rebuilt_app/" \
    | grep -qvE "^apps/$rebuilt_app/(compose\.yaml|route\.caddy|env\.example)$" || continue
  if printf '%s\n' "$BUILDABLE" | grep -qx "$rebuilt_app"; then
    REBUILT_APPS="$REBUILT_APPS $rebuilt_app"
  fi
done
if [[ -n "$REBUILT_APPS" ]]; then
  echo "   recreating rebuilt on-demand apps:$REBUILT_APPS"
  # shellcheck disable=SC2086  # word-splitting is intentional
  COMPOSE_PROFILES="$ALL_PROFILES" docker compose up -d $REBUILT_APPS || true
fi

# SSO has one host-side source of truth: Pocket ID's client callbacks and the
# local-dev compose override are derived by sso-setup.sh.  A merged browser
# surface or door/proxy change therefore must refresh that state before its
# first visit.  This is deliberately conditional: the setup touches the IdP,
# so unrelated deploys do not perform external configuration work.
if printf '%s\n' "$CHANGED" | grep -qE '^(scripts/sso-setup\.sh|docker-compose\.yml|caddy/Caddyfile|apps/[^/]+/(compose\.yaml|route\.caddy))$'; then
  echo "   refreshing SSO wiring (callback or proxy configuration changed)"
  # Keep sso-setup's reason (e.g. an expired POCKET_ID_API_KEY) in deploy-info.
  SSO_ERR=$(mktemp)
  if ! ./scripts/sso-setup.sh 2>"$SSO_ERR"; then
    cat "$SSO_ERR" >&2
    record_msg ERROR "sso-setup.sh failed: $(grep -v '^[[:space:]]*$' "$SSO_ERR" | tail -1)"
    rm -f "$SSO_ERR"
    write_deploy_info failed || true
    exit 1
  fi
  cat "$SSO_ERR" >&2
  rm -f "$SSO_ERR"
fi

# 6. Bind-mounted CONTENT changes don't recreate containers — compose only
#    diffs the service spec. Caddy gets a validated reload (routes are its
#    config); any app whose apps/<name>/ files changed beyond compose.yaml/
#    route.caddy (e.g. radicale's `config` file, read once at startup) gets a
#    restart so the process actually re-reads what the merge changed.
if docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile && echo "   caddy reloaded"
else
  record_msg WARN "caddy config failed validation — NOT reloading (fix the route, re-run)"
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
#     Any WARN collected along the way (build failures, skipped SSO, caddy
#     validation) downgrades the status to "warning" so the dashboard shows the
#     badge; a clean run is "ok". Hard aborts are handled by the ERR trap above.
DEPLOY_FINAL_STATUS=ok
if [[ -s "$DEPLOY_MSGS" ]]; then
  DEPLOY_FINAL_STATUS=warning
fi
write_deploy_info "$DEPLOY_FINAL_STATUS"
echo "   deploy-info recorded (status=${DEPLOY_FINAL_STATUS}, $(git rev-parse --short HEAD) at $(date -u +%Y-%m-%dT%H:%M:%SZ))"
