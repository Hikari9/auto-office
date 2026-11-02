#!/bin/sh
# Read-only validation of a CLI launch target. No agent is started.
set -eu
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo 'usage: sh agent-preflight.sh <codex|agy|claude|herdr> <absolute-worktree> [expected-branch]' >&2
  exit 2
fi
harness=$1
worktree=$2
expected=${3-}
case "$harness" in
  codex|agy|claude|herdr) ;;
  *) echo "unknown harness: $harness" >&2; exit 2 ;;
esac
case "$worktree" in
  /*) ;;
  *) echo 'worktree must be an absolute path' >&2; exit 2 ;;
esac
command -v "$harness" >/dev/null 2>&1 || { echo "missing executable: $harness" >&2; exit 3; }
if [ "$harness" = herdr ] && [ "${HERDR_ENV:-}" != 1 ]; then
  echo 'Herdr control is allowed only from an owning Herdr pane' >&2
  exit 3
fi
actual=$(cd "$worktree" && pwd -P) || exit 3
root=$(git -C "$actual" rev-parse --show-toplevel 2>/dev/null) || { echo 'not a Git worktree' >&2; exit 3; }
root=$(cd "$root" && pwd -P) || exit 3
if [ "$root" != "$actual" ]; then
  echo "target is not the worktree root: $actual (root: $root)" >&2
  exit 3
fi
branch=$(git -C "$actual" symbolic-ref --quiet --short HEAD 2>/dev/null || printf DETACHED)
if [ -n "$expected" ] && [ "$branch" != "$expected" ]; then
  echo "wrong branch: expected $expected, got $branch" >&2
  exit 3
fi
printf 'harness=%s\nworktree=%s\nbranch=%s\n' "$harness" "$actual" "$branch"
echo 'preflight=ok (inspect model, effort, permissions, session and writer ownership after launch)'
