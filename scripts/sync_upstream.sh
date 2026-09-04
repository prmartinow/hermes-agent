#!/usr/bin/env bash
# ==============================================================================
# scripts/sync_upstream.sh
#
# Manual Upstream Synchronization for Hermes Agent
# Strategy: "Rebase Topics, Rebuild Serving"
#
# 1. Fast-forward pristine 'main' from upstream/main
# 2. Rebase each topic branch (bug-fixes, gemini, memory, mobile) onto main
# 3. Rebuild active serving branch ('local') cleanly: main + 4 topic merges
# 4. Rebuild UI/TUI assets
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="${HERMES_AGENT_DIR:-$SCRIPT_DIR}"
LOG_FILE="${HOME}/.hermes/logs/upstream_sync.log"
mkdir -p "$(dirname "${LOG_FILE}")"

# Topics defined by the 5-branch architecture in github-ops skill
TOPIC_BRANCHES=("dev")

DRY_RUN=0
SKIP_BUILD=0
NO_PUSH=0
REBUILD_ONLY=0
SPECIFIC_TOPIC=""

usage() {
    cat << 'EOF'
Usage: scripts/sync_upstream.sh [options]

Options:
  --dry-run        Check status and report without modifying branches
  --skip-build     Skip compiling UI/TUI assets after rebuilding local
  --no-push        Update and rebase locally, do not push to origin
  --rebuild-only   Skip upstream fetch & rebase; rebuild local from existing topics
  --topic <name>   Rebase only a specific topic branch (e.g. bug-fixes, gemini)
  -h, --help       Show this help message

Workflow:
  1. git fetch origin & upstream
  2. Fast-forward 'main' to upstream/main (ff-only)
  3. Rebase topic branches on latest main
  4. Rebuild 'local' = main + non-ff merge of each topic branch
  5. Recompile assets and push to origin
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --no-push) NO_PUSH=1; shift ;;
        --rebuild-only) REBUILD_ONLY=1; shift ;;
        --topic) SPECIFIC_TOPIC="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo -e "$msg"
    echo -e "$msg" >> "${LOG_FILE}"
}

cd "${REPO_DIR}"

ORIGINAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
STASHED=0

if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    log "Working tree has uncommitted modifications. Stashing..."
    git stash push -u -m "upstream-sync-$(date +%s)" >> "${LOG_FILE}" 2>&1
    STASHED=1
fi

restore_state() {
    if [ "$STASHED" -eq 1 ]; then
        log "Restoring stashed changes..."
        git stash pop >> "${LOG_FILE}" 2>&1 || true
    fi
}

trap restore_state EXIT

log "======================================================================"
log "Hermes Upstream Sync: Rebase Topics, Rebuild Serving"
log "Repository: ${REPO_DIR}"
log "Original branch: ${ORIGINAL_BRANCH}"
log "======================================================================"

# ------------------------------------------------------------------------------
# 1. Fetch remotes
# ------------------------------------------------------------------------------
if [ "$REBUILD_ONLY" -eq 0 ]; then
    log "Fetching latest changes from remotes (origin, upstream)..."
    git fetch origin >> "${LOG_FILE}" 2>&1
    git fetch upstream >> "${LOG_FILE}" 2>&1

    LOCAL_MAIN_SHA=$(git rev-parse main 2>/dev/null || echo "")
    UPSTREAM_MAIN_SHA=$(git rev-parse upstream/main 2>/dev/null || echo "")

    if [ "$LOCAL_MAIN_SHA" != "$UPSTREAM_MAIN_SHA" ]; then
        NEW_COUNT=$(git rev-list --count main..upstream/main 2>/dev/null || echo "N/A")
        log "Upstream has new commits: ${LOCAL_MAIN_SHA:0:10} -> ${UPSTREAM_MAIN_SHA:0:10} (${NEW_COUNT} commits ahead)"
    else
        log "main is already aligned with upstream/main (${LOCAL_MAIN_SHA:0:10})"
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY RUN complete. No branches modified."
        exit 0
    fi

    # --------------------------------------------------------------------------
    # 2. Fast-Forward Pristine 'main'
    # --------------------------------------------------------------------------
    if [ "$LOCAL_MAIN_SHA" != "$UPSTREAM_MAIN_SHA" ]; then
        log "Fast-forwarding 'main' to upstream/main..."
        git checkout main >> "${LOG_FILE}" 2>&1
        git merge --ff-only upstream/main >> "${LOG_FILE}" 2>&1
        if [ "$NO_PUSH" -eq 0 ]; then
            git push origin main >> "${LOG_FILE}" 2>&1
            log "✓ 'main' fast-forwarded and pushed to origin/main"
        else
            log "✓ 'main' fast-forwarded locally (push skipped)"
        fi
    fi

    # --------------------------------------------------------------------------
    # 3. Rebase Topic Branches onto latest 'main'
    # --------------------------------------------------------------------------
    TARGETS=("${TOPIC_BRANCHES[@]}")
    if [ -n "$SPECIFIC_TOPIC" ]; then
        TARGETS=("$SPECIFIC_TOPIC")
    fi

    for branch in "${TARGETS[@]}"; do
        if ! git show-ref --verify --quiet "refs/heads/${branch}"; then
            log "⚠️ Topic branch '${branch}' does not exist locally. Skipping."
            continue
        fi

        log "----------------------------------------------------------------------"
        log "Rebasing topic branch '${branch}' onto main..."
        git checkout "${branch}" >> "${LOG_FILE}" 2>&1

        if git merge-base --is-ancestor main "${branch}" 2>/dev/null; then
            log "✓ '${branch}' is already rebased onto main. Up to date."
            continue
        fi

        if ! git rebase main >> "${LOG_FILE}" 2>&1; then
            log "\n❌ [REBASE CONFLICT] Conflict occurred while rebasing '${branch}' onto main!"
            log "Git is currently paused in rebase state."
            log "To resolve:"
            log "  1. Check git status to see conflicting files."
            log "  2. Edit files and resolve conflict markers."
            log "  3. git add <resolved-files>"
            log "  4. git rebase --continue"
            log "  Or abort with: git rebase --abort\n"
            exit 1
        fi

        log "✓ '${branch}' successfully rebased onto main."
        if [ "$NO_PUSH" -eq 0 ]; then
            git push --force-with-lease origin "${branch}" >> "${LOG_FILE}" 2>&1
            log "✓ '${branch}' force-pushed with lease to origin/${branch}"
        fi
    done
fi

# ------------------------------------------------------------------------------
# 4. Rebuild Serving Branch ('local')
# ------------------------------------------------------------------------------
if [ -z "$SPECIFIC_TOPIC" ]; then
    log "======================================================================"
    log "Rebuilding serving branch 'local' from pristine 'main' + 'dev' topic branch..."

    git checkout -B local main >> "${LOG_FILE}" 2>&1
    log "✓ Reset 'local' to pristine main ($(git rev-parse --short main))"

    for topic in "${TOPIC_BRANCHES[@]}"; do
        if git show-ref --verify --quiet "refs/heads/${topic}"; then
            log "Merging topic branch '${topic}' into local..."
            git merge --no-ff "${topic}" -m "chore(serving): integrate ${topic} topic branch" >> "${LOG_FILE}" 2>&1
            log "✓ Merged '${topic}'"
        else
            log "⚠️ Topic branch '${topic}' not found. Skipped."
        fi
    done

    # --------------------------------------------------------------------------
    # 5. Asset Compilation
    # --------------------------------------------------------------------------
    if [ "$SKIP_BUILD" -eq 0 ]; then
        if [ -d "${REPO_DIR}/ui-tui" ] && command -v npm >/dev/null 2>&1; then
            log "Compiling ui-tui assets..."
            (cd "${REPO_DIR}/ui-tui" && npm run build:ink && npm run build) >> "${LOG_FILE}" 2>&1 || {
                log "⚠️ Asset compilation had warnings or errors. Check ${LOG_FILE}"
            }
            log "✓ UI assets compilation complete."
        fi
    else
        log "Skipping asset build (--skip-build requested)."
    fi

    if [ "$NO_PUSH" -eq 0 ]; then
        git push --force-with-lease origin local >> "${LOG_FILE}" 2>&1
        log "✓ 'local' force-pushed with lease to origin/local"
    fi
fi

# ------------------------------------------------------------------------------
# 6. Checkout original branch & summary
# ------------------------------------------------------------------------------
git checkout "${ORIGINAL_BRANCH}" >> "${LOG_FILE}" 2>&1

log "======================================================================"
log "🎉 Upstream sync complete!"
log "Current status of branches:"
for b in main "${TOPIC_BRANCHES[@]}" local; do
    if git show-ref --verify --quiet "refs/heads/${b}"; then
        SHA=$(git rev-parse --short "$b")
        log "  • ${b}: ${SHA} ($(git log -1 --pretty=%s "$b"))"
    fi
done
log "======================================================================"
