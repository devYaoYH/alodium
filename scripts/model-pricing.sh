#!/usr/bin/env bash
# OpenRouter pricing for config/litellm.yaml — fetch it, or check what's pinned.
#
#   ./scripts/model-pricing.sh fetch deepseek/deepseek-v4-flash
#   ./scripts/model-pricing.sh check
#
# Why this exists: LiteLLM prices a request by looking up litellm_params.model
# VERBATIM in its cost map, and that map carries almost no `openrouter/*` keys.
# An unmapped model bills $0.00 — it does not error, it does not warn, it just
# silently stops counting against litellm_settings.max_budget. OpenRouter's own
# usage.cost is only read back on non-streaming calls, and agent traffic
# streams, so the only reliable fix is to pin rates per deployment. This script
# is the way to get them right: OpenRouter's API returns per-token USD strings
# that drop straight into the config, with no rescaling to fumble.
#
# `fetch` prints a ready-to-paste block. `check` re-reads every pinned
# openrouter/* entry and diffs it against live pricing — run it when a provider
# changes rates, or when a spend number looks wrong.
#
# Needs network. verify-config.sh's offline gate only checks that pins EXIST;
# only this script can tell you whether they are CORRECT.
set -uo pipefail
cd "$(dirname "$0")/.."

API="https://openrouter.ai/api/v1/models"
CONFIG="config/litellm.yaml"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

fetch_all() {
  # Unauthenticated endpoint — the model catalogue is public.
  if ! curl -fsS --max-time 20 "$API"; then
    echo "model-pricing: could not reach $API (this host may have no egress;" >&2
    echo "               the litellm container does: docker exec litellm ...)" >&2
    exit 2
  fi
}

cmd_fetch() {
  local slug="${1:-}"
  [[ -n "$slug" ]] || usage
  fetch_all | python3 -c "
import sys, json
slug = sys.argv[1]
data = json.load(sys.stdin)['data']
hit = next((m for m in data if m['id'] == slug), None)
if hit is None:
    near = [m['id'] for m in data if slug.split('/')[-1][:8] in m['id']][:8]
    print(f'model-pricing: no such model on OpenRouter: {slug}', file=sys.stderr)
    if near:
        print('  did you mean: ' + ', '.join(near), file=sys.stderr)
    sys.exit(1)
p = hit['pricing']
# OpenRouter quotes per-token USD already — LiteLLM's *_cost_per_token fields
# use the same unit, so these copy across untouched. Rescaling is the classic
# way to be off by 1e6, so we deliberately do none.
print(f'''      # OpenRouter pricing, $(date -u +%Y-%m-%d) (scripts/model-pricing.sh check).
      input_cost_per_token: {p[\"prompt\"].rstrip(\"0\").rstrip(\".\") or 0}
      output_cost_per_token: {p[\"completion\"].rstrip(\"0\").rstrip(\".\") or 0}''')
cr = p.get('input_cache_read')
if cr and float(cr) > 0:
    print(f'      cache_read_input_token_cost: {cr.rstrip(\"0\").rstrip(\".\")}')
" "$slug"
}

cmd_check() {
  local live
  live=$(fetch_all) || exit 2
  printf '%s' "$live" | python3 -c "
import sys, json, yaml

live = {m['id']: m['pricing'] for m in json.load(sys.stdin)['data']}
cfg = yaml.safe_load(open('$CONFIG'))

FIELDS = [('input_cost_per_token', 'prompt'),
          ('output_cost_per_token', 'completion'),
          ('cache_read_input_token_cost', 'input_cache_read')]

bad = 0
for entry in cfg.get('model_list') or []:
    params = entry.get('litellm_params') or {}
    model = str(params.get('model', ''))
    if not model.startswith('openrouter/'):
        continue
    slug = model.split('/', 1)[1]
    name = entry.get('model_name', model)
    if slug not in live:
        print(f'  FAIL: {name}: {slug} is not on OpenRouter any more (retired? renamed?)')
        bad = 1
        continue
    for field, key in FIELDS:
        pinned = params.get(field)
        upstream = live[slug].get(key)
        upstream = float(upstream) if upstream not in (None, '') else None
        if pinned is None:
            # A missing cache-read pin is fine when upstream has no cache tier;
            # a missing prompt/completion pin is the \$0.00 bug itself.
            if key != 'input_cache_read' or (upstream or 0) > 0:
                print(f'  FAIL: {name}: {field} not pinned (upstream: {upstream})')
                bad = 1
            continue
        if upstream is None:
            print(f'  WARN: {name}: {field} pinned at {pinned}, upstream no longer quotes it')
            continue
        if abs(float(pinned) - upstream) > upstream * 1e-9:
            print(f'  FAIL: {name}: {field} pinned {pinned}, upstream {upstream}')
            bad = 1
if not bad:
    print('  OK: every openrouter/* entry matches live OpenRouter pricing')
sys.exit(bad)
"
}

case "${1:-}" in
  fetch) shift; cmd_fetch "$@" ;;
  check) shift; cmd_check "$@" ;;
  -h|--help|help) usage 0 ;;
  *) usage ;;
esac
