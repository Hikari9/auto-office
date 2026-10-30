#!/usr/bin/env bash
set -euo pipefail

OFFICE_STATE_DIR="${OFFICE_STATE_DIR:-.office}"
LOCKFILE="${OFFICE_STATE_DIR}/hooks/session_end.lock"
RUNTIME_SCRIPT="$(dirname "$0")/../office_runtime.py"

# A repository's .office directory contains run pointers; the durable state and
# recorder normally live outside the repository. Resolve the selected run before
# reading or writing lifecycle state so this hook cannot silently create a second
# telemetry database beside the repo.
RUN_ID="${OFFICE_RUN_ID:-}"
RUN_STATE_DIR="$OFFICE_STATE_DIR"
if [ ! -f "${RUN_STATE_DIR}/state.json" ]; then
    if [ -n "$RUN_ID" ] && [ -f "${OFFICE_STATE_DIR}/runs/${RUN_ID}.ref" ]; then
        RUN_STATE_DIR=$(sed -n '1p' "${OFFICE_STATE_DIR}/runs/${RUN_ID}.ref")
    else
        refs=("${OFFICE_STATE_DIR}"/runs/*.ref)
        if [ "${#refs[@]}" -eq 1 ] && [ -f "${refs[0]}" ]; then
            RUN_STATE_DIR=$(sed -n '1p' "${refs[0]}")
        fi
    fi
fi

if [ ! -f "${RUN_STATE_DIR}/state.json" ]; then
    echo "auto-office session_end: no resolvable run state under ${OFFICE_STATE_DIR}" >&2
    exit 0
fi

mkdir -p "$(dirname "$LOCKFILE")"
if ! (set -o noclobber; > "$LOCKFILE") 2> /dev/null; then
    exit 0
fi
trap 'rm -f "$LOCKFILE"' EXIT

if ! git diff-index --quiet HEAD --; then
    git commit -am "[auto-office:checkpoint] session-end"
fi

RUN_ID=$(grep -m1 '"run_id"' "${RUN_STATE_DIR}/state.json" | cut -d'"' -f4)
FAMILY_ID=$(grep -m1 '"family_id"' "${RUN_STATE_DIR}/state.json" | cut -d'"' -f4)
PHASE=$(grep -m1 '"phase"' "${RUN_STATE_DIR}/state.json" | cut -d'"' -f4)
PLAN_VERSION=$(grep -m1 '"plan_version"' "${RUN_STATE_DIR}/state.json" | grep -o '[0-9]*')
PACKET_VERSION=$(grep -m1 '"packet_version"' "${RUN_STATE_DIR}/state.json" | grep -o '[0-9]*')

"$RUNTIME_SCRIPT" state-save \
    --state-dir "$RUN_STATE_DIR" \
    --run-id "$RUN_ID" \
    --family-id "$FAMILY_ID" \
    --phase "$PHASE" \
    --plan-version "$PLAN_VERSION" \
    --packet-version "$PACKET_VERSION" || true

if [ -d "${RUN_STATE_DIR}/dispatches" ]; then
    for pidfile in "${RUN_STATE_DIR}/dispatches/"*.pid; do
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
            fi
            rm -f "$pidfile"
        fi
    done
fi

exit 0
