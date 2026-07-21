#!/usr/bin/env bash
# Thin launchd/cron entrypoint. The controller itself owns the PR trust checks.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec ./host/sut/sutctl.sh watch
