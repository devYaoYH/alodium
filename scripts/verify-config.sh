#!/usr/bin/env bash
# Offline config verification — a pure text check, no daemon, no data, no
# network. Runs the same validations the operator runs when reviewing a config
# PR, so the agent can self-check BEFORE pushing (the jail image carries the
# caddy binary + shellcheck for exactly this). Also runnable by the operator
# and by CI.
#
#   ./scripts/verify-config.sh            # validate everything
#
# What it checks:
#   1. The FULL assembled Caddyfile (root Caddyfile + every apps/*/route.caddy)
#      adapts+validates — with dummy env values, since validation is about
#      syntax/structure, not real secrets. This is what catches header_up
#      misplacement, bad matchers, brace nesting, etc.
#   2. YAML parses (docker-compose.yml, apps/*/compose.yaml, config/*.yaml).
#   3. Shell scripts pass shellcheck (syntax + common bugs).
# Exit non-zero on the first failure; prints what failed and where.
set -uo pipefail
cd "$(dirname "$0")/.."
FAIL=0
note() { printf '  %s\n' "$1"; }
sec()  { printf '\n== %s ==\n' "$1"; }

# --- 1. Caddy: assemble the whole door and validate ------------------------
sec "caddy validate (full assembled Caddyfile)"
if ! command -v caddy >/dev/null 2>&1; then
  note "SKIP: no caddy binary here (present in the jail image; install caddy to run this locally)"
else
  TD=$(mktemp -d)
  cp -r caddy "$TD/caddy"
  cp -r apps  "$TD/apps"
  # The root Caddyfile imports app routes by ABSOLUTE path
  # (import /srv/apps/*/route.caddy — where prod mounts them). In this temp
  # tree they live at $TD/apps, so rewrite the import to point there —
  # otherwise the glob matches nothing, the routes are silently skipped, and
  # a broken route.caddy validates as "OK" against an empty door. (This is the
  # subtle trap: without this, the linter passes broken configs.)
  # portable in-place (GNU sed -i and BSD sed -i differ) — rewrite via temp.
  sed "s#/srv/apps/#$TD/apps/#g" "$TD/caddy/Caddyfile" > "$TD/Caddyfile.tmp" \
    && mv "$TD/Caddyfile.tmp" "$TD/caddy/Caddyfile"
  printf 'NODE_DOMAIN=localhost\nACME_EMAIL=op@example.com\nEXTRA_TRUSTED_RANGES=192.0.2.0/32\nRADICALE_WEB_AUTH=ZHVtbXk6ZHVtbXk=\nRADICALE_OPERATOR_EMAIL=op@example.com\n' > "$TD/envfile"
  # Dummy env so interpolation resolves; validation is about structure, not
  # real values.
  if caddy validate --config "$TD/caddy/Caddyfile" --adapter caddyfile \
        --envfile "$TD/envfile" >/tmp/vc_caddy.log 2>&1
  then
    note "OK: config adapts and validates"
  else
    note "FAIL: caddy validate errored —"
    grep -viE "using config|maintenance|shutting down|^\{.*level.:.info" /tmp/vc_caddy.log | sed 's/^/    /' | tail -12
    FAIL=1
  fi
  rm -rf "$TD"
fi

# --- 2. YAML parse ----------------------------------------------------------
sec "yaml parse"
YAML_FILES=$(ls docker-compose.yml apps/*/compose.yaml config/*.yaml 2>/dev/null)
for f in $YAML_FILES; do
  if python3 -c "import sys,yaml; list(yaml.safe_load_all(open(sys.argv[1])))" "$f" 2>/tmp/vc_yaml.log; then
    :
  else
    note "FAIL: $f —"; sed 's/^/    /' /tmp/vc_yaml.log | tail -4; FAIL=1
  fi
done
[[ "$FAIL" -eq 0 ]] && note "OK: all YAML parses" || true

# --- 3. Dispatch tiers reconciled with litellm ---------------------------------
sec "dispatch tiers vs litellm"
if [[ -f "config/dispatch-tiers.yaml" ]]; then
  python3 -c "
import sys, yaml, json

with open('config/dispatch-tiers.yaml') as f:
    tiers = yaml.safe_load(f)
with open('config/litellm.yaml') as f:
    llm = yaml.safe_load(f)

llm_models = {m['model_name'] for m in (llm.get('model_list') or [])}
tier_models = {t['model'] for t in (tiers.get('tiers', {})).values()}
unknown = tier_models - llm_models

if unknown:
    print('FAIL: tier models not in litellm.yaml: ' + ', '.join(sorted(unknown)))
    sys.exit(1)
else:
    print('OK: all tier models (' + ', '.join(sorted(tier_models)) + ') are in litellm.yaml')
    sys.exit(0)
" 2>/tmp/vc_tiers.log
  if [[ $? -ne 0 ]]; then
    note "FAIL: dispatch-tiers.yaml references models not in litellm.yaml —"; sed 's/^/    /' /tmp/vc_tiers.log; FAIL=1
  else
    note "OK: tier models exist in litellm.yaml"
  fi
else
  note "SKIP: no config/dispatch-tiers.yaml (not yet deployed)"
fi

# --- 4. Label definitions valid (quoting/encoding) -------------------------
sec "label definitions"
if [[ -f "scripts/ensure-tier-labels.sh" ]]; then
  if bash "scripts/ensure-tier-labels.sh" --verify >/tmp/vc_labels.log 2>&1; then
    note "OK: all label definitions parse correctly"
  else
    note "FAIL: label definition errors —"; sed 's/^/    /' /tmp/vc_labels.log; FAIL=1
  fi
else
  note "SKIP: no scripts/ensure-tier-labels.sh"
fi

# --- 5. Shell lint ----------------------------------------------------------
sec "shellcheck"
if ! command -v shellcheck >/dev/null 2>&1; then
  note "SKIP: no shellcheck here (present in the jail image)"
else
  SH=$(git ls-files 'scripts/*.sh' 'host/**/*.sh' 2>/dev/null || ls scripts/*.sh)
  # -S error: only fail the gate on errors, not style warnings (the existing
  # scripts predate this and use intentional patterns).
  if shellcheck -S error $SH >/tmp/vc_sh.log 2>&1; then
    note "OK: no shellcheck errors"
  else
    note "FAIL: shellcheck errors —"; sed 's/^/    /' /tmp/vc_sh.log | head -20; FAIL=1
  fi
fi

# --- 6. Copilot containment invariants -------------------------------------
# The copilot seat is a capable, subscription-backed Claude Code session. Its
# safety rests on a hard code/data-plane split: it may reach ONLY its door
# (front), the agent spur (agents) and its egress proxy (copilot-egress), and
# its ONLY internet path is that proxy. A config change that puts it on a
# data-plane network, on `edge` directly, or hands it the docker socket / a
# secrets mount would silently dismantle that boundary — this catches it before
# it can be proposed, let alone merged.
sec "copilot containment"
if [[ -f "apps/copilot/compose.yaml" ]]; then
  python3 - <<'PY' 2>/tmp/vc_copilot.log
import sys, yaml

cfg = yaml.safe_load(open('apps/copilot/compose.yaml'))
svcs = cfg.get('services', {})
errs = []

def nets(name):
    n = svcs.get(name, {}).get('networks', []) or []
    return set(n.keys()) if isinstance(n, dict) else set(n)

# The load-bearing boundary: reach front (door), agents (spur) and its egress
# proxy — nothing else. Any other network, above all a data-plane net or `edge`
# (direct internet), breaks the separation the whole design rests on.
ALLOWED = {'front', 'agents', 'copilot-egress'}
extra = nets('copilot') - ALLOWED
if extra:
    errs.append(f"copilot joins forbidden network(s) {sorted(extra)} "
                f"(allowed: {sorted(ALLOWED)}) — data-plane reach / direct "
                f"internet must never be granted to the copilot.")
if 'edge' in nets('copilot'):
    errs.append("copilot must NOT join `edge` — its only egress is via copilot-egress.")

# Only the egress companion may bridge to the internet, and only paired with its
# private link to the copilot — exactly the search-egress shape.
EGRESS_ALLOWED = {'copilot-egress', 'edge'}
e_extra = nets('copilot-egress') - EGRESS_ALLOWED
if e_extra:
    errs.append(f"copilot-egress joins unexpected network(s) {sorted(e_extra)} "
                f"(allowed: {sorted(EGRESS_ALLOWED)}).")

# No host socket, no secrets mount — node maintenance only, no host control.
for v in svcs.get('copilot', {}).get('volumes', []) or []:
    src = (v.split(':', 1)[0] if isinstance(v, str) else v.get('source', '')).strip()
    if 'docker.sock' in src:
        errs.append("copilot mounts the docker socket — forbidden (no host control).")
    if src == 'secrets' or src.startswith('./secrets') or src.startswith('/'):
        if 'COPILOT.md' not in (v if isinstance(v, str) else ''):
            errs.append(f"copilot mounts host path {src!r} — only ./COPILOT.md (ro) is allowed.")

# The egress allowlist default must stay Anthropic-owned: a widened default here
# would quietly turn the one controlled hole into general internet access. The
# value is a `${VAR:-<default>}` string — pull out the default and require EVERY
# entry to be an Anthropic domain (anthropic.com = API, claude.com = auth plane).
import re
ANTHROPIC_DOMAINS = ('anthropic.com', 'claude.com')
egress_env = svcs.get('copilot-egress', {}).get('environment', {}) or {}
raw = str(egress_env.get('EGRESS_ALLOW', ''))
m = re.search(r':-([^}]*)\}', raw)          # ${VAR:-<default>} -> <default>
default = m.group(1) if m else raw
entries = [e.strip().lstrip('.').lower() for e in default.split(',') if e.strip()]
if not entries:
    errs.append("copilot-egress EGRESS_ALLOW has no default allowlist.")
for e in entries:
    if not any(e == d or e.endswith('.' + d) for d in ANTHROPIC_DOMAINS):
        errs.append(f"copilot-egress EGRESS_ALLOW default entry {e!r} is not an "
                    f"Anthropic-owned host ({'/'.join(ANTHROPIC_DOMAINS)}) — the "
                    f"egress must stay Anthropic-only.")

if errs:
    for e in errs:
        sys.stderr.write('  - ' + e + '\n')
    sys.exit(1)
PY
  if [[ $? -ne 0 ]]; then
    note "FAIL: copilot containment violated —"; sed 's/^/    /' /tmp/vc_copilot.log; FAIL=1
  else
    note "OK: copilot reaches only front/agents/egress; no socket, secrets, or data-plane net"
  fi
else
  note "SKIP: no apps/copilot/compose.yaml"
fi

# --- 7. Build provenance — every first-party image must be reproducible -------
sec "build provenance"
# Scans manifest/*.toml for sovereign-node/ images that have no version tag, no
# [build] section, and no build: context in their app's compose fragment — the
# classic "runs once from a hand-built :local image, then silently ossifies" trap.
# Exempt: env-var references, @sha256:-pinned images, and non-sovereign-node/
# upstream images (those are reproducible by digest/tag upstream).
python3 - <<'PY' 2>/tmp/vc_build.log
import os, sys, tomllib, yaml, re

errors = []
skip_example = True  # app.example.toml is not a real app

# Same rule _build_mirrored_parse.py uses — build-mirrored.sh rejects anything
# that isn't a 40-char hex SHA, so verify-config must agree pre-merge or a
# bad ref passes the gate and only blows up at deploy time.
SHA_RE = re.compile(r"[0-9a-f]{40}")

manifest_dir = 'manifest'
apps_dir = 'apps'

for entry in sorted(os.listdir(manifest_dir)):
    if not entry.endswith('.toml'):
        continue
    if skip_example and entry == 'app.example.toml':
        continue

    path = os.path.join(manifest_dir, entry)
    with open(path, 'rb') as f:
        try:
            m = tomllib.load(f)
        except Exception as e:
            errors.append(f'{entry}: failed to parse TOML — {e}')
            continue

    app = m.get('app', {})
    name = app.get('name', '')
    image = app.get('image', '')

    if not image or not name:
        continue

    # Skip env-var references (e.g. GOG_BRIDGE_IMAGE)
    if image.startswith('$') or '${' in image:
        continue

    # Skip images pinned by digest
    if '@sha256:' in image:
        continue

    # Skip non-first-party images (not from the sovereign-node org)
    if 'sovereign-node/' not in image:
        continue

    # Check tag: extract the part after the last colon
    tag = image.split(':')[-1] if ':' in image else None
    # A versioned tag (e.g. :0.6.0, :0.22.7) is fine — the operator
    # manages it. Flag :local, no tag, or empty tag.
    is_floating = (tag is None) or (tag == '') or (tag == 'local')

    if not is_floating:
        # Has a concrete version tag — not in the floating-trap class
        continue

    # Check for [build] section
    build = m.get('build')
    has_build = bool(build)

    # If [build] is declared, [build].ref MUST be a 40-char hex SHA — the same
    # rule scripts/_build_mirrored_parse.py enforces at deploy time. A
    # mis-pinned ref used to sail through verify-config (this script only
    # checked provenance, not the ref format), which is how a stray HEAD
    # literal for egress-broker reached review. Catch it here so the agent /
    # operator sees it before pushing.
    if has_build and isinstance(build, dict):
        ref = build.get('ref', '')
        if not SHA_RE.fullmatch(ref or ''):
            errors.append(
                f'{entry}: [build].ref "{ref}" is not a 40-char hex SHA — '
                f'pin it to a real commit (or remove the [build] section and '
                f'use compose `build: .` instead, like floor).'
            )
            continue

    # Check for build: context in apps/<name>/compose.yaml
    has_compose_build = False
    compose_path = os.path.join(apps_dir, name, 'compose.yaml')
    if os.path.isfile(compose_path):
        with open(compose_path) as f:
            try:
                compose_data = yaml.safe_load(f)
            except Exception:
                compose_data = None
        if compose_data and isinstance(compose_data, dict):
            services = compose_data.get('services', {}) or {}
            for svc in services.values():
                if isinstance(svc, dict) and 'build' in svc:
                    has_compose_build = True
                    break

    if has_build:
        # [build] section exists (and ref validated above) — provenance is
        # declared
        continue

    if has_compose_build:
        # compose fragment has build: context — image is built locally
        continue

    errors.append(
        f'{entry}: image "{image}" is a first-party (sovereign-node/) image '
        f'with no version tag, no [build] section, and no build: context in '
        f'apps/{name}/compose.yaml — this image cannot be reproduced. '
        f'Add a [build] section with pinned repo+ref, or add build: context '
        f'to the compose fragment.'
    )

if errors:
    for e in errors:
        print(f'  FAIL: {e}', file=sys.stderr)
    sys.exit(1)
else:
    print('OK: every first-party image has build provenance')
PY
if [[ $? -ne 0 ]]; then
  note "FAIL: build provenance errors —"; sed 's/^/    /' /tmp/vc_build.log; FAIL=1
else
  note "OK: all first-party images have build provenance"
fi

echo
if [[ "$FAIL" -eq 0 ]]; then echo "verify-config: PASS"; else echo "verify-config: FAIL (fix the above before pushing)"; fi
exit "$FAIL"
