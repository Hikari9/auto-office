#!/usr/bin/env bash
set -euo pipefail

OFFICE_STATE_DIR="${OFFICE_STATE_DIR:-.office}"
STATE_FILE="${OFFICE_STATE_DIR}/state.json"
CATCH_UP="${OFFICE_STATE_DIR}/catch-up.md"

RUN_ID="unknown"
FAMILY_ID="unknown"
PHASE="unknown"
PLAN_VERSION="1"

if [ -f "$STATE_FILE" ]; then
    RUN_ID=$(grep -m1 '"run_id"' "$STATE_FILE" | cut -d'"' -f4 || echo "unknown")
    FAMILY_ID=$(grep -m1 '"family_id"' "$STATE_FILE" | cut -d'"' -f4 || echo "unknown")
    PHASE=$(grep -m1 '"phase"' "$STATE_FILE" | cut -d'"' -f4 || echo "unknown")
    PLAN_VERSION=$(grep -m1 '"plan_version"' "$STATE_FILE" | grep -o '[0-9]*' || echo "1")
fi

cat > "$CATCH_UP" << EOF
# Catch-Up

Run ID: $RUN_ID
Family ID: $FAMILY_ID
Current Phase: $PHASE
Plan Version: $PLAN_VERSION
EOF

exit 0
