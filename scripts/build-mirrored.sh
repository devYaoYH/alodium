#!/usr/bin/env bash
# build-mirrored.sh — generic image build for mirrored upstream apps.
#
# Reads manifest/*.toml files that contain a [build] section. For each such
# app, if the declared image does not exist locally, clones the mirror repo at
# the pinned ref and runs docker build with the declared args + tag.
#
# Idempotent: an existing image is skipped, so deploys stay fast.
# Intended to be called by scripts/deploy.sh before the compose up step.
#
# Usage:
#   ./scripts/build-mirrored.sh            # build all missing images
#   ./scripts/build-mirrored.sh calino     # build only calino
#
# Dependencies: git, docker, python3 (for TOML parsing + env expansion).
set -euo pipefail
cd "$(dirname "$0")/.."

# Source .env so ${VAR} references in build arg values expand correctly.
# The mirror.sh sibling uses the same pattern.
[[ -f .env ]] && set -a && source .env && set +a

BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT

BUILT=0
SKIPPED=0

# Helper script path (separate .py file to avoid bash interpreting ${VAR}).
PARSE_SCRIPT="scripts/_build_mirrored_parse.py"

# Collect the TOML files to process.
toml_files=(manifest/*.toml)
if [[ $# -gt 0 ]]; then
  toml_files=()
  for app in "$@"; do
    toml_files+=("manifest/${app}.toml")
  done
fi

for toml in "${toml_files[@]}"; do
  [[ -f "$toml" ]] || continue

  # Run the Python parsing helper. It outputs one tab-separated line:
  #   image\trepo\tref\targ1\targ2\t...
  # and exits with a code we dispatch on:
  #   0 = build needed, 2 = no [build], 3 = missing fields, 4 = invalid ref.
  OUTPUT=$(python3 "$PARSE_SCRIPT" "$toml" 2>&1) || true
  EXIT_CODE=$?

  # Exit code 2 = no [build] section, skip silently.
  if [[ "$EXIT_CODE" -eq 2 ]]; then
    continue
  fi
  # Exit code 3 = missing required field, skip with a note.
  if [[ "$EXIT_CODE" -eq 3 ]]; then
    echo "  build-mirrored: skipping $toml (missing image, repo, or ref)"
    continue
  fi
  # Exit code 4 = invalid ref (message already on stderr).
  if [[ "$EXIT_CODE" -eq 4 ]]; then
    continue
  fi
  # Non-zero for any other reason — show Python's output and abort.
  if [[ "$EXIT_CODE" -ne 0 ]]; then
    echo "$OUTPUT"
    exit "$EXIT_CODE"
  fi

  # Parse the tab-separated output.  image\trepo\tref\targ1\targ2\t...
  # ARGS_REST captures everything after the first 3 fields (may be empty).
  IFS=$'\t' read -r IMAGE REPO REF ARGS_REST <<<"$OUTPUT"

  echo "  build-mirrored: $IMAGE ($REPO @ $REF)"

  # Idempotent: skip if image already exists.
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "    image exists, skipping"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  echo "    cloning $REPO @ $REF ..."

  # Operator review blocker 2: git clone --depth 1 --branch <sha> fails for
  # a commit SHA.  Go straight to init + fetch-by-SHA + checkout.
  CLONE_DIR="$BUILD_DIR/$REPO"
  mkdir -p "$CLONE_DIR"
  git init "$CLONE_DIR" >/dev/null 2>&1
  git -C "$CLONE_DIR" remote add origin "https://git.localhost/$REPO.git"
  git -C "$CLONE_DIR" fetch origin "$REF" --depth 1 2>&1 | sed 's/^/    /'
  git -C "$CLONE_DIR" checkout "$REF" 2>&1 | sed 's/^/    /'

  echo "    building $IMAGE ..."

  # Build the docker command with an array (bash 3.2 compatible:
  # read -a works, only readarray/mapfile don't).  Split ARGS_REST on
  # tabs into an array, then loop to add --build-arg for each.
  BUILD_CMD=(docker build)
  if [[ -n "$ARGS_REST" ]]; then
    IFS=$'\t' read -r -a ARG_ARR <<<"$ARGS_REST"
    for a in "${ARG_ARR[@]}"; do
      BUILD_CMD+=(--build-arg "$a")
    done
  fi
  BUILD_CMD+=(-t "$IMAGE" "$CLONE_DIR")

  "${BUILD_CMD[@]}" 2>&1 | sed 's/^/    /'

  echo "    built $IMAGE"
  BUILT=$((BUILT + 1))
done

echo "  build-mirrored: $BUILT built, $SKIPPED skipped"