#!/usr/bin/env bash
# Build published dev commits and operate one explicitly selected Compose service.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
LEDGER_FILE="$REPO_ROOT/website/docs/developer-guide/CONTAINER_VERSIONS.md"

usage() {
  cat >&2 <<EOF
Usage:
  $(basename "$0") build <version> [description]
  $(basename "$0") deploy|promote|rollback <version> <compose-file> <project> <service> <health-url>
  $(basename "$0") status
  $(basename "$0") ledger

Builds use only tracked files from a clean, published dev commit.
Deployments never build; the Compose file, project, service and health endpoint are explicit.
EOF
  exit 1
}

die() { echo "Error: $*" >&2; exit 1; }

validate_version() {
  [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "Invalid image version tag."
}

published_revision() {
  [[ "$(git branch --show-current)" == dev ]] || die "Container releases require the dev branch."
  git diff --quiet || die "Tracked working-tree changes must be committed first."
  git diff --cached --quiet || die "Staged changes must be committed first."
  local revision main_revision branch expected published merges
  revision="$(git rev-parse --verify HEAD)" || die "Cannot resolve HEAD."
  main_revision="$(git rev-parse --verify refs/heads/main)" || die "Cannot resolve main."
  [[ "$revision" =~ ^[0-9a-f]{40}$ && "$revision" != 0000000000000000000000000000000000000000 ]] || die "HEAD is not a full commit SHA."
  [[ "$(git rev-parse --verify refs/remotes/origin/dev)" == "$revision" ]] || die "HEAD differs from origin/dev."
  [[ "$(git rev-parse --verify refs/remotes/origin/main)" == "$main_revision" ]] || die "main differs from origin/main."
  git merge-base --is-ancestor "$main_revision" "$revision" || die "main must be an ancestor of dev."
  merges="$(git rev-list --merges "$main_revision..$revision")" || die "Cannot inspect dev history."
  [[ -z "$merges" ]] || die "dev must be linear above main."
  for branch in dev main; do
    expected="$revision"
    [[ "$branch" != main ]] || expected="$main_revision"
    published="$(git ls-remote --exit-code origin "refs/heads/$branch")" || die "Cannot verify published $branch."
    [[ "$published" == "$expected"$'\t'"refs/heads/$branch" ]] || die "Local $branch differs from its published commit."
  done
  printf '%s\n' "$revision"
}

image_revision() {
  local revision
  revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1")" || die "Cannot inspect image provenance."
  [[ "$revision" =~ ^[0-9a-f]{40}$ && "$revision" != 0000000000000000000000000000000000000000 ]] || die "Image lacks full commit provenance."
  printf '%s\n' "$revision"
}

cmd_build() {
  [[ $# -ge 1 && $# -le 2 ]] || usage
  local version="$1" description="${2:-}" revision built_revision
  validate_version "$version"
  revision="$(published_revision)"
  echo "Building hermes-agent:$version from $revision"
  # An archived commit excludes untracked local state and credentials from the context.
  git archive --format=tar "$revision" | DOCKER_BUILDKIT=1 docker build \
    --progress=auto --build-arg "HERMES_VERSION=$version" \
    --build-arg "HERMES_GIT_SHA=$revision" \
    --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --tag "hermes-agent:$version" -f Dockerfile -
  built_revision="$(image_revision "hermes-agent:$version")"
  [[ "$built_revision" == "$revision" ]] || die "Built image revision differs from the verified source."
  echo "Built hermes-agent:$version ($revision)."
  [[ -z "$description" ]] || printf 'Release note: %s\nRecord this build in %s.\n' "$description" "$LEDGER_FILE"
}

healthy() { curl --fail --silent --show-error --max-time 2 "$1" >/dev/null 2>&1; }

wait_for_health() {
  local attempt
  for ((attempt=0; attempt<15; attempt++)); do
    if healthy "$1"; then return 0; fi
    sleep 1
  done
  return 1
}

cmd_deploy() {
  [[ $# -eq 5 ]] || usage
  local version="$1" compose_file="$2" project="$3" service="$4" health_url="$5"
  local candidate
  validate_version "$version"
  [[ -f "$compose_file" ]] || die "The explicit Compose file does not exist."
  [[ "$project" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid Compose project."
  [[ "$service" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "Invalid Compose service."
  [[ "$health_url" == http://* || "$health_url" == https://* ]] || die "An explicit HTTP health URL is required."
  published_revision >/dev/null
  image_revision "hermes-agent:$version" >/dev/null
  local -a compose=(docker compose -p "$project" -f "$compose_file")
  echo "Deploying hermes-agent:$version to $project/$service"
  if HERMES_TAG="$version" "${compose[@]}" up -d --no-build --wait --wait-timeout 45 "$service"; then
    candidate="$(HERMES_TAG="$version" "${compose[@]}" ps --quiet "$service")"
    if [[ -n "$candidate" && "$(docker inspect --format '{{.State.Running}}' "$candidate")" == true ]] && wait_for_health "$health_url"; then
      docker tag "hermes-agent:$version" hermes-agent:serving
      echo "Healthy: $project/$service is running hermes-agent:$version"
      return 0
    fi
  fi
  echo "Deployment failed readiness checks." >&2
  return 1
}

cmd_status() {
  docker ps --filter name=hermes-agent --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
  docker images --filter reference=hermes-agent --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}\t{{.Size}}'
}

command="${1:-}"
[[ $# -gt 0 ]] || usage
shift
case "$command" in
  build) cmd_build "$@" ;;
  deploy|promote|stage|rollback) cmd_deploy "$@" ;;
  status) [[ $# -eq 0 ]] || usage; cmd_status ;;
  ledger) [[ $# -eq 0 ]] || usage; cat "$LEDGER_FILE" ;;
  *) usage ;;
esac
