#!/usr/bin/env bash
set -euo pipefail

OFFICE_STATE_DIR="${OFFICE_STATE_DIR:-.office}"
COMPACT_DIR="${OFFICE_STATE_DIR}/compact"
mkdir -p "$COMPACT_DIR"

TS=$(date +%s)
SNAPSHOT_PATH="${COMPACT_DIR}/snapshot-${TS}.json"
STATE_FILE="${OFFICE_STATE_DIR}/state.json"

if [ -f "$STATE_FILE" ]; then
    cp "$STATE_FILE" "$SNAPSHOT_PATH"
fi

cat > "${COMPACT_DIR}/advisory.md" << EOF
# Compact Advisory
Please resume the current phase and follow the plan version recorded in state.
EOF

exit 0
