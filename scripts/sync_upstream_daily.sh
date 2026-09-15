#!/usr/bin/env bash
# ==============================================================================
# scripts/sync_upstream_daily.sh
# Legacy wrapper pointing to the canonical manual upstream sync script.
# ==============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/sync_upstream.sh" "$@"
