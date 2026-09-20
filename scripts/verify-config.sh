#!/usr/bin/env bash
# Thin wrapper. The gate lives in scripts/verify_config.py and scripts/node_verify/.
#
#   ./scripts/verify-config.sh            # validate everything
#
# This file stays so docs, tasks/issue-work.md, the skills and every habit that
# learned the old path keep working. It carries no logic on purpose: this is
# the pre-push gate every agent must pass, and a gate is worth exactly the
# confidence that it still bites. Each section is now a function over parsed
# data with offline tests for its pass AND fail branch
# (scripts/node_verify/test_*.py), instead of 550 lines of bash wrapping seven
# heredoc'd Python programs that could only be checked by breaking the repo on
# purpose.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/verify_config.py "$@"
