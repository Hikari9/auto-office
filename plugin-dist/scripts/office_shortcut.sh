#!/usr/bin/env bash
set -euo pipefail
# office-shortcut.sh — per-repo shorthand for office_runtime.py --state-dir calls
#
# A consuming repo runs many office_runtime.py subcommands per run (check-spoke,
# mark-spoke, spoke-digest, state reads, ...), each needing --state-dir resolved
# to the current run's state directory. Copy this file into the consuming repo
# at <repo>/.office/bin/office (matching how office_worktree.sh already expects
# a <repo>/.office/ tree) to get short, state-dir-free subcommands instead of
# retyping the full invocation every time.
#
# Usage (once installed as <repo>/.office/bin/office):
#   office run <run-id>              # print an export line pinning OFFICE_RUN_ID
#   office check <spoke>              # check-spoke against the pinned/latest run
#   office mark <spoke>               # spoke-digest + mark-spoke in one shot
#   office mark <spoke> --unverified
#   office state                      # dump the resolved run's state.json
#   office <raw-subcommand> [args...] # passthrough to office_runtime.py,
#                                      # auto-injecting --state-dir when omitted
#
# State-dir resolution, in order:
#   1. $OFFICE_STATE_DIR if set
#   2. $OFFICE_RUN_ID + <repo>/.office/runs/<run-id>.ref (written by `office_runtime.py start`)
#   3. the most-recently-modified .ref file under <repo>/.office/runs/
#
# office_runtime.py resolution, in order (so this script works whether it's
# run in place from this plugin's own scripts/ dir, or copied into a
# consuming repo's .office/bin/):
#   1. $OFFICE_RUNTIME_PY if set
#   2. a sibling office_runtime.py next to this script (in-place / scaffolded-next-to-runtime use)
#   3. the conventional Claude Code plugin install path

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

resolve_runtime() {
  if [[ -n "${OFFICE_RUNTIME_PY:-}" ]]; then
    echo "$OFFICE_RUNTIME_PY"
    return
  fi
  if [[ -f "$SCRIPT_DIR/office_runtime.py" ]]; then
    echo "$SCRIPT_DIR/office_runtime.py"
    return
  fi
  echo "$HOME/.claude/skills/auto-office/scripts/office_runtime.py"
}

RUNTIME="$(resolve_runtime)"
# <repo>/.office/bin/office -> <repo>; falls back to the plugin repo root when
# run in place from scripts/ (SCRIPT_DIR/..) for local testing.
if [[ "$(basename "$SCRIPT_DIR")" == "bin" ]]; then
  REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
else
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
RUNS_DIR="$REPO_ROOT/.office/runs"

resolve_state_dir() {
  if [[ -n "${OFFICE_STATE_DIR:-}" ]]; then
    echo "$OFFICE_STATE_DIR"
    return
  fi
  if [[ -n "${OFFICE_RUN_ID:-}" ]]; then
    cat "$RUNS_DIR/${OFFICE_RUN_ID}.ref"
    return
  fi
  local latest
  latest="$(ls -t "$RUNS_DIR"/*.ref 2>/dev/null | head -1)"
  if [[ -z "$latest" ]]; then
    echo "no run pointer found in $RUNS_DIR and OFFICE_STATE_DIR/OFFICE_RUN_ID unset" >&2
    exit 2
  fi
  cat "$latest"
}

cmd="${1:-}"
shift || true

case "$cmd" in
  run)
    run_id="${1:?run id required}"
    echo "export OFFICE_RUN_ID=$run_id"
    ;;
  check)
    spoke="${1:?spoke name required}"
    python3 "$RUNTIME" check-spoke --state-dir "$(resolve_state_dir)" --spoke "$spoke"
    ;;
  mark)
    spoke="${1:?spoke name required}"
    shift || true
    if [[ "${1:-}" == "--unverified" ]]; then
      python3 "$RUNTIME" mark-spoke --state-dir "$(resolve_state_dir)" --spoke "$spoke" --unverified
    else
      digest="$(python3 "$RUNTIME" spoke-digest --spoke "$spoke" | python3 -c 'import json,sys;print(json.load(sys.stdin)["digest"])')"
      python3 "$RUNTIME" mark-spoke --state-dir "$(resolve_state_dir)" --spoke "$spoke" --digest "$digest"
    fi
    ;;
  state)
    cat "$(resolve_state_dir)/state.json"
    ;;
  "")
    echo "usage: office {run <run-id>|check <spoke>|mark <spoke> [--unverified]|state|<raw office_runtime.py subcommand> [args]}" >&2
    exit 2
    ;;
  *)
    # Passthrough: inject --state-dir if the subcommand takes one and it's not already present.
    args=("$@")
    if [[ "${args[*]:-}" != *"--state-dir"* ]]; then
      python3 "$RUNTIME" "$cmd" --state-dir "$(resolve_state_dir)" ${args[@]+"${args[@]}"}
    else
      python3 "$RUNTIME" "$cmd" ${args[@]+"${args[@]}"}
    fi
    ;;
esac
