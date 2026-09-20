#!/usr/bin/env bash
# Encrypted backup of every node volume — restic, IN A CONTAINER.
# This box is your identity; a backup you haven't restored is a rumor.
#
#   ./scripts/backup.sh init     # once, after ~/.alodium/backup.env exists
#   ./scripts/backup.sh          # then schedule it (PR 4 owns scheduling)
#
# Why a container and not a host `restic`: this script used to derive host
# paths from `docker volume inspect … .Mountpoint`, i.e. /var/lib/docker/
# volumes/…. On Docker Desktop that path exists only inside the LinuxKit VM,
# so the `[[ -d "$t" ]]` filter dropped EVERY volume, restic snapshotted the
# pg dumps alone — and the script printed "backup complete". Same "reports ok,
# did nothing" class of bug as the old deploy.sh. Docker is the only thing
# that can read a named volume on every platform we care about (macOS,
# Windows/WSL2, Linux), so restic runs where the data is: inside a container,
# with each volume bind-mounted READ-ONLY at /data/<volume>.
#
# Nothing here may write to a production volume. Every data mount is :ro.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="sovereign-node"

# Pinned by digest like every other image on this node. This is a multi-arch
# manifest list (linux/amd64, arm64, arm, 386) — checked deliberately, because
# an arm64-only pin would break a restore onto an amd64 machine, which is the
# entire point of having backups (coordination#71; PR 6 audits the other pins).
RESTIC_IMAGE="restic/restic:0.19.0@sha256:7f44e0057b82348597568ea209360762d0b38f8e1dbc8ad859661ac1055e45f2"

# The node's host-side storage root. Outside the checkout on purpose: the
# checkout is replaceable, this is not. Overridable for drills and tests.
ALODIUM_HOME="${ALODIUM_HOME:-$HOME/.alodium}"

log()  { printf 'backup: %s\n' "$*"; }
warn() { printf 'backup: WARN %s\n' "$*" >&2; }
die()  { printf 'backup: ERROR %s\n' "$*" >&2; exit 1; }

# A run that ends anywhere but the last line must say so. The failure mode this
# script exists to prevent is "exited 0, backed up nothing", so success is an
# explicit flag rather than the absence of a complaint.
#
# The flag is not belt-and-braces: macOS ships bash 3.2, where some `set -u`
# aborts terminate the script with status ZERO. Without this, an unbound
# variable would look exactly like a clean backup to whatever scheduled it.
BACKUP_OK=0
cleanup() {
  local ec=$?
  if [[ -n "${STAGE_DIR:-}" ]]; then rm -rf "$STAGE_DIR"; fi
  if (( BACKUP_OK != 1 )); then
    if (( ec == 0 )); then
      printf 'backup: FAILED — ended early with a zero status (bash %s); reporting failure\n' "${BASH_VERSION%%(*}" >&2
      ec=1
    else
      printf 'backup: FAILED (exit %s)\n' "$ec" >&2
    fi
    exit "$ec"
  fi
}
trap cleanup EXIT

# --- preflight: the local layer ----------------------------------------------
# Deciding what to back up means parsing the compose tree, and compose needs the
# local layer git never sees: .env for the ${VARS}, secrets/<app>.env for every
# env_file. Run this from a git worktree and compose dies with
# "stat …/secrets/radicale.env: no such file or directory", which sends the
# reader hunting for a missing secret instead of telling them where they are.
for required in .env secrets; do
  if [[ ! -e "$required" ]]; then
    die "missing '$required' in $PWD — backup.sh needs the local layer (.env, secrets/) and must run from the live checkout, not a git worktree."
  fi
done

# --- configuration -----------------------------------------------------------
# ~/.alodium/backup.env is the documented home (mode 600: it names the repo and
# how to get the passphrase). scripts/backup.env stays supported as a fallback
# for the pre-~/.alodium layout; if both exist the ~/.alodium one wins and the
# other is called out rather than silently ignored.
ENV_FILE=""
for candidate in "$ALODIUM_HOME/backup.env" "scripts/backup.env"; do
  [[ -f "$candidate" ]] || continue
  if [[ -z "$ENV_FILE" ]]; then
    ENV_FILE="$candidate"
  else
    warn "ignoring $candidate — $ENV_FILE takes precedence"
  fi
done
[[ -n "$ENV_FILE" ]] \
  || die "no backup.env found. Copy scripts/backup.env.example to $ALODIUM_HOME/backup.env (chmod 600) and fill it in."
# shellcheck disable=SC1090
source "$ENV_FILE"

[[ -n "${RESTIC_REPOSITORY:-}" ]] || die "$ENV_FILE sets no RESTIC_REPOSITORY"

# Restic's cache. Lives under ~/.alodium/cache, never inside the repository and
# never inside a backed-up volume, so it can never end up in a snapshot.
CACHE_DIR="${RESTIC_CACHE_DIR:-$ALODIUM_HOME/cache/restic}"

# --- the repository ----------------------------------------------------------
# A local-directory repository (the P0 destination) is a host path we bind-mount
# at /repo. Anything with a backend prefix (b2:, s3:, sftp:, rest:) is passed
# through untouched, with its provider credentials forwarded explicitly below.
REPO_IS_LOCAL=0
REPO_DIR=""
case "$RESTIC_REPOSITORY" in
  /*|\~/*|./*|../*)
    REPO_IS_LOCAL=1
    REPO_DIR="${RESTIC_REPOSITORY/#\~/$HOME}"
    ;;
  *:*) REPO_IS_LOCAL=0 ;;
  *)   die "RESTIC_REPOSITORY='$RESTIC_REPOSITORY' is neither an absolute path nor a restic backend URL" ;;
esac

# --- the passphrase ----------------------------------------------------------
# Resolved on the HOST and handed to the container as a mounted file, because
# the password command reads the macOS Keychain — `security` exists here, not
# in the restic image. The passphrase never reaches argv and never reaches the
# container's environment (where `docker inspect` would print it).
STAGE_DIR="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/alodium-backup.XXXXXX")"
chmod 700 "$STAGE_DIR"
PASS_FILE="$STAGE_DIR/restic-pass"

resolve_passphrase() {
  if [[ -n "${RESTIC_PASSWORD_COMMAND:-}" ]]; then
    eval "$RESTIC_PASSWORD_COMMAND"
  elif [[ -n "${RESTIC_PASSWORD_FILE:-}" ]]; then
    cat "${RESTIC_PASSWORD_FILE/#\~/$HOME}"
  elif [[ -n "${RESTIC_PASSWORD:-}" ]]; then
    printf '%s\n' "$RESTIC_PASSWORD"
  else
    die "$ENV_FILE supplies no passphrase — set RESTIC_PASSWORD_COMMAND (preferred), RESTIC_PASSWORD_FILE, or RESTIC_PASSWORD"
  fi
}
( umask 077; resolve_passphrase > "$PASS_FILE" ) \
  || die "passphrase lookup failed: ${RESTIC_PASSWORD_COMMAND:-<file/env>}"
[[ -s "$PASS_FILE" ]] \
  || die "passphrase resolved to nothing. For the Keychain path, create the item first: security add-generic-password -a \"\$USER\" -s alodium-restic -w"

# --- how restic is invoked ---------------------------------------------------
# BASE_MOUNTS is everything restic needs to reach its repository. DATA_MOUNTS
# (built later) is the read-only production data, and only the `backup` call
# gets it — `init` and `forget` never see a production volume at all.
BASE_MOUNTS=(-v "$PASS_FILE:/secrets/restic-pass:ro" -v "$CACHE_DIR:/cache")
RESTIC_ENV=(-e "RESTIC_PASSWORD_FILE=/secrets/restic-pass" -e "RESTIC_CACHE_DIR=/cache")
if (( REPO_IS_LOCAL )); then
  BASE_MOUNTS+=(-v "$REPO_DIR:/repo")
  RESTIC_ENV+=(-e "RESTIC_REPOSITORY=/repo")
else
  RESTIC_ENV+=(-e "RESTIC_REPOSITORY=$RESTIC_REPOSITORY")
  # Forward only the credentials restic's backends read; never the whole
  # environment. An unset name is skipped rather than passed through empty.
  for v in B2_ACCOUNT_ID B2_ACCOUNT_KEY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY \
           AWS_DEFAULT_REGION AWS_SESSION_TOKEN RESTIC_REST_USERNAME RESTIC_REST_PASSWORD; do
    if [[ -n "${!v:-}" ]]; then RESTIC_ENV+=(-e "$v=${!v}"); fi
  done
fi

restic_run() {  # restic_run <args...> — repository only, no node data mounted
  docker run --rm "${BASE_MOUNTS[@]}" "${RESTIC_ENV[@]}" "$RESTIC_IMAGE" "$@"
}

mkdir -p "$ALODIUM_HOME" "$CACHE_DIR"
chmod 700 "$ALODIUM_HOME"

# --- init --------------------------------------------------------------------
# Creating the repository is an explicit, operator-typed step. Nothing on the
# normal path creates one: `restic backup` against a missing repository must
# fail, not quietly start a second, empty history next to the real one.
if [[ "${1:-}" == "init" ]]; then
  if (( REPO_IS_LOCAL )); then mkdir -p "$REPO_DIR"; fi
  restic_run init
  log "repository initialized: $RESTIC_REPOSITORY"
  BACKUP_OK=1
  exit 0
fi

if (( REPO_IS_LOCAL )) && [[ ! -f "$REPO_DIR/config" ]]; then
  die "no restic repository at $REPO_DIR — run ./scripts/backup.sh init first"
fi

# --- what gets backed up -----------------------------------------------------
# Core volumes: the stack itself. Not optional, not profile-gated; a missing
# one is a hard failure.
CORE_VOLUMES=(
  forgejo_data      # node-config, coordination, every issue and PR
  litellm_db        # virtual keys and spend history
  radicale_data     # calendars and contacts
  caddy_data        # the internal CA — restoring it is how TLS survives
  pocketid_data     # identity: SQLite + passkey public halves
)

# App volumes: GENERATED from each app manifest's [lifecycle].backup — the
# manifest is the inventory (DESIGN.md). Add an app, declare its backup, and
# this list follows; nothing to remember.
DECLARED=(${CORE_VOLUMES[@]+"${CORE_VOLUMES[@]}"})
while IFS= read -r vol; do
  [[ -n "$vol" ]] || continue
  DECLARED+=("$vol")
done < <(python3 - <<'PY'
import pathlib, tomllib
for p in sorted(pathlib.Path("manifest").glob("*.toml")):
    if p.name.endswith(".example.toml"):
        continue
    for v in tomllib.loads(p.read_text()).get("lifecycle", {}).get("backup", []):
        print(v.removeprefix("sovereign-node_"))
PY
)
# Order-preserving dedupe: radicale_data is both core and manifest-declared.
# shellcheck disable=SC2207  # volume names are shell-safe by construction
DECLARED=($(printf '%s\n' ${DECLARED[@]+"${DECLARED[@]}"} | awk '!seen[$0]++'))

# Which volumes does the compose tree actually define, and which service mounts
# each one? Profile-gated services are invisible to a bare `compose config`, so
# enumerate ALL profiles — same trick, same reason, as deploy.sh step 4b.
ALL_PROFILES=$(
  (grep -h "profiles:" docker-compose.yml apps/*/compose.yaml 2>/dev/null || true) \
    | grep -o '\[.*\]' | tr -d '[]' | tr ',' '\n' | tr -d ' "' | sort -u | paste -sd, -
)
[[ -n "$ALL_PROFILES" ]] || ALL_PROFILES=$(docker compose config --profiles 2>/dev/null | paste -sd, - || true)

COMPOSE_JSON="$STAGE_DIR/compose.json"
COMPOSE_ERR="$STAGE_DIR/compose.err"
if ! COMPOSE_PROFILES="$ALL_PROFILES" docker compose config --format json >"$COMPOSE_JSON" 2>"$COMPOSE_ERR"; then
  cat "$COMPOSE_ERR" >&2
  die "docker compose config failed — refusing to guess what to back up"
fi

# volume -> the services that mount it. A declared volume that no service mounts
# is a manifest that has drifted from the compose tree, and drift is exactly how
# a volume silently stops being backed up. Fatal, always.
VOLUME_OWNERS="$STAGE_DIR/volume-owners.tsv"
python3 - "$COMPOSE_JSON" > "$VOLUME_OWNERS" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
owners = {name: [] for name in cfg.get("volumes", {})}
for svc, spec in sorted(cfg.get("services", {}).items()):
    for m in spec.get("volumes", []) or []:
        if m.get("type") == "volume" and m.get("source") in owners:
            owners[m["source"]].append(svc)
for name, svcs in sorted(owners.items()):
    print(f"{name}\t{','.join(svcs)}")
PY

# Services with a container in this project — running, exited or merely created.
# This is "has this app's profile ever run", and it is what separates "not
# installed yet, skip" from "installed, and its data is GONE".
RAN_SERVICES="$STAGE_DIR/ran-services.txt"
docker compose ps -a --services > "$RAN_SERVICES" 2>/dev/null || : > "$RAN_SERVICES"

volume_owners()   { awk -F'\t' -v v="$1" '$1==v {print $2}' "$VOLUME_OWNERS"; }
volume_in_compose() { awk -F'\t' -v v="$1" '$1==v {f=1} END {exit !f}' "$VOLUME_OWNERS"; }
service_has_run() { grep -qx "$1" "$RAN_SERVICES"; }

VOLUMES=()   # full docker volume names, in snapshot order
SKIPPED=()
MISSING=()
for vol in ${DECLARED[@]+"${DECLARED[@]}"}; do
  full="${PROJECT}_${vol}"

  if ! volume_in_compose "$vol"; then
    MISSING+=("$full — declared for backup, but no compose service defines it (manifest drift)")
    continue
  fi

  if docker volume inspect "$full" >/dev/null 2>&1; then
    VOLUMES+=("$full")
    continue
  fi

  # The volume does not exist. Either the app has never run (fine — say so out
  # loud), or it has run and its data is missing (never fine).
  owners="$(volume_owners "$vol")"
  ran=""
  IFS=',' read -r -a owner_list <<< "$owners"
  for svc in ${owner_list[@]+"${owner_list[@]}"}; do
    [[ -n "$svc" ]] || continue
    if service_has_run "$svc"; then ran="$svc"; break; fi
  done
  if [[ -n "$ran" ]]; then
    MISSING+=("$full — service '$ran' has run on this node, but the volume does not exist")
  else
    SKIPPED+=("$full (profile never run: ${owners:-no service})")
  fi
done

for s in ${SKIPPED[@]+"${SKIPPED[@]}"}; do log "skip    $s"; done
if (( ${#MISSING[@]} )); then
  for m in ${MISSING[@]+"${MISSING[@]}"}; do printf 'backup: ERROR missing volume %s\n' "$m" >&2; done
  die "${#MISSING[@]} declared volume(s) missing — refusing to take a snapshot that pretends they are backed up"
fi
if (( ${#VOLUMES[@]} == 0 )); then
  die "computed volume set is empty — that is the bug this script was rewritten to make impossible, not a backup"
fi
for v in ${VOLUMES[@]+"${VOLUMES[@]}"}; do log "include $v"; done

# --- consistent DB snapshots -------------------------------------------------
# Dump Postgres rather than trusting a copy of live files. The list is still
# hardcoded here; moving it into each app manifest — and adding the missing
# redash and egress-audit dumps, `pg_dump -Fc`, and SQLite consistency — is
# PR 2 of coordination#71. This table is the seam it replaces: PR 2 generates
# these rows instead of spelling them out.
#
#   container | compose service | pg user | database | dump file
DUMP_SPECS=(
  "litellm-db|litellm-db|litellm|litellm|litellm.sql"
  "miniflux-db|miniflux-db|miniflux|miniflux|miniflux.sql"
  "search-audit-db|search-audit-db|search_audit_owner|search_audit|search_audit.sql"
)

# Dumps stage in a private mktemp -d (700) removed by the EXIT trap, so a crash
# no longer leaves LiteLLM keys and user data readable in /tmp. The umask makes
# the dump files themselves 600, which is also the mode a restore replays.
umask 077
DUMP_DIR="$STAGE_DIR/dumps"
mkdir -p "$DUMP_DIR"
chmod 700 "$DUMP_DIR"

RUNNING_CONTAINERS="$STAGE_DIR/running.txt"
docker ps --format '{{.Names}}' > "$RUNNING_CONTAINERS"

# Three outcomes, and the distinction is the point. The operator stops Docker
# to reclaim RAM, so a partially-up node is a normal state here, not an edge
# case — and a dump that is missing because its database was down must never
# ride inside a snapshot that reports success.
#
#   running                          -> dump it
#   has run on this node, but down   -> DEGRADED: snapshot anyway, exit non-zero
#   never run on this node           -> clean skip, same as the volume logic
DUMPED=()
DUMP_SKIPPED=()
DEGRADED=()
for spec in "${DUMP_SPECS[@]}"; do
  IFS='|' read -r d_container d_service d_user d_db d_file <<< "$spec"
  if grep -qx "$d_container" "$RUNNING_CONTAINERS"; then
    if docker exec "$d_container" pg_dump -U "$d_user" "$d_db" > "$DUMP_DIR/$d_file"; then
      DUMPED+=("$d_file")
    else
      # A half-written dump is worse than none: it would restore as a truncated
      # database and look like data.
      rm -f "$DUMP_DIR/$d_file"
      DEGRADED+=("$d_db — pg_dump failed against the running container '$d_container'")
    fi
  elif service_has_run "$d_service"; then
    DEGRADED+=("$d_db — service '$d_service' has run on this node but its container is not running")
  else
    DUMP_SKIPPED+=("$d_db (service '$d_service' has never run)")
  fi
done

for s in ${DUMP_SKIPPED[@]+"${DUMP_SKIPPED[@]}"}; do log "skip    dump $s"; done
log "dumps   ${DUMPED[*]:-<none>}"
for d in ${DEGRADED[@]+"${DEGRADED[@]}"}; do
  printf 'backup: DEGRADED dump missing: %s\n' "$d" >&2
done

# --- the snapshot ------------------------------------------------------------
# Each volume is mounted READ-ONLY at a predictable /data/<volume>, so a path in
# the snapshot names the volume it restores into. The dumps ride along at
# /data/dumps.
DATA_MOUNTS=()
TARGETS=()
for v in "${VOLUMES[@]}"; do
  DATA_MOUNTS+=(-v "$v:/data/$v:ro")
  TARGETS+=("/data/$v")
done
DATA_MOUNTS+=(-v "$DUMP_DIR:/data/dumps:ro")
TARGETS+=("/data/dumps")

# A degraded run still takes the snapshot — partial data beats no data when the
# operator actually needs a restore — but it is tagged so it can be told apart
# from a complete one without reading a log.
SNAPSHOT_TAGS=(--tag "$PROJECT")
if (( ${#DEGRADED[@]} )); then SNAPSHOT_TAGS+=(--tag partial); fi

# --host is not cosmetic: without it restic records the container's random
# hostname, every snapshot lands in its own retention group, and `forget` never
# expires anything. A fixed name also survives a restore onto another machine.
docker run --rm "${BASE_MOUNTS[@]}" "${DATA_MOUNTS[@]}" "${RESTIC_ENV[@]}" "$RESTIC_IMAGE" \
  backup "${TARGETS[@]}" --host "$PROJECT" "${SNAPSHOT_TAGS[@]}" --exclude-caches

# Retention never runs on a degraded run: expiring a complete snapshot to make
# room for a partial one is exactly the wrong trade. Nothing is deleted, the
# partial snapshot stays for the restore it might be needed for, and the run
# exits non-zero so whatever scheduled it knows.
if (( ${#DEGRADED[@]} )); then
  log "retention skipped — nothing was expired"
  die "degraded run: snapshot taken and tagged 'partial', ${#DEGRADED[@]} dump(s) missing (listed above). Volumes are backed up; the databases above are not."
fi

# Retention. Scoped to this repository by construction — the container sees only
# the configured repo — and grouped by host alone: one host, one backup job, one
# history. Grouping by paths or tags would fragment it every time an app is added
# or a run is tagged 'partial', leaving groups that are each too young to expire.
restic_run forget --host "$PROJECT" --tag "$PROJECT" --group-by host \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune

BACKUP_OK=1
# BSD date (macOS) has no -Is; deploy.sh spells it out the same way.
log "complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)  volumes=${#VOLUMES[@]} skipped=${#SKIPPED[@]} dumps=${#DUMPED[@]}"

# Restore drill (quarterly, minimum). PR 5 of coordination#71 turns this into
# scripts/restore.sh + docs/RESTORE.md, drilled in the SUT VM, never against
# the production project. Until then, to look at what is in the repository:
#   docker run --rm -v ~/.alodium/backups/restic:/repo -e RESTIC_REPOSITORY=/repo \
#     restic/restic snapshots
# Then update manifest/node.yaml -> backups.tested_restore with today's date.
