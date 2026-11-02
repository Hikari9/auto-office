#!/usr/bin/env bash
set -euo pipefail
# office-worktree.sh — Git worktree management for dispatches
#
# Usage:
#   office-worktree.sh create --family-id <id> --run-id <id> --dispatch-id <id> [--base-ref <ref>] [--worktree-path <path>]
#   office-worktree.sh check --worktree <path> [--base-ref <ref>]
#   office-worktree.sh snapshot-diff --worktree <path> --output <file> [--base-ref <ref>]
#   office-worktree.sh cleanup --worktree <path> [--force]
#   office-worktree.sh cleanup-run [--state-dir <dir>] [--run-id <id>] [--force]
#   office-worktree.sh prune
#
# --base-ref on check/snapshot-diff: executors now commit their own work
# locally to their dispatch branch as a checkpoint (see skills/auto-loop and
# skills/herdr) rather than leaving it uncommitted. Once a dispatch has
# commits, a bare `git diff HEAD`/`diff-index HEAD` is empty even though real,
# unmerged work exists on the branch -- it only ever saw working-tree
# dirtiness, never the branch's own commits. Pass --base-ref (the SHA/ref the
# dispatch branch was cut from, e.g. the run's pinned base_sha) to see the
# dispatch's full contribution, committed and uncommitted together. Omit it
# and both subcommands fall back to their original HEAD-relative behavior.

ACTION=${1:-}
shift || true

case "$ACTION" in
    create)
        FAMILY_ID=""
        RUN_ID=""
        DISPATCH_ID=""
        BASE_REF="HEAD"
        CUSTOM_WORKTREE_PATH=""

        while [[ $# -gt 0 ]]; do
            case $1 in
                --family-id) FAMILY_ID="$2"; shift 2 ;;
                --run-id) RUN_ID="$2"; shift 2 ;;
                --dispatch-id) DISPATCH_ID="$2"; shift 2 ;;
                --base-ref) BASE_REF="$2"; shift 2 ;;
                --worktree-path) CUSTOM_WORKTREE_PATH="$2"; shift 2 ;;
                *) echo "Unknown arg: $1"; exit 1 ;;
            esac
        done

        if [[ -z "$FAMILY_ID" || -z "$RUN_ID" || -z "$DISPATCH_ID" ]]; then
            echo "Usage: office-worktree.sh create --family-id <id> --run-id <id> --dispatch-id <id> [--base-ref <ref>] [--worktree-path <path>]" >&2
            exit 1
        fi

        BRANCH_NAME="office/${FAMILY_ID}/${RUN_ID}/${DISPATCH_ID}"
        # We assume the current directory is within the git repo we want to create a worktree for.
        # Alternatively, create them in a specific directory. 
        # Typically worktrees are placed adjacent or in a hidden folder. Let's place it in .office/worktrees/
        WORKTREE_PATH="${CUSTOM_WORKTREE_PATH:-${HOME}/.office/worktrees/${DISPATCH_ID}}"
        mkdir -p "$(dirname "$WORKTREE_PATH")"

        git worktree add -b "$BRANCH_NAME" "$WORKTREE_PATH" "$BASE_REF"
        echo "$WORKTREE_PATH"
        ;;
    
    check)
        WORKTREE=""
        BASE_REF=""
        while [[ $# -gt 0 ]]; do
            case $1 in
                --worktree) WORKTREE="$2"; shift 2 ;;
                --base-ref) BASE_REF="$2"; shift 2 ;;
                *) echo "Unknown arg: $1"; exit 1 ;;
            esac
        done

        if [[ -z "$WORKTREE" ]]; then
            echo "Usage: office-worktree.sh check --worktree <path> [--base-ref <ref>]" >&2
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

        if [[ -n "$BASE_REF" ]]; then
            COMMITS_AHEAD=$(git rev-list --count "$BASE_REF"..HEAD 2>/dev/null || echo 0)
            cat <<EOF
{
  "dirty": $DIRTY,
  "uncommitted": $UNCOMMITTED,
  "commits_ahead_of_base": $COMMITS_AHEAD
}
EOF
        else
            cat <<EOF
{
  "dirty": $DIRTY,
  "uncommitted": $UNCOMMITTED
}
EOF
        fi
        ;;
    
    snapshot-diff)
        WORKTREE=""
        OUTPUT=""
        BASE_REF=""
        while [[ $# -gt 0 ]]; do
            case $1 in
                --worktree) WORKTREE="$2"; shift 2 ;;
                --output) OUTPUT="$2"; shift 2 ;;
                --base-ref) BASE_REF="$2"; shift 2 ;;
                *) echo "Unknown arg: $1"; exit 1 ;;
            esac
        done

        if [[ -z "$WORKTREE" || -z "$OUTPUT" ]]; then
            echo "Usage: office-worktree.sh snapshot-diff --worktree <path> --output <file> [--base-ref <ref>]" >&2
            exit 1
        fi

        cd "$WORKTREE"
        if [[ -n "$BASE_REF" ]]; then
            # Committed + uncommitted contribution since the dispatch branch's
            # own base -- the only diff that still shows a checkpointed
            # executor's work (see the --base-ref note in the header comment).
            git diff "$BASE_REF" > "$OUTPUT"
        else
            # Include staged and unstaged changes
            git diff HEAD > "$OUTPUT"
        fi
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

        BRANCH_NAME=""
        if [[ -d "$WORKTREE" ]]; then
            BRANCH_NAME=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
        fi

        # Removal refuses a dirty (uncommitted) tree by default. A refusal
        # reports work that exists in no other place; --force discards it, so
        # the caller asks for that explicitly rather than inheriting it. This
        # only catches *uncommitted* work -- an executor that already
        # committed its own checkpoint (see skills/auto-loop) leaves a clean
        # tree here even with real unmerged commits sitting on its branch, so
        # `git branch -d` just below is the only remaining backstop for that
        # case. Its refusal must not be swallowed silently, or an unmerged
        # branch's existence becomes invisible right after its worktree is
        # gone.
        if [[ "$FORCE" -eq 1 ]]; then
            git worktree remove --force "$WORKTREE"
        else
            git worktree remove "$WORKTREE"
        fi
        git worktree prune
        if [[ -n "$BRANCH_NAME" && "$BRANCH_NAME" != "HEAD" && "$BRANCH_NAME" == office/* ]]; then
            if ! BRANCH_DELETE_ERR=$(git branch -d "$BRANCH_NAME" 2>&1); then
                echo "warning: kept branch '$BRANCH_NAME' -- delete refused (commit(s) not yet merged?): $BRANCH_DELETE_ERR" >&2
            fi
        fi
        ;;

    cleanup-run)
        STATE_DIR=""
        RUN_ID=""
        FORCE=0
        while [[ $# -gt 0 ]]; do
            case $1 in
                --state-dir) STATE_DIR="$2"; shift 2 ;;
                --run-id) RUN_ID="$2"; shift 2 ;;
                --force) FORCE=1; shift ;;
                *) echo "Unknown arg: $1"; exit 1 ;;
            esac
        done

        if [[ -z "$STATE_DIR" && -z "$RUN_ID" ]]; then
            echo "Usage: office-worktree.sh cleanup-run [--state-dir <dir>] [--run-id <id>] [--force]" >&2
            exit 1
        fi

        if [[ -n "$STATE_DIR" && -f "$STATE_DIR/state.json" && -z "$RUN_ID" ]]; then
            RUN_ID=$(python3 -c "import json; print(json.load(open('$STATE_DIR/state.json')).get('run_id', ''))" 2>/dev/null || true)
        fi

        WORKTREES=()
        BRANCHES=()
        if [[ -n "$STATE_DIR" && -d "$STATE_DIR/dispatches" ]]; then
            for meta in "$STATE_DIR/dispatches/"*/meta.json; do
                if [[ -f "$meta" ]]; then
                    WT=$(python3 -c "import json; print(json.load(open('$meta')).get('worktree', ''))" 2>/dev/null || true)
                    if [[ -n "$WT" && -d "$WT" && "$WT" != "." ]]; then
                        WORKTREES+=("$WT")
                        BR=$(git -C "$WT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
                        if [[ -n "$BR" && "$BR" != "HEAD" && "$BR" == office/* ]]; then
                            BRANCHES+=("$BR")
                        fi
                    fi
                fi
            done
        fi

        if [[ -n "$RUN_ID" ]]; then
            while IFS= read -r line; do
                if [[ "$line" =~ ^worktree\ (.*) ]]; then
                    WT_PATH="${BASH_REMATCH[1]}"
                elif [[ "$line" =~ ^branch\ refs/heads/(office/.*) ]]; then
                    BR_NAME="${BASH_REMATCH[1]}"
                    if [[ "$BR_NAME" == *"/$RUN_ID/"* || "$BR_NAME" == *"/$RUN_ID" ]]; then
                        WORKTREES+=("$WT_PATH")
                        BRANCHES+=("$BR_NAME")
                    fi
                fi
            done < <(git worktree list --porcelain 2>/dev/null || true)
        fi

        if [[ ${#WORKTREES[@]} -gt 0 ]]; then
            while IFS= read -r wt; do
                if [[ -n "$wt" && -d "$wt" ]]; then
                    echo "Removing worktree: $wt"
                    if [[ "$FORCE" -eq 1 ]]; then
                        git worktree remove --force "$wt" || true
                    else
                        git worktree remove "$wt" || true
                    fi
                fi
            done < <(printf "%s\n" "${WORKTREES[@]}" | sort -u)
        fi

        git worktree prune

        if [[ ${#BRANCHES[@]} -gt 0 ]]; then
            while IFS= read -r br; do
                if [[ -n "$br" ]]; then
                    echo "Deleting branch: $br"
                    if ! BRANCH_DELETE_ERR=$(git branch -d "$br" 2>&1); then
                        echo "warning: kept branch '$br' -- delete refused (commit(s) not yet merged?): $BRANCH_DELETE_ERR" >&2
                    fi
                fi
            done < <(printf "%s\n" "${BRANCHES[@]}" | sort -u)
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
