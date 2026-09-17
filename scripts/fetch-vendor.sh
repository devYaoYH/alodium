#!/usr/bin/env bash
# fetch-vendor.sh — stage browser-side dependencies into an app's build context.
#
# Reads manifest/*.toml files that contain [[vendor]] blocks. For each one,
# fetches the pinned package tarball, verifies its integrity hash, and unpacks
# the named files into the app's source tree.
#
# This runs on the HOST, before `docker compose build` — the same shape as
# build-mirrored.sh, and for the same reason. The alternative, fetching inside
# the Dockerfile, would need the node's CA and a registry credential baked into
# an image layer; doing it here keeps the app images offline builds with no
# secrets in them.
#
# The source of truth is this node's own Forgejo package registry, not the
# public one: the point of vendoring through the node is that a rebuild works
# when upstream is gone. Set VENDOR_REGISTRY to override (e.g. to bootstrap a
# package the node does not hold yet).
#
# Idempotent: a destination that already matches the pinned integrity is left
# alone, so deploys stay fast and work offline.
#
#   ./scripts/fetch-vendor.sh              # every app
#   ./scripts/fetch-vendor.sh floor        # one app
#
# Dependencies: curl, tar, python3 (TOML + hashing). No npm, no node.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] && set -a && source .env && set +a
: "${NODE_DOMAIN:=localhost}"

# Reading a package needs no credential when the registry owner is public;
# when it does, VENDOR_TOKEN is passed as a bearer. Never written to the image.
VENDOR_TOKEN="${VENDOR_TOKEN:-${FORGEJO_TOKEN:-}}"

toml_files=(manifest/*.toml)
if [[ $# -gt 0 ]]; then
  toml_files=()
  for app in "$@"; do toml_files+=("manifest/$app.toml"); done
fi

FETCHED=0
SKIPPED=0
FAILED=0

# One line per [[vendor]] block: app, package, version, integrity, registry,
# dest, then the file map as "src:dst" pairs.
while IFS=$'\t' read -r app package version integrity registry dest files; do
  [[ -n "$package" ]] || continue

  stamp="$dest/.vendor-$(printf '%s' "$package" | tr -c 'a-zA-Z0-9' '-')"
  if [[ -f "$stamp" ]] && [[ "$(cat "$stamp")" == "$version $integrity" ]]; then
    echo "   $app: $package@$version already staged"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Scoped names are percent-encoded in a registry path: @xterm/xterm ->
  # @xterm%2Fxterm, and the tarball basename drops the scope.
  enc_pkg=${package/\//%2F}
  base_name=${package##*/}
  url="$registry/$enc_pkg/-/$base_name-$version.tgz"

  echo "   $app: fetching $package@$version"
  tmp=$(mktemp -d)
  # shellcheck disable=SC2064  # expand $tmp now, not at trap time
  trap "rm -rf '$tmp'" RETURN

  auth=()
  [[ -n "$VENDOR_TOKEN" ]] && auth=(-H "Authorization: token $VENDOR_TOKEN")
  # -k: the node's own Forgejo presents the local CA's cert, which the host
  # may not trust yet on a fresh install. The integrity check below is what
  # actually secures this, not the transport.
  if ! curl -fsSLk "${auth[@]+"${auth[@]}"}" -o "$tmp/pkg.tgz" "$url"; then
    echo "   $app: FAILED to fetch $url" >&2
    echo "        publish it to the node's registry first (docs/MIRRORING.md)," >&2
    echo "        or set VENDOR_REGISTRY to bootstrap from upstream." >&2
    FAILED=$((FAILED + 1))
    continue
  fi

  actual=$(python3 - "$tmp/pkg.tgz" <<'PY'
import base64, hashlib, pathlib, sys
print("sha512-" + base64.b64encode(
    hashlib.sha512(pathlib.Path(sys.argv[1]).read_bytes()).digest()).decode())
PY
)
  if [[ "$actual" != "$integrity" ]]; then
    echo "   $app: INTEGRITY MISMATCH for $package@$version" >&2
    echo "        pinned:   $integrity" >&2
    echo "        received: $actual" >&2
    FAILED=$((FAILED + 1))
    continue
  fi

  tar xzf "$tmp/pkg.tgz" -C "$tmp"       # npm tarballs unpack under package/
  mkdir -p "$dest"
  ok=1
  for pair in $files; do
    src="$tmp/package/${pair%%:*}"
    dst="$dest/${pair##*:}"
    if [[ ! -f "$src" ]]; then
      echo "   $app: $package@$version has no ${pair%%:*}" >&2
      ok=0
      break
    fi
    cp "$src" "$dst"
  done
  if [[ "$ok" -ne 1 ]]; then FAILED=$((FAILED + 1)); continue; fi

  # Ship the licence beside the code — it is a redistribution, not a build dep.
  for cand in LICENSE LICENSE.md LICENSE.txt; do
    [[ -f "$tmp/package/$cand" ]] && cp "$tmp/package/$cand" "$dest/$base_name.LICENSE" && break
  done

  printf '%s %s\n' "$version" "$integrity" > "$stamp"
  FETCHED=$((FETCHED + 1))
done < <(python3 - "${toml_files[@]}" <<'PY'
import os, pathlib, sys, tomllib

domain = os.environ.get("NODE_DOMAIN", "localhost")
override = os.environ.get("VENDOR_REGISTRY", "")
for p in map(pathlib.Path, sys.argv[1:]):
    if not p.is_file() or p.name.endswith(".example.toml"):
        continue
    m = tomllib.loads(p.read_text())
    app = m.get("app", {}).get("name", p.stem)
    for v in m.get("vendor", []):
        registry = override or v["registry"].replace("${NODE_DOMAIN}", domain)
        files = " ".join(v["files"])
        print("\t".join([app, v["package"], v["version"], v["integrity"],
                         registry, v["dest"], files]))
PY
)

echo "fetch-vendor: $FETCHED fetched, $SKIPPED already staged, $FAILED failed"
exit $((FAILED > 0 ? 1 : 0))
