#!/usr/bin/env bash
# Thin launchd/cron entrypoint. The controller itself owns the PR trust checks:
# it tests only heads the operator asked for with the `requires-sut` label.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec ./host/sut/sutctl.sh dispatch
