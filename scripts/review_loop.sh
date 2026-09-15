#!/usr/bin/env bash
set -euo pipefail
# review_loop.sh — orchestrate the review/verification/fix cycle
#
# Usage:
#   review_loop.sh --state-dir <dir> --dispatch-id <producer-dispatch-id> \
#     --reviewer-dispatch-id <id> --worktree <path> --db <path> \
#     [--review-file <review-result.json>] [--review-scope <csv>] \
#     [--max-iterations <n>] [--config <config.yaml>]
#
# --review-file is the ONLY source of an independent-review PASS. It must be a
# review-result.json (schemas/review-result.schema.json) produced by the
# routed reviewer's own dispatch. A recorded PASS is reachable only when ALL of
# the following hold (docs/plans/v3-final-merge.md amendment v2 finding F3):
#   - the file's reviewer_id equals --reviewer-dispatch-id (the routed reviewer)
#   - the file's dispatch_id equals --dispatch-id (the producer under review)
#   - the file's reviewed_head_sha matches the current tree at --worktree
#   - --review-scope, when given, equals the file's review_scope set
#   - office_runtime.py record-review accepts it (schema-valid, producer !=
#     reviewer, all three version numbers match the family record when the
#     file carries a family_id, non-empty evidence)
# Missing, unset, synthetic, or unbound review evidence makes PASS
# UNAVAILABLE -- never PASS -- and the loop exits 4 (fail closed).

STATE_DIR=""
DISPATCH_ID=""
REVIEWER_ID=""
WORKTREE=""
DB=""
MAX_ITERATIONS=3
CONFIG=""
REVIEW_FILE=""
REVIEW_SCOPE=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --dispatch-id) DISPATCH_ID="$2"; shift 2 ;;
    --reviewer-dispatch-id) REVIEWER_ID="$2"; shift 2 ;;
    --worktree) WORKTREE="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --max-iterations) MAX_ITERATIONS="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --review-file) REVIEW_FILE="$2"; shift 2 ;;
    --review-scope) REVIEW_SCOPE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$STATE_DIR" || -z "$DISPATCH_ID" || -z "$REVIEWER_ID" || -z "$WORKTREE" || -z "$DB" ]]; then
  echo "Missing required arguments"
  exit 4
fi

if [[ "$DISPATCH_ID" == "$REVIEWER_ID" ]]; then
  echo "Self-approval rejection: producer_dispatch_id and reviewer_dispatch_id must be different."
  exit 4
fi

if [[ -n "$CONFIG" && -f "$CONFIG" ]]; then
  if grep -q "max_review_iterations:" "$CONFIG"; then
    MAX_ITERATIONS=$(grep "max_review_iterations:" "$CONFIG" | awk '{print $2}')
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFY_SCRIPT="${SCRIPT_DIR}/verify.sh"
REVIEW_FINDING_SCRIPT="${SCRIPT_DIR}/review_finding.sh"
RUNTIME="${SCRIPT_DIR}/office_runtime.py"

# write_outcome_label persists one outcome_labels row for the producer dispatch,
# citing the evidence hash it was given. A dispatch with no evidence (empty
# $2) is left unlabeled -- never recorded as a success or any other outcome
# (docs/plans/v3-final-merge.md amendment v3).
write_outcome_label() {
  local label="$1" evidence_hash="$2" narrative="${3:-}" attribution="${4:-unknown}"
  if [[ -z "$evidence_hash" ]]; then
    return 0
  fi
  PYTHONPATH="$SCRIPT_DIR" python3 -c "
import json, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
import office_runtime as rt

dispatch_id, label, attribution, narrative, evidence_hash, db = sys.argv[1:7]
obj = {'label': label, 'attribution': attribution, 'evidence_hash': evidence_hash}
if narrative:
    obj['contributing_attributions'] = [narrative]
errors = rt.validate_with_schema(obj, 'outcome-label.schema.json')
if errors:
    # A label that fails T0's pinned vocabulary/evidence contract is not
    # written at all -- the dispatch stays unlabeled rather than recording
    # something schema-invalid.
    sys.exit(0)
con = rt.init_db(Path(db))
row_id = 'lbl-' + uuid.uuid4().hex[:16]
now = datetime.now(timezone.utc).isoformat()
con.execute(
    'INSERT INTO outcome_labels VALUES (?,?,?,?,?,?,?)',
    (row_id, dispatch_id, label, attribution,
     json.dumps(obj.get('contributing_attributions')) if 'contributing_attributions' in obj else None,
     now, evidence_hash),
)
con.commit()
con.close()
" "$DISPATCH_ID" "$label" "$attribution" "$narrative" "$evidence_hash" "$DB" || true
}

# run_review validates --review-file against every F3 binding and, only if all
# bindings hold, delegates to `office_runtime.py record-review` for schema,
# self-approval and version-triple enforcement. It never consults an
# environment variable: there is no way to assert a PASS into existence.
# Output is two lines: `STATUS::<word>` and `REASON::<text>`.
run_review() {
  if [[ -z "$REVIEW_FILE" || ! -f "$REVIEW_FILE" ]]; then
    echo "STATUS::UNAVAILABLE"
    echo "REASON::unset_review_source: no --review-file supplied, or the file does not exist; independent review PASS is unavailable until a real reviewer dispatch produces one"
    return 0
  fi

  local file_reviewer file_dispatch file_head
  file_reviewer=$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1])).get('reviewer_id') or '')
except Exception:
    print('')" "$REVIEW_FILE")
  file_dispatch=$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1])).get('dispatch_id') or '')
except Exception:
    print('')" "$REVIEW_FILE")
  file_head=$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1])).get('reviewed_head_sha') or '')
except Exception:
    print('')" "$REVIEW_FILE")

  if [[ -z "$file_reviewer" ]]; then
    echo "STATUS::UNAVAILABLE"
    echo "REASON::schema_invalid: review file is missing or unparseable"
    return 0
  fi

  if [[ "$file_reviewer" != "$REVIEWER_ID" ]]; then
    echo "STATUS::UNAVAILABLE"
    echo "REASON::reviewer_identity_mismatch: review file reviewer_id '$file_reviewer' does not equal the routed reviewer '$REVIEWER_ID'"
    return 0
  fi

  if [[ "$file_dispatch" != "$DISPATCH_ID" ]]; then
    echo "STATUS::UNAVAILABLE"
    echo "REASON::dispatch_identity_mismatch: review file dispatch_id '$file_dispatch' does not equal the producer under review '$DISPATCH_ID'"
    return 0
  fi

  local current_head
  current_head=$(git -C "$WORKTREE" rev-parse HEAD 2>/dev/null || true)
  if [[ -z "$file_head" || -z "$current_head" ]] || \
     { [[ "$current_head" != "$file_head"* ]] && [[ "$file_head" != "$current_head"* ]]; }; then
    echo "STATUS::UNAVAILABLE"
    echo "REASON::stale_tree_sha: reviewed_head_sha '$file_head' does not match the current tree '$current_head' at $WORKTREE"
    return 0
  fi

  if [[ -n "$REVIEW_SCOPE" ]]; then
    local scope_status
    scope_status=$(python3 -c "
import json, sys
expected = set(x for x in sys.argv[2].split(',') if x)
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print('mismatch'); sys.exit()
actual = set(data.get('review_scope') or [])
print('match' if expected and expected == actual else 'mismatch')
" "$REVIEW_FILE" "$REVIEW_SCOPE")
    if [[ "$scope_status" != "match" ]]; then
      echo "STATUS::UNAVAILABLE"
      echo "REASON::scope_mismatch: review_scope in '$REVIEW_FILE' does not equal the declared scope '$REVIEW_SCOPE'"
      return 0
    fi
  fi

  local record_out record_rc
  set +e
  record_out=$("$RUNTIME" record-review --file "$REVIEW_FILE" --state-dir "$STATE_DIR" 2>&1)
  record_rc=$?
  set -e
  if [[ $record_rc -ne 0 ]]; then
    local reason
    reason=$(echo "$record_out" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('reason') or 'provenance_rejected')
except Exception:
    print('provenance_rejected')
" 2>/dev/null || echo "provenance_rejected")
    echo "STATUS::UNAVAILABLE"
    echo "REASON::${reason}: ${record_out}"
    return 0
  fi

  local overall_status
  overall_status=$(echo "$record_out" | python3 -c "import json,sys; print(json.load(sys.stdin).get('overall_status') or '')")
  echo "STATUS::${overall_status:-UNAVAILABLE}"
  echo "REASON::validated independent review from reviewer '$REVIEWER_ID', tree '$current_head'"
}

iter=0
while [[ $iter -lt $MAX_ITERATIONS ]]; do
  iter=$((iter + 1))

  verify_out=$("$VERIFY_SCRIPT" --worktree "$WORKTREE" --dispatch-id "$DISPATCH_ID" --state-dir "$STATE_DIR" --db "$DB")
  passed=$(echo "$verify_out" | jq -r '.passed' 2>/dev/null || echo "$verify_out" | grep -o '"passed": *true' || true)

  if [[ "$passed" != "true" && "$passed" != "\"passed\": true" ]]; then
    finding_id=$("$REVIEW_FINDING_SCRIPT" --dispatch-id "$DISPATCH_ID" --reviewer-dispatch-id "$REVIEWER_ID" \
      --status "IMPLEMENTATION_DEFECT" --summary "Self-verification failed at iteration $iter" \
      --state-dir "$STATE_DIR" --db "$DB")

    if [[ $iter -eq $MAX_ITERATIONS ]]; then
      evidence_hash=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('evidence_hash') or '')" \
        "$STATE_DIR/.office/findings/${finding_id}.json" 2>/dev/null || true)
      write_outcome_label "abandoned" "$evidence_hash" "narrative:failed_verification" "verification"
      echo "MAX_ITERATIONS reached during self-verification."
      exit 1
    fi

    # Run fix hook if provided
    eval "${FIX_COMMAND:-true}"
    continue
  fi

  review_out=$(run_review)
  review_status=$(echo "$review_out" | sed -n 's/^STATUS:://p')
  review_reason=$(echo "$review_out" | sed -n 's/^REASON:://p')

  case "$review_status" in
    PASS)
      finding_id=$("$REVIEW_FINDING_SCRIPT" --dispatch-id "$DISPATCH_ID" --reviewer-dispatch-id "$REVIEWER_ID" \
        --status "PASS" --summary "Independent review passed" --evidence-file "$REVIEW_FILE" \
        --state-dir "$STATE_DIR" --db "$DB")
      evidence_hash=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('evidence_hash') or '')" \
        "$STATE_DIR/.office/findings/${finding_id}.json" 2>/dev/null || true)
      write_outcome_label "verified_no_observed_failure" "$evidence_hash" "" "model"
      exit 0
      ;;
    IMPLEMENTATION_DEFECT|CHANGES_REQUIRED)
      finding_id=$("$REVIEW_FINDING_SCRIPT" --dispatch-id "$DISPATCH_ID" --reviewer-dispatch-id "$REVIEWER_ID" \
        --status "IMPLEMENTATION_DEFECT" --summary "Independent review found implementation defect" \
        --state-dir "$STATE_DIR" --db "$DB")

      if [[ $iter -eq $MAX_ITERATIONS ]]; then
        evidence_hash=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('evidence_hash') or '')" \
          "$STATE_DIR/.office/findings/${finding_id}.json" 2>/dev/null || true)
        write_outcome_label "abandoned" "$evidence_hash" "narrative:defect_detected" "model"
        echo "MAX_ITERATIONS reached."
        exit 1
      fi

      # Run fix hook if provided
      eval "${FIX_COMMAND:-true}"
      continue
      ;;
    PLAN_DEFECT)
      finding_id=$("$REVIEW_FINDING_SCRIPT" --dispatch-id "$DISPATCH_ID" --reviewer-dispatch-id "$REVIEWER_ID" \
        --status "PLAN_DEFECT" --summary "Independent review found plan defect" \
        --state-dir "$STATE_DIR" --db "$DB")
      evidence_hash=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('evidence_hash') or '')" \
        "$STATE_DIR/.office/findings/${finding_id}.json" 2>/dev/null || true)
      write_outcome_label "abandoned" "$evidence_hash" "narrative:defect_detected" "planner"

      # Increment plan version and invalidate packets
      new_plan=$("$RUNTIME" increment-plan --state-dir "$STATE_DIR" --db "$DB" | grep -o '"plan_version": *[0-9]*' | awk '{print $2}')
      if [[ -n "$new_plan" ]]; then
        "$RUNTIME" invalidate-packets --state-dir "$STATE_DIR" --plan-version "$new_plan" >/dev/null
      fi
      exit 2
      ;;
    BRIEF_DEFECT)
      finding_id=$("$REVIEW_FINDING_SCRIPT" --dispatch-id "$DISPATCH_ID" --reviewer-dispatch-id "$REVIEWER_ID" \
        --status "BRIEF_DEFECT" --summary "Independent review found brief defect" \
        --state-dir "$STATE_DIR" --db "$DB")
      evidence_hash=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('evidence_hash') or '')" \
        "$STATE_DIR/.office/findings/${finding_id}.json" 2>/dev/null || true)
      write_outcome_label "abandoned" "$evidence_hash" "narrative:defect_detected" "brief"
      exit 3
      ;;
    UNAVAILABLE)
      echo "Review unavailable: ${review_reason}"
      exit 4
      ;;
    *)
      echo "Unknown review status: $review_status"
      exit 4
      ;;
  esac
done

exit 1
