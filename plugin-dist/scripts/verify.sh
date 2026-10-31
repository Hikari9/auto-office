#!/usr/bin/env bash
set -euo pipefail
# verify.sh — run verification gates against a worktree
#
# Usage:
#   verify.sh --worktree <path> --dispatch-id <id> --state-dir <dir> --db <path> \
#     [--packet <packet.json>] [--config <config.yaml>]

WORKTREE=""
DISPATCH_ID=""
STATE_DIR=""
DB=""
PACKET=""
CONFIG=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --worktree) WORKTREE="$2"; shift 2 ;;
    --dispatch-id) DISPATCH_ID="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --packet) PACKET="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$WORKTREE" || -z "$DISPATCH_ID" || -z "$STATE_DIR" || -z "$DB" ]]; then
  echo "Missing required arguments"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="${SCRIPT_DIR}/office_runtime.py"

mkdir -p "$STATE_DIR/.office/validations"

PROJECT_TYPE="unknown"
if [[ -f "$WORKTREE/package.json" ]]; then
  PROJECT_TYPE="node"
elif [[ -f "$WORKTREE/Cargo.toml" ]]; then
  PROJECT_TYPE="rust"
elif [[ -f "$WORKTREE/pyproject.toml" || -f "$WORKTREE/requirements.txt" ]]; then
  PROJECT_TYPE="python"
fi

OVERALL_PASS=true
EXECUTED=0
SKIPPED=0
RECORD_FAILURES=0
RESULTS="[]"

# A gate with no command is SKIPPED, not passed. It records no validation row, because a
# validation row asserts that a command ran -- and it carries no evidence hash, because the
# sha256 of the empty string is not proof of anything. Previously every empty-command gate
# reported passed:true with sha256("") as its evidence, so four of the eight gates could never
# fail and an unknown project type produced a full green that executed nothing.
run_gate() {
  local kind="$1"
  local cmd="$2"
  local evidence_hash=""
  local passed=true
  local skip_reason=""

  if [[ -z "$cmd" ]]; then
    skip_reason="no command for $PROJECT_TYPE"
    SKIPPED=$((SKIPPED + 1))
    local temp_skipped
    temp_skipped=$(echo "$RESULTS" | sed 's/]$//')
    if [[ "$RESULTS" != "[]" ]]; then temp_skipped="${temp_skipped},"; fi
    RESULTS="${temp_skipped}{\"name\": \"$kind\", \"passed\": null, \"skipped\": true, \"skip_reason\": \"$skip_reason\", \"evidence_hash\": null}]"
    return 0
  fi

  EXECUTED=$((EXECUTED + 1))
  {
    if ! (cd "$WORKTREE" && eval "$cmd" > gate.log 2>&1); then
      passed=false
      OVERALL_PASS=false
    fi
    if [[ -f "$WORKTREE/gate.log" ]]; then
      evidence_hash=$("$RUNTIME" hash "$WORKTREE/gate.log" | grep -o '"hash": *"[^"]*"' | cut -d'"' -f4 || echo "")
    fi
  }

  local pass_int=1
  if [[ "$passed" == "false" ]]; then pass_int=0; fi

  # known_bad_proven records that the packet's declared known-bad control RAN and passed. It is
  # not a claim that the control can detect the behaviour it names -- one green run shows the
  # behaviour is absent now, not that the control would go red if it returned. Treat it as
  # "the declared control executed and was satisfied", nothing stronger.
  local kb_proven=0
  if [[ "$kind" == "known_bad_controls" && "$pass_int" == "1" ]]; then kb_proven=1; fi
  
  # Built by json.dump, not a heredoc. Now that $cmd comes from a packet it routinely contains
  # double quotes (python3 -c "...", pytest -k "a or b"), and interpolating it into hand-written
  # JSON produced a malformed file -- record-validation then failed and `|| true` swallowed it,
  # so the gate reported a pass with no row in runs.db. That row is exactly what record_landing
  # now requires before a landing can be recorded, so the evidence was lost silently.
  local val_file="$STATE_DIR/.office/validations/${kind}_${DISPATCH_ID}.json"
  python3 -c "
import json, sys
dispatch_id, kind, cmd, passed, kb_proven, evidence_hash, out_path = sys.argv[1:8]
row = {
    'dispatch_id': dispatch_id,
    'kind': kind,
    'command': cmd,
    'passed': int(passed),
    'known_bad_proven': int(kb_proven),
    'evidence_hash': evidence_hash,
}
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(row, f, indent=2, sort_keys=True)
" "$DISPATCH_ID" "$kind" "$cmd" "$pass_int" "$kb_proven" "$evidence_hash" "$val_file"

  # A validation row that fails to record is a lost receipt, not a detail to swallow.
  if ! "$RUNTIME" record-validation --db "$DB" "$val_file" > /dev/null; then
    echo "verify: failed to record validation row for gate $kind" >&2
    RECORD_FAILURES=$((RECORD_FAILURES + 1))
    OVERALL_PASS=false
  fi

  local p_str="true"
  if [[ "$passed" == "false" ]]; then p_str="false"; fi
  
  # Append to JSON array
  local temp_results
  temp_results=$(echo "$RESULTS" | sed 's/]$//')
  if [[ "$RESULTS" != "[]" ]]; then temp_results="${temp_results},"; fi
  RESULTS="${temp_results}{\"name\": \"$kind\", \"passed\": $p_str, \"skip_reason\": \"$skip_reason\", \"evidence_hash\": \"$evidence_hash\"}]"
}

cmd_lint=""
cmd_typecheck=""
cmd_build=""
cmd_regression=""
cmd_targeted=""
cmd_known_bad=""

# The packet is where a task states what verifying IT means. Without this, targeted_tests and
# known_bad_controls had no command for any project type and so had no constructible failing
# case at all.
if [[ -n "$PACKET" && -f "$PACKET" ]]; then
  cmd_targeted=$(python3 -c "
import json,sys
p=json.load(open(sys.argv[1]))
cmds=p.get('validation_commands') or []
print(' && '.join(str(c) for c in cmds))
" "$PACKET" 2>/dev/null || echo "")
  cmd_known_bad=$(python3 -c "
import json,sys
p=json.load(open(sys.argv[1]))
kb=p.get('known_bad_behavior_to_exclude') or ''
if isinstance(kb,list): kb=[str(x) for x in kb]
else: kb=[str(kb)] if kb else []
# Only an executable control counts. Prose describing what must not happen is not a command.
print(' && '.join(c for c in kb if c.startswith('!') is False and ' ' in c and c.split()[0] in ('python3','pytest','npm','npx','bash','sh','make','cargo','go','test')))
" "$PACKET" 2>/dev/null || echo "")
fi

case "$PROJECT_TYPE" in
  node)
    cmd_lint="npm run lint"
    cmd_typecheck="npx tsc --noEmit"
    cmd_build="npm run build"
    cmd_regression="npm test"
    ;;
  rust)
    cmd_lint="cargo clippy -- -D warnings"
    cmd_typecheck="cargo check"
    cmd_build="cargo build"
    cmd_regression="cargo test"
    ;;
  python)
    cmd_lint="ruff check ."
    cmd_typecheck="mypy ."
    cmd_build=""
    cmd_regression="pytest"
    ;;
esac

run_gate "lint" "$cmd_lint"
run_gate "typecheck" "$cmd_typecheck"
run_gate "build" "$cmd_build"
run_gate "targeted_tests" "$cmd_targeted"
run_gate "regression_tests" "$cmd_regression"
run_gate "runtime_verification" ""
run_gate "browser_acceptance" ""
run_gate "known_bad_controls" "$cmd_known_bad"

overall_str="true"
reason=""
if [[ "$RECORD_FAILURES" -gt 0 ]]; then
  overall_str="false"
  reason="validation_row_not_recorded"
elif [[ "$OVERALL_PASS" == "false" ]]; then
  overall_str="false"
  reason="a gate failed"
elif [[ "$EXECUTED" -eq 0 ]]; then
  # Nothing ran, so nothing was verified. Reporting this as a pass is how a project type with no
  # configured commands used to earn a full green without executing a single command.
  overall_str="false"
  reason="no_gate_executed"
fi

cat <<EOF
{
  "passed": $overall_str,
  "reason": "$reason",
  "executed": $EXECUTED,
  "skipped": $SKIPPED,
  "project_type": "$PROJECT_TYPE",
  "dispatch_id": "$DISPATCH_ID",
  "gates": $RESULTS
}
EOF
