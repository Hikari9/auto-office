#!/usr/bin/env bash
set -euo pipefail
# office-spawn.sh — V3 adapter-driven agent spawner
#
# Reads the adapter YAML to determine executable, argv template, prompt
# transport, and cwd handling. Spawns the agent process and records
# dispatch state.
#
# Usage:
#   office-spawn.sh --adapter <adapter.yaml> --model <model> --effort <effort> \
#     --worktree <dir> --brief <file> --dispatch-id <id> --run-id <id> \
#     [--state-dir <dir>] [--timeout <seconds>] [--pane-id <id>] [--agent-name <name>]
#
# --pane-id/--agent-name record the dispatch in the Herdr pane ledger
# (OFFICE_PANE_LEDGER, default /tmp/office/panes.jsonl) so the Stop hook
# scripts/hooks/close_finished_panes.mjs can close the pane once its agent
# reports done. A pane-hosted dispatch spawned without these stays open forever,
# because the hook only ever closes panes it finds in the ledger.

ADAPTER=""
MODEL=""
EFFORT=""
WORKTREE=""
BRIEF=""
DISPATCH_ID=""
RUN_ID=""
STATE_DIR=""
TIMEOUT=30
PANE_ID=""
AGENT_NAME=""


while [[ $# -gt 0 ]]; do
  case $1 in
    --adapter) ADAPTER="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --effort) EFFORT="$2"; shift 2 ;;
    --worktree) WORKTREE="$2"; shift 2 ;;
    --brief) BRIEF="$2"; shift 2 ;;
    --dispatch-id) DISPATCH_ID="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --pane-id) PANE_ID="$2"; shift 2 ;;
    --agent-name) AGENT_NAME="$2"; shift 2 ;;
    *) echo "office-spawn: unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$ADAPTER" ]]     || { echo "office-spawn: --adapter is required" >&2; exit 1; }
[[ -n "$DISPATCH_ID" ]] || { echo "office-spawn: --dispatch-id is required" >&2; exit 1; }
[[ -n "$MODEL" ]]       || { echo "office-spawn: --model is required" >&2; exit 1; }
[[ -f "$ADAPTER" ]]     || { echo "office-spawn: adapter file not found: $ADAPTER" >&2; exit 1; }
[[ -z "$BRIEF" || -f "$BRIEF" ]] || { echo "office-spawn: brief file not found: $BRIEF" >&2; exit 1; }
[[ -z "$WORKTREE" || -d "$WORKTREE" ]] || { echo "office-spawn: worktree not found: $WORKTREE" >&2; exit 1; }

# Default state dir
if [[ -z "$STATE_DIR" ]]; then
  STATE_DIR="${WORKTREE:-.}/.office"
fi

DISPATCH_DIR="${STATE_DIR}/dispatches/${DISPATCH_ID}"
mkdir -p "$DISPATCH_DIR"

LOGFILE="${DISPATCH_DIR}/output.log"
PIDFILE="${DISPATCH_DIR}/pid"

# Read adapter config and build command via python
PROMPT_CONTENT=""
if [[ -n "$BRIEF" ]]; then
  PROMPT_CONTENT="$(cat "$BRIEF")"
fi

LAUNCH_SCRIPT=$(python3 -c "
import sys, json, yaml, shlex

adapter = yaml.safe_load(open(sys.argv[1]))
model = sys.argv[2]
effort = sys.argv[3]
cwd = sys.argv[4]
prompt = sys.argv[5]

inv = adapter.get('invocation', {})
exe = inv.get('executable', '')
argv_template = inv.get('argv', [])
prompt_transport = inv.get('prompt_transport', 'argv')

# Build argv by substituting placeholders
argv = [exe]
for arg in argv_template:
    a = str(arg)
    a = a.replace('{model}', model)
    a = a.replace('{effort}', effort)
    a = a.replace('{cwd}', cwd)
    if '{prompt}' in a:
        a = a.replace('{prompt}', prompt)
    elif '{label}' in a:
        a = a.replace('{label}', 'office-' + sys.argv[6])
    argv.append(a)

result = {
    'argv': argv,
    'prompt_transport': prompt_transport,
    'executable': exe,
}
print(json.dumps(result))
" "$ADAPTER" "$MODEL" "$EFFORT" "${WORKTREE:-.}" "$PROMPT_CONTENT" "$DISPATCH_ID" 2>&1) || {
  echo "office-spawn: failed to parse adapter: $LAUNCH_SCRIPT" >&2
  exit 1
}

PROMPT_TRANSPORT=$(echo "$LAUNCH_SCRIPT" | python3 -c "import sys,json; print(json.load(sys.stdin)['prompt_transport'])")

# Extract argv as a shell-safe command
LAUNCH_CMD=$(echo "$LAUNCH_SCRIPT" | python3 -c "
import sys, json, shlex
d = json.load(sys.stdin)
print(' '.join(shlex.quote(a) for a in d['argv']))
")

# Spawn based on prompt transport
if [[ "$PROMPT_TRANSPORT" == "stdin" && -n "$PROMPT_CONTENT" ]]; then
  ( echo "$PROMPT_CONTENT" | eval "$LAUNCH_CMD" > "$LOGFILE" 2>&1; echo $? > "${DISPATCH_DIR}/exit_code" ) &
  PID=$!
else
  ( eval "$LAUNCH_CMD" > "$LOGFILE" 2>&1; echo $? > "${DISPATCH_DIR}/exit_code" ) &
  PID=$!
fi

echo "$PID" > "$PIDFILE"

# Record dispatch metadata
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
python3 -c "
import json, sys
print(json.dumps({
    'pid': int(sys.argv[1]),
    'dispatch_id': sys.argv[2],
    'run_id': sys.argv[3],
    'model': sys.argv[4],
    'effort': sys.argv[5],
    'adapter': sys.argv[6],
    'worktree': sys.argv[7],
    'started_at': sys.argv[8],
    'logfile': sys.argv[9],
}, indent=2))
" "$PID" "$DISPATCH_ID" "$RUN_ID" "$MODEL" "$EFFORT" "$ADAPTER" "${WORKTREE:-.}" "$NOW" "$LOGFILE" \
  > "${DISPATCH_DIR}/meta.json"

# Record the pane in the Herdr ledger, in the same block that spawned it, so it
# cannot be forgotten separately from spawning.
if [[ -n "$PANE_ID" ]]; then
  LEDGER="${OFFICE_PANE_LEDGER:-/tmp/office/panes.jsonl}"
  mkdir -p "$(dirname "$LEDGER")"
  python3 -c "
import json, sys
print(json.dumps({
    'pane_id': sys.argv[1],
    'agent': sys.argv[2] or None,
    'dispatch_id': sys.argv[3],
    'run_id': sys.argv[4],
    'recorded_at': sys.argv[5],
}))
" "$PANE_ID" "$AGENT_NAME" "$DISPATCH_ID" "$RUN_ID" "$NOW" >> "$LEDGER"
fi

# Startup check: wait briefly and verify process didn't die immediately
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  EXIT_CODE=1
  [[ -f "${DISPATCH_DIR}/exit_code" ]] && EXIT_CODE=$(cat "${DISPATCH_DIR}/exit_code")
  echo "{\"error\": \"process died immediately\", \"exit_code\": $EXIT_CODE, \"dispatch_id\": \"$DISPATCH_ID\"}" >&2
  exit 1
fi

# Output success
cat <<EOF
{
  "pid": $PID,
  "dispatch_id": "$DISPATCH_ID",
  "run_id": "$RUN_ID",
  "logfile": "$LOGFILE",
  "adapter": "$ADAPTER",
  "model": "$MODEL",
  "started_at": "$NOW"
}
EOF
