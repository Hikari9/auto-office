#!/usr/bin/env bash
set -euo pipefail
# office-worktree.sh — Git worktree management for dispatches
#
# Usage:
#   office-worktree.sh create --family-id <id> --run-id <id> --dispatch-id <id> [--base-ref <ref>]
#   office-worktree.sh check --worktree <path>
#   office-worktree.sh snapshot-diff --worktree <path> --output <file>
#   office-worktree.sh cleanup --worktree <path>
#   office-worktree.sh prune

ACTION=${1:-}
shift || true

case "$ACTION" in
    create)
        FAMILY_ID=""
        RUN_ID=""
        DISPATCH_ID=""
        BASE_REF="HEAD"

        while [[ $# -gt 0 ]]; do
            case $1 in
                --family-id) FAMILY_ID="$2"; shift 2 ;;
                --run-id) RUN_ID="$2"; shift 2 ;;
                --dispatch-id) DISPATCH_ID="$2"; shift 2 ;;
                --base-ref) BASE_REF="$2"; shift 2 ;;
                *) echo "Unknown arg: $1"; exit 1 ;;
            esac
        done

        if [[ -z "$FAMILY_ID" || -z "$RUN_ID" || -z "$DISPATCH_ID" ]]; then
            echo "Usage: office-worktree.sh create --family-id <id> --run-id <id> --dispatch-id <id> [--base-ref <ref>]" >&2
            exit 1
        fi

        BRANCH_NAME="office/${FAMILY_ID}/${RUN_ID}/${DISPATCH_ID}"
        # We assume the current directory is within the git repo we want to create a worktree for.
        # Alternatively, create them in a specific directory. 
        # Typically worktrees are placed adjacent or in a hidden folder. Let's place it in .office/worktrees/
        WORKTREE_PATH="${HOME}/.office/worktrees/${DISPATCH_ID}"
        mkdir -p "${HOME}/.office/worktrees"

        git worktree add -b "$BRANCH_NAME" "$WORKTREE_PATH" "$BASE_REF"
        echo "$WORKTREE_PATH"
        ;;
    
    check)
        WORKTREE=""
        while [[ $# -gt 0 ]]; do
            case $1 in
                --worktree) WORKTREE="$2"; shift 2 ;;
                *) echo "Unknown arg: $1"; exit 1 ;;
            esac
        done

        if [[ -z "$WORKTREE" ]]; then
            echo "Usage: office-worktree.sh check --worktree <path>" >&2
            exit 1
        fi

        cd "$WORKTREE"
        DIRTY=false
        UNCOMMITTED=false
        
        if ! git diff-index --quiet HEAD --; then
            DIRTY=true
            UNCOMMITTED=true
        fi

        # Also check untracked files
        if [[ -n $(git ls-files --others --exclude-standard) ]]; then
            DIRTY=true
            UNCOMMITTED=true
        fi

        cat <<EOF
{
  "dirty": $DIRTY,
  "uncommitted": $UNCOMMITTED
}
EOF
        ;;
    
    snapshot-diff)
        WORKTREE=""
        OUTPUT=""
        while [[ $# -gt 0 ]]; do
            case $1 in
                --worktree) WORKTREE="$2"; shift 2 ;;
                --output) OUTPUT="$2"; shift 2 ;;
                *) echo "Unknown arg: $1"; exit 1 ;;
            esac
        done

        if [[ -z "$WORKTREE" || -z "$OUTPUT" ]]; then
            echo "Usage: office-worktree.sh snapshot-diff --worktree <path> --output <file>" >&2
            exit 1
        fi

        cd "$WORKTREE"
        # Include staged and unstaged changes
        git diff HEAD > "$OUTPUT"
        # Untracked files can also be added to diff, but standard diff doesn't include them.
        # So we can add them to index temporarily or just leave it out. The prompt says "diff of uncommitted changes to a patch file".
        # Sticking to git diff HEAD is best.
        ;;

    cleanup)
        WORKTREE=""
        FORCE=0
        while [[ $# -gt 0 ]]; do
            case $1 in
                --worktree) WORKTREE="$2"; shift 2 ;;
                --force) FORCE=1; shift ;;
                *) echo "Unknown arg: $1"; exit 1 ;;
            esac
        done

        if [[ -z "$WORKTREE" ]]; then
            echo "Usage: office-worktree.sh cleanup --worktree <path> [--force]" >&2
            exit 1
        fi

        # Removal refuses a dirty tree by default. A refusal reports work that
        # exists in no other place; --force discards it, so the caller asks for
        # that explicitly rather than inheriting it.
        if [[ "$FORCE" -eq 1 ]]; then
            git worktree remove --force "$WORKTREE"
        else
            git worktree remove "$WORKTREE"
        fi
        ;;

    prune)
        git worktree prune
        ;;
    
    *)
        echo "Unknown action: $ACTION" >&2
        exit 1
        ;;
esac
