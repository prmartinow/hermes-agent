#!/usr/bin/env bash
# ==============================================================================
# Hermes Agent Container Release & Lifecycle Automation CLI
#
# Provides streamlined workflows for building container images, deploying to the
# single serving container (Port :9119), inspecting status, and managing releases.
# ==============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LEDGER_FILE="$REPO_ROOT/docs/CONTAINER_VERSIONS.md"

usage() {
  cat <<EOF
Usage: $(basename "$0") <command> [arguments]

Commands:
  build <version> [description]   Build container image with BuildKit and inject Git metadata
  deploy <version>                Deploy/restart the single serving container (port :9119)
  promote <version>               Alias for deploy (promotes image to serving container)
  status                          Display active container, image versions, and health
  rollback <version>              Rollback serving container to a previous image version
  ledger                          Display the container version ledger (docs/CONTAINER_VERSIONS.md)

Examples:
  $(basename "$0") build v0.14 "Release notes here"
  $(basename "$0") deploy v0.14
  $(basename "$0") status
  $(basename "$0") rollback v0.13
EOF
  exit 1
}

cmd_build() {
  local version="${1:-}"
  local description="${2:-}"

  if [ -z "$version" ]; then
    echo "Error: Version tag required (e.g. v0.8)"
    usage
  fi

  local git_sha
  git_sha="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
  local build_date
  build_date="$(date -u +"%Y-%m-%d %H:%M UTC")"

  echo "=== Building Hermes Agent Container Image ==="
  echo "  • Version Tag:  $version"
  echo "  • Git Revision: $git_sha"
  echo "  • Timestamp:    $build_date"
  echo ""

  DOCKER_BUILDKIT=1 docker build \
    --build-arg HERMES_VERSION="$version" \
    --build-arg HERMES_GIT_SHA="$git_sha" \
    --tag "hermes-agent:$version" \
    --tag "hermes-agent:test" \
    -f Dockerfile .

  echo ""
  echo "✓ Successfully built hermes-agent:$version and hermes-agent:test"

  if [ -n "$description" ] && [ -f "$LEDGER_FILE" ]; then
    local image_id
    image_id="$(docker image inspect --format="{{.Id}}" "hermes-agent:$version" | cut -d: -f2 | cut -c1-12)"
    echo "  • Image ID:     $image_id"
    echo "  • Note: Update $LEDGER_FILE to record this release."
  fi
}

cmd_deploy() {
  local version="${1:-}"

  if [ -z "$version" ]; then
    echo "Error: Version tag required (e.g. v0.14)"
    usage
  fi

  echo "=== Deploying $version to Serving Container (Port 9119) ==="
  docker tag "hermes-agent:$version" hermes-agent:local

  # Recreate the serving container with the new version tag
  docker stop hermes-agent-serving >/dev/null 2>&1 || true
  docker rm hermes-agent-serving >/dev/null 2>&1 || true
  HERMES_TAG="$version" docker compose -f docker-compose.local.yml up -d

  echo "Waiting for healthcheck on port 9119..."
  local healthy=0
  for _ in $(seq 1 15); do
    if curl -s -f "http://127.0.0.1:9119/api/status" >/dev/null 2>&1 || curl -s -f "http://127.0.0.1:9119/login" >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
  done

  if [ "$healthy" -eq 1 ]; then
    echo "✓ Serving container (:9119) is healthy and running hermes-agent:$version"
  else
    echo "⚠ Warning: Serving container started, waiting for service convergence."
  fi
}

cmd_promote() {
  local version="${1:-}"

  if [ -z "$version" ]; then
    echo "Error: Version tag required (e.g. v0.8)"
    usage
  fi

  echo "=== Promoting $version to Serving Slot (Port 9119) ==="
  docker tag "hermes-agent:$version" hermes-agent:local

  # Recreate the serving container with the new version tag
  docker stop hermes-agent-serving >/dev/null 2>&1 || true
  docker rm hermes-agent-serving >/dev/null 2>&1 || true
  HERMES_TAG="$version" docker compose -p hermes-serving -f docker-compose.local.yml up -d

  echo "Waiting for healthcheck on port 9119..."
  local healthy=0
  for _ in $(seq 1 15); do
    if curl -s -f "http://127.0.0.1:9119/api/status" >/dev/null 2>&1 || curl -s -f "http://127.0.0.1:9119/login" >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
  done

  if [ "$healthy" -eq 1 ]; then
    echo "✓ Serving slot (:9119) is healthy and running hermes-agent:$version"
  else
    echo "⚠ Warning: Serving container started, waiting for service convergence."
  fi
}

cmd_rollback() {
  local version="${1:-}"

  if [ -z "$version" ]; then
    echo "Error: Rollback target version tag required (e.g. v0.6)"
    usage
  fi

  echo "=== Rolling Back Serving Slot to $version ==="
  cmd_promote "$version"
}

cmd_status() {
  echo "=== Active Hermes Agent Containers ==="
  docker ps --filter "name=hermes-agent" --format "table {{.Names}}	{{.Image}}	{{.Status}}	{{.Ports}}"
  echo ""
  echo "=== Local Image Inventory ==="
  docker images --filter "reference=hermes-agent" --format "table {{.Repository}}	{{.Tag}}	{{.ID}}	{{.CreatedSince}}	{{.Size}}"
}

cmd_ledger() {
  if [ -f "$LEDGER_FILE" ]; then
    cat "$LEDGER_FILE"
  else
    echo "Ledger file not found at: $LEDGER_FILE"
  fi
}

# Subcommand dispatch
COMMAND="${1:-}"
shift || true

case "$COMMAND" in
  build)    cmd_build "$@" ;;
  deploy)   cmd_deploy "$@" ;;
  stage)    cmd_deploy "$@" ;;
  promote)  cmd_deploy "$@" ;;
  promote)  cmd_promote "$@" ;;
  rollback) cmd_rollback "$@" ;;
  status)   cmd_status ;;
  ledger)   cmd_ledger ;;
  *)        usage ;;
esac
