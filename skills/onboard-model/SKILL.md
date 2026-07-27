---
name: onboard-model
description: Add or retire a model alias in the LiteLLM proxy — pinned pricing, capability flags, dispatch tier — as one node-config PR. Use whenever config/litellm.yaml's model_list or config/dispatch-tiers.yaml changes.
---

# Onboard a model

One PR, one concern: which models this node can call, and what they cost.
The blast radius is small and fixed — `config/litellm.yaml`, optionally
`config/dispatch-tiers.yaml` — but the failure mode is silent, so the
checklist is not optional.

**The thing that goes wrong**: LiteLLM prices a request by looking up
`litellm_params.model` VERBATIM in its cost map. That map is fetched fresh
from GitHub at startup (the container has egress; this is not a staleness
problem) but it carries almost no `openrouter/*` keys. A miss does not
error and does not warn — it bills $0.00, which quietly exempts the model
from `litellm_settings.max_budget`, and the spend ceiling stops being a
ceiling. OpenRouter's own `usage.cost` is read back only on NON-streaming
responses; agent traffic streams, so that fallback rescues nothing.

## Adding an alias

- **Alias name is the contract.** `model_name` is what consumers and
  `dispatch-tiers.yaml` reference; `litellm_params.model` is the provider
  route. Never change a `model_name` to fix pricing — the prefix
  (`openrouter/…` vs `deepseek/…`) selects the endpoint AND the API key, so
  rewriting it re-routes real traffic to a provider you may have no
  credential for.
- **Pin the price, always, for every `openrouter/*` entry.** Get the real
  numbers from the provider, never from a pricing page:

      ./scripts/model-pricing.sh fetch deepseek/deepseek-v4-flash

  It prints a paste-ready block. The values are already per-token, matching
  LiteLLM's `*_cost_per_token` unit — do not rescale. Pin
  `cache_read_input_token_cost` too when the model has a cache tier; at
  agent volume that term dominates. Put all of it in `litellm_params`, not
  `model_info` — `model_info` pricing is honored inconsistently across code
  paths, `litellm_params` is not.
- **Do not borrow another provider's map entry.** `model_info.base_model`
  can point pricing at `deepseek/deepseek-v4-flash` while routing stays on
  OpenRouter, and it works right up until either provider's rate moves —
  then you are mis-billing with no signal. Pin the rate you are actually
  charged.
- **Capabilities are per-entry** (`model_info: supports_vision: true` and
  friends). LiteLLM uses these for `/model/info` and to stop `drop_params`
  from silently stripping content blocks the model could have handled.
- **Behavior knobs belong to the alias, not the caller**: pin
  `reasoning_effort` in `litellm_params` so every consumer of the alias gets
  identical behavior. A different effort level is a different alias.
- **Tier it, or leave it unreferenced.** `config/dispatch-tiers.yaml` maps
  difficulty labels to a `model_name` + budget. Retiering is a deliberate
  cost/quality decision — argue it in the PR body, with the per-token rates
  you just pinned. `verify-config.sh` enforces that every tier model exists
  in `litellm.yaml`.

## Before you push

    ./scripts/verify-config.sh          # offline: pins exist, tiers resolve
    ./scripts/model-pricing.sh check    # network: pins match live pricing

The first is a hard gate on every config PR. The second is what catches a
provider changing rates under you — run it when onboarding, and whenever a
spend number looks implausible.

## Verifying after merge

The operator deploys (`scripts/deploy.sh`). Then confirm the model is
actually costed, because a zero here means uncosted, not free:

    docker exec litellm-db psql -U litellm -d litellm -c \
      "select model, count(*), sum(spend) from \"LiteLLM_SpendLogs\" \
       where model like 'openrouter/%' group by 1;"

Send one STREAMED request first — streaming is the path where the provider
cost fallback does not apply, so it is the case that must show `spend > 0`.

## Retiring an alias

Remove the `model_list` entry and every `dispatch-tiers.yaml` reference in
the same PR (leaving a dangling tier fails `verify-config.sh`). Historical
`LiteLLM_SpendLogs` rows keep the old model string — that is intended, spend
history is an audit record and is never rewritten to match current config.

PR body per the propose-change skill: blast radius, rollback, credentials.
A new provider means a new key: declare it by NAME in the PR
(`docker-compose.yml` env as `${PROVIDER_API_KEY:-}`) and let the operator
mint the value. You never place a key and never read one.
