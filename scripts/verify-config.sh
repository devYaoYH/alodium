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

echo
if [[ "$FAIL" -eq 0 ]]; then echo "verify-config: PASS"; else echo "verify-config: FAIL (fix the above before pushing)"; fi
exit "$FAIL"
