#!/usr/bin/env bash
set -euo pipefail

OFFICE_STATE_DIR="${OFFICE_STATE_DIR:-.office}"
LOCKFILE="${OFFICE_STATE_DIR}/hooks/session_end.lock"
RUNTIME_SCRIPT="$(dirname "$0")/../office_runtime.py"
DB="${OFFICE_STATE_DIR}/telemetry.db"

mkdir -p "$(dirname "$LOCKFILE")"
if ! (set -o noclobber; > "$LOCKFILE") 2> /dev/null; then
    exit 0
fi
trap 'rm -f "$LOCKFILE"' EXIT

if ! git diff-index --quiet HEAD --; then
    git commit -am "[auto-office:checkpoint] session-end"
fi

RUN_ID=$(grep -m1 '"run_id"' "${OFFICE_STATE_DIR}/state.json" 2>/dev/null | cut -d'"' -f4 || echo "unknown")
FAMILY_ID=$(grep -m1 '"family_id"' "${OFFICE_STATE_DIR}/state.json" 2>/dev/null | cut -d'"' -f4 || echo "unknown")
PHASE=$(grep -m1 '"phase"' "${OFFICE_STATE_DIR}/state.json" 2>/dev/null | cut -d'"' -f4 || echo "unknown")
PLAN_VERSION=$(grep -m1 '"plan_version"' "${OFFICE_STATE_DIR}/state.json" 2>/dev/null | grep -o '[0-9]*' || echo 1)
PACKET_VERSION=$(grep -m1 '"packet_version"' "${OFFICE_STATE_DIR}/state.json" 2>/dev/null | grep -o '[0-9]*' || echo 1)

"$RUNTIME_SCRIPT" state-save \
    --state-dir "$OFFICE_STATE_DIR" \
    --run-id "$RUN_ID" \
    --family-id "$FAMILY_ID" \
    --phase "$PHASE" \
    --plan-version "$PLAN_VERSION" \
    --packet-version "$PACKET_VERSION" || true

if [ -d "${OFFICE_STATE_DIR}/dispatches" ]; then
    for pidfile in "${OFFICE_STATE_DIR}/dispatches/"*.pid; do
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
            fi
            rm -f "$pidfile"
        fi
    done
fi

# A shared, fixed /tmp path here would let two concurrent sessions'
# session_end hooks race on the same file; this dispatch record is scoped
# under this run's own state dir instead.
DISPATCH_RECORD="${OFFICE_STATE_DIR}/session_end_dispatch.json"
cat << EOF > "$DISPATCH_RECORD"
{
  "run_id": "$RUN_ID",
  "role": "system",
  "holder_id": "session_end_hook",
  "task_shape": "checkpoint",
  "outcome": "pending"
}
EOF
"$RUNTIME_SCRIPT" record-dispatch --db "$DB" "$DISPATCH_RECORD" || true
exit 0
