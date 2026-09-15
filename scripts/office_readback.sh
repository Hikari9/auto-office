#!/usr/bin/env bash
set -euo pipefail
# office-readback.sh — read dispatch output and classify result
#
# Usage:
#   office-readback.sh --dispatch-id <id> [--state-dir <dir>] [--landing-file <file>]
#
# --landing-file is optional. When given, the process-diagnostic
# classification below (log tail, exit code, quota/rate-limit signatures) is
# retained unchanged, and additionally the landing file is validated as
# semantic landing evidence via `office_runtime.py verify-landing` --
# schema-valid, non-empty validation commands, and a head_sha that matches the
# current tree. A dispatch can look SUCCESS by exit code and log tail while
# its landing evidence is missing or unverifiable; `landing_verified` and
# `landing_reason` surface that distinction instead of collapsing it.

DISPATCH_ID=""
STATE_DIR="${HOME}/.office/state"
LANDING_FILE=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --dispatch-id) DISPATCH_ID="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --landing-file) LANDING_FILE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$DISPATCH_ID" ]]; then
  echo "Usage: office-readback.sh --dispatch-id <id>" >&2
  exit 1
fi

DISPATCH_DIR="${STATE_DIR}/dispatches/${DISPATCH_ID}"
LOGFILE="${DISPATCH_DIR}/output.log"
METAFILE="${DISPATCH_DIR}/meta.json"
EXITFILE="${DISPATCH_DIR}/exit_code"

if [[ ! -f "$METAFILE" || ! -f "$LOGFILE" ]]; then
    echo "{\"error\": \"Dispatch not found or no output\"}" >&2
    exit 1
fi

# Extract adapter basename (e.g., 'hermes' from 'adapters/seed/hermes.yaml')
ADAPTER=$(python3 -c "import json, os; print(os.path.basename(json.load(open('$METAFILE')).get('adapter', '')).replace('.yaml', ''))")

EXIT_CODE=-1
if [[ -f "$EXITFILE" ]]; then
    EXIT_CODE=$(cat "$EXITFILE")
fi

OUTPUT_BYTES=$(stat -f %z "$LOGFILE" 2>/dev/null || stat -c %s "$LOGFILE" || echo 0)

# Output tail, escaping for JSON
# This correctly escapes quotes, backslashes, and newlines
OUTPUT_TAIL=""
if [[ "$OUTPUT_BYTES" -gt 0 ]]; then
    OUTPUT_TAIL=$(tail -c 2000 "$LOGFILE" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read())[1:-1])')
fi

# Classify result
CLASSIFICATION="UNKNOWN"
FAILURE_SIGNATURE=""

check_signature() {
    local sig="$1"
    if tail -c 2000 "$LOGFILE" | grep -qi "$sig"; then
        FAILURE_SIGNATURE="$sig"
        CLASSIFICATION="FAILURE"
        return 0
    fi
    return 1
}

# Each arm is a chain of `||`-joined checks whose overall exit status is
# non-zero whenever none of them match -- the common, non-failure case. Under
# `set -e` that would abort the whole script right here instead of falling
# through to classification below, so every arm is anchored with `|| true`.
case "$ADAPTER" in
    codex)
        check_signature "rate limit" || check_signature "quota exceeded" || check_signature "context length" || true
        ;;
    claude)
        check_signature "rate limit" || check_signature "quota exceeded" || check_signature "overloaded" || true
        ;;
    agy)
        check_signature "quota" || check_signature "rate limit" || check_signature "model not available" || true
        ;;
    hermes)
        check_signature "quota exceeded" || check_signature "model not available" || check_signature "rate limit" || check_signature "context length exceeded" || check_signature "invalid API key" || true
        ;;
    *)
        check_signature "quota exceeded" || check_signature "rate limit" || true
        ;;
esac

if [[ -z "$FAILURE_SIGNATURE" ]]; then
    if [[ "$EXIT_CODE" == "0" ]]; then
        CLASSIFICATION="SUCCESS"
    elif [[ "$EXIT_CODE" -gt 0 ]]; then
        CLASSIFICATION="FAILURE"
    fi
fi

if [[ "$CLASSIFICATION" == "UNKNOWN" && "$OUTPUT_BYTES" -gt 0 ]]; then
    CLASSIFICATION="PARTIAL_SUCCESS"
fi

# Override for quota exceeded specifically as requested in prompt: 
# "Classify result: SUCCESS, PARTIAL_SUCCESS, FAILURE, TIMEOUT, QUOTA_EXCEEDED, CRASH, UNKNOWN"
if echo "$FAILURE_SIGNATURE" | grep -qi "quota"; then
    CLASSIFICATION="QUOTA_EXCEEDED"
fi
if echo "$FAILURE_SIGNATURE" | grep -qi "rate limit"; then
    CLASSIFICATION="QUOTA_EXCEEDED" # or something similar, prompt didn't specify rate limit class, likely FAILURE or QUOTA_EXCEEDED
fi

COMPLETED_AT=$(date +%s)

LANDING_VERIFIED="null"
LANDING_REASON=""
if [[ -n "$LANDING_FILE" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    RUNTIME="${SCRIPT_DIR}/office_runtime.py"
    if [[ -f "$LANDING_FILE" ]]; then
        VERIFY_OUT=$("$RUNTIME" verify-landing --file "$LANDING_FILE" 2>&1) || true
        LANDING_VERIFIED=$(echo "$VERIFY_OUT" | python3 -c "import json,sys
try:
    print('true' if json.load(sys.stdin).get('verified') else 'false')
except Exception:
    print('false')" 2>/dev/null || echo "false")
        LANDING_REASON=$(echo "$VERIFY_OUT" | python3 -c "import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get('reason') or ('' if d.get('verified') else 'landing not verified'))
except Exception:
    print('landing file did not produce valid verify-landing output')" 2>/dev/null || echo "landing verification failed")
    else
        LANDING_VERIFIED="false"
        LANDING_REASON="landing file not found: $LANDING_FILE"
    fi
fi
LANDING_REASON_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$LANDING_REASON")

cat <<EOF
{
  "dispatch_id": "$DISPATCH_ID",
  "exit_code": $EXIT_CODE,
  "classification": "$CLASSIFICATION",
  "output_bytes": $OUTPUT_BYTES,
  "output_tail": "${OUTPUT_TAIL}",
  "failure_signature": "$FAILURE_SIGNATURE",
  "completed_at": $COMPLETED_AT,
  "landing_verified": $LANDING_VERIFIED,
  "landing_reason": $LANDING_REASON_JSON
}
EOF
