#!/usr/bin/env bash
set -euo pipefail

OFFICE_STATE_DIR="${OFFICE_STATE_DIR:-.office}"
DISPATCHES_DIR="${OFFICE_STATE_DIR}/dispatches"

if [ -d "$DISPATCHES_DIR" ]; then
    find "$DISPATCHES_DIR" -name "*.pid" -type f -mmin +60 | while read -r pidfile; do
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
    done
fi

exit 0
