#!/usr/bin/env bash
set -euo pipefail
# review_finding.sh — persist a review finding
#
# Usage:
#   review_finding.sh --dispatch-id <id> --reviewer-dispatch-id <id> \
#     --status <PASS|IMPLEMENTATION_DEFECT|PLAN_DEFECT|BRIEF_DEFECT> \
#     --summary <text> --state-dir <dir> --db <path> \
#     [--severity <level>] [--evidence-file <path>]
#
# --evidence-file, when supplied, becomes both the recorded evidence (file
# contents) and the input to evidence_hash, so the hash is traceable to a real
# artifact (e.g. a review-result.json) rather than a fixed placeholder. Absent
# it, evidence is --summary and evidence_hash is computed over that text --
# still a real, content-derived hash, never the empty-string constant this
# script previously hardcoded (docs/v3-runtime-contracts.md 6.3).

DISPATCH_ID=""
REVIEWER_ID=""
STATUS=""
SUMMARY=""
STATE_DIR=""
DB=""
SEVERITY="medium"
EVIDENCE_FILE=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --dispatch-id) DISPATCH_ID="$2"; shift 2 ;;
    --reviewer-dispatch-id) REVIEWER_ID="$2"; shift 2 ;;
    --status) STATUS="$2"; shift 2 ;;
    --summary) SUMMARY="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --severity) SEVERITY="$2"; shift 2 ;;
    --evidence-file) EVIDENCE_FILE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$DISPATCH_ID" || -z "$REVIEWER_ID" || -z "$STATUS" || -z "$SUMMARY" || -z "$STATE_DIR" || -z "$DB" ]]; then
  echo "Missing required arguments"
  exit 1
fi

if [[ -n "$EVIDENCE_FILE" && ! -f "$EVIDENCE_FILE" ]]; then
  echo "review_finding: evidence file not found: $EVIDENCE_FILE"
  exit 1
fi

mkdir -p "$STATE_DIR/.office/findings"
FINDING_ID="$(uuidgen || cat /proc/sys/kernel/random/uuid)"
FINDING_FILE="$STATE_DIR/.office/findings/${FINDING_ID}.json"

SCHEMA_STATUS="$STATUS"
case "$STATUS" in
  PASS) SCHEMA_STATUS="rejected-on-evidence" ;;
  IMPLEMENTATION_DEFECT|PLAN_DEFECT|BRIEF_DEFECT) SCHEMA_STATUS="accepted-material" ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="${SCRIPT_DIR}/office_runtime.py"

python3 -c "
import hashlib, json, sys

finding_id, dispatch_id, reviewer_id, status, severity, summary, evidence_file, out_path = sys.argv[1:9]

if evidence_file:
    evidence_bytes = open(evidence_file, 'rb').read()
    evidence = evidence_bytes.decode('utf-8', errors='replace')
else:
    evidence = summary
    evidence_bytes = summary.encode('utf-8')

evidence_hash = 'sha256:' + hashlib.sha256(evidence_bytes).hexdigest()

obj = {
    'finding_id': finding_id,
    'dispatch_id': dispatch_id,
    'reviewer_dispatch_id': reviewer_id,
    'status': status,
    'severity': severity,
    'summary': summary,
    'evidence': evidence,
    'evidence_hash': evidence_hash,
}
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(obj, f, indent=2, sort_keys=True)
" "$FINDING_ID" "$DISPATCH_ID" "$REVIEWER_ID" "$SCHEMA_STATUS" "$SEVERITY" "$SUMMARY" "$EVIDENCE_FILE" "$FINDING_FILE"

"$RUNTIME" record-finding --db "$DB" "$FINDING_FILE" > /dev/null

echo "$FINDING_ID"
