#!/usr/bin/env bash
set -euo pipefail
# office-liveness.sh — check dispatch liveness
#
# Usage:
#   office-liveness.sh --dispatch-id <id> [--state-dir <dir>]
#     [--silence-timeout <seconds>] [--max-runtime <seconds>]
#     [--session-id <id> --family-id <id>]
#
# --session-id/--family-id are optional. When both are given and this probe
# observes the process has exited, it additionally asks
# scripts/office_monitor.py to record a durable process_exit completion event
# (source="process_exit") for the dispatch — the same native, harness-independent
# signal every scripts/office_spawn.sh dispatch produces (pid + exit_code),
# trusted without a corroboration window. Recording is idempotent: a repeat
# call after the event already exists is a no-op. Omitting either flag leaves
# this script's own JSON output unchanged (no event is recorded), so existing
# callers see no behavior change.

DISPATCH_ID=""
STATE_DIR="${HOME}/.office/state"
SILENCE_TIMEOUT=300
MAX_RUNTIME=3600
SESSION_ID=""
FAMILY_ID=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --dispatch-id) DISPATCH_ID="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --silence-timeout) SILENCE_TIMEOUT="$2"; shift 2 ;;
    --max-runtime) MAX_RUNTIME="$2"; shift 2 ;;
    --session-id) SESSION_ID="$2"; shift 2 ;;
    --family-id) FAMILY_ID="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$DISPATCH_ID" ]]; then
  echo "Usage: office-liveness.sh --dispatch-id <id>" >&2
  exit 1
fi

DISPATCH_DIR="${STATE_DIR}/dispatches/${DISPATCH_ID}"
PIDFILE="${DISPATCH_DIR}/pid"
LOGFILE="${DISPATCH_DIR}/output.log"
METAFILE="${DISPATCH_DIR}/meta.json"
LAST_SIZE_FILE="${DISPATCH_DIR}/last_size"
LAST_TIME_FILE="${DISPATCH_DIR}/last_time"

if [[ ! -f "$PIDFILE" || ! -f "$METAFILE" ]]; then
    echo "{\"error\": \"Dispatch not found\"}" >&2
    exit 1
fi

PID=$(cat "$PIDFILE")
# Parse started_at with python for safety
STARTED_AT=$(python3 -c "import json; print(json.load(open('$METAFILE'))['started_at'])")
NOW=$(date +%s)
RUNTIME=$((NOW - STARTED_AT))

ALIVE="false"
if kill -0 $PID 2>/dev/null; then
    ALIVE="true"
fi

OUTPUT_BYTES=0
if [[ -f "$LOGFILE" ]]; then
    # OS X stat uses -f %z, Linux stat uses -c %s
    OUTPUT_BYTES=$(stat -f %z "$LOGFILE" 2>/dev/null || stat -c %s "$LOGFILE" || echo 0)
fi

LAST_SIZE=0
if [[ -f "$LAST_SIZE_FILE" ]]; then
    LAST_SIZE=$(cat "$LAST_SIZE_FILE")
fi

LAST_TIME=$NOW
if [[ -f "$LAST_TIME_FILE" ]]; then
    LAST_TIME=$(cat "$LAST_TIME_FILE")
else
    echo "$NOW" > "$LAST_TIME_FILE"
fi

STATUS="running"
SILENT_SECONDS=0

if [[ "$ALIVE" == "true" ]]; then
    if [[ "$OUTPUT_BYTES" -gt "$LAST_SIZE" ]]; then
        echo "$OUTPUT_BYTES" > "$LAST_SIZE_FILE"
        echo "$NOW" > "$LAST_TIME_FILE"
        SILENT_SECONDS=0
    else
        SILENT_SECONDS=$((NOW - LAST_TIME))
        if [[ "$SILENT_SECONDS" -ge "$SILENCE_TIMEOUT" ]]; then
            STATUS="silent"
        fi
    fi
    
    if [[ "$RUNTIME" -ge "$MAX_RUNTIME" ]]; then
        STATUS="timed_out"
    fi
else
    if [[ "$OUTPUT_BYTES" -eq 0 && "$RUNTIME" -lt 10 ]]; then
        STATUS="startup_failed"
    else
        STATUS="completed"
    fi
fi

# Emit a durable process_exit completion event once the process has actually
# exited, but only when the caller supplied the identity fields a completion
# event requires (session_id, family_id) — see usage note above. This never
# runs for "running"/"silent"/"timed_out" (the process may still be alive) or
# "startup_failed" (no PID identity worth recording as a terminal fact).
# Omitting --session-id/--family-id leaves the JSON output below byte-identical
# to before this field existed: no "completion_event" key is added at all.
if [[ ( "$STATUS" == "completed" ) && -n "$SESSION_ID" && -n "$FAMILY_ID" ]]; then
    MONITOR_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/office_monitor.py"
    COMPLETION_EVENT_JSON=$(python3 "$MONITOR_SCRIPT" process-exit-event \
        --state-dir "$STATE_DIR" \
        --session-id "$SESSION_ID" \
        --family-id "$FAMILY_ID" \
        --dispatch-id "$DISPATCH_ID" 2>/dev/null || echo "null")
    [[ -n "$COMPLETION_EVENT_JSON" ]] || COMPLETION_EVENT_JSON="null"
    cat <<EOF
{
  "alive": $ALIVE,
  "pid": $PID,
  "runtime_seconds": $RUNTIME,
  "output_bytes": $OUTPUT_BYTES,
  "last_output_at": $LAST_TIME,
  "silent_seconds": $SILENT_SECONDS,
  "status": "$STATUS",
  "completion_event": $COMPLETION_EVENT_JSON
}
EOF
else
cat <<EOF
{
  "alive": $ALIVE,
  "pid": $PID,
  "runtime_seconds": $RUNTIME,
  "output_bytes": $OUTPUT_BYTES,
  "last_output_at": $LAST_TIME,
  "silent_seconds": $SILENT_SECONDS,
  "status": "$STATUS"
}
EOF
fi
