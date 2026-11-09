#!/usr/bin/env bash
# Delegate to the installed GitHub Ops workflow: main mirrors upstream,
# dev owns linear development and serving, and local is retired.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_DIR="${HERMES_AGENT_DIR:-$REPO_ROOT}"
GITHUB_CI_DIR="${GITHUB_CI_DIR:-$(dirname "$REPO_ROOT")/prmartinow/github-ci}"
SYNC_SCRIPT="${GITHUB_CI_DIR}/scripts/sync_hermes.sh"
if [[ ! -f "$SYNC_SCRIPT" ]]; then
    echo 'GitHub Ops sync script not found. Set GITHUB_CI_DIR to the installed github-ci checkout.' >&2
    exit 1
fi
export HERMES_DIR
exec bash "$SYNC_SCRIPT" "$@"
