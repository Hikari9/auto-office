---
name: office-submit
description: "Finish an Auto Office executor task: behavior-preserving simplify pass, adversarial self-review of the diff, fix and mutation-prove medium+ findings, run the brief's checks, commit and push, `office preflight`, `office submit`, and end with one status line. Use when you are an Office executor (inside a task worktree with an Office brief) and your work is ready to submit, or when asked to submit, resubmit, or finish an Office task."
---

# Office Submit

The executor's tail sequence, in order. The brief's `SIMPLIFY`, `SELF-REVIEW`, `WHEN DONE`, and `FINAL REPORT` lines
are the contract; this skill is how to run them in Claude Code. Every step runs from the task worktree.

## 1. Simplify

After targeted checks pass, refine your own `git diff <base>` (from the brief's `SIMPLIFY` line) yourself:
(a) reuse existing helpers and the module that owns the concept, (b) simplify away needless state,
duplication, nesting, and dead code, (c) drop clearly repeated work, (d) altitude: fix the shared owner
when it is inside SCOPE. Behavior-preserving only: no contract, auth, validation, migration, SQL, or
data-semantic change. A higher-level owner outside SCOPE goes in the report, unedited. A real defect
goes to step 2. Skip for an empty or tiny mechanical diff. Fix rounds repeat steps 1-6, including repairs routed to you from review findings.

## 2. Adversarial self-review

Find the base and the tier in the brief's `SELF-REVIEW` line (`git diff <base>`, `(tier: <tier>)`). A new run's tier is
always `focused`. Only a run pinned to v3.1 gets `inline`, `single` or `deep`, set from its gear and risk (unknown risk is
never `inline`). You cannot lower it. Do the review as the tier says:

- **`focused`:** one round, edge cases and test strength only (platform/build too when the diff touches scripts, CI,
  packaging or OS/process code). Start exactly one `Agent` subagent with the diff command, those lenses and a read-only
  instruction, or make one fresh pass yourself. Security is not yours: the convergence reviewer covers it. Write
  `ROUND 1`, `LENS edge-cases reviewed`, `LENS test-strength reviewed` and a `LENS platform` line. There is no fix-diff re-review.
  Each medium or high fix names the one test that proves it (`fixed <test path> mutation=failed`). Office runs that test
  alone on a copy of your commit with the non-test source reverted: it must fail by a test failure (a collection error,
  timeout or missing command does not count) and pass unreverted. You do not revert anything for the proof, and with no
  named test or no pytest command in the task's checks Office records an exemption instead of blocking. `office preflight`
  runs the proof and `office submit` needs its recorded result. No exemption flag exists on this tier.

- **`inline`:** no subagents. Make one fresh pass per lens yourself and fix medium+ findings.
  You may skip a lens that clearly does not apply, with a one-line reason in your report.
- **`single`:** start exactly one `Agent` subagent, given the diff command, the brief path, all four lenses, and
  a read-only instruction (no edits).
- **`deep`:** start four `Agent` subagents in one message, in parallel. Give each the diff command, the brief
  path, and one lens, and tell it to read only (no edits).

If the tier is missing from the line, treat it as `deep`. The lenses of the v3.1 tiers are:

- **(a) Security:** secrets, token exposure in workflows or logs (`workflow_dispatch` inputs echoed),
  auth bypass, missing server-side revalidation, path or symlink escapes.
- **(b) Edge cases:** parser and lexer ambiguity (regex vs division, apostrophes in JSX), empty or null
  input, keyboard and interaction paths that bypass an explicit confirmation (Enter past a Select
  button), offline and restart states.
- **(c) Platform and build:** macOS vs Linux (BSD `sed -i`, process groups and ancestor shells),
  signing and notarization order, CI manifest and artifact names, stale build artifacts.
- **(d) Test strength:** weak assertions, and tests that would still pass with the fix reverted.

Each reviewer (subagent or your own pass) returns only a JSON array:

```json
[{"severity": "high|medium|low", "location": "file:line", "repro": "concrete input or steps", "fix": "smallest correct change"}]
```

Fix every `medium` or `high` finding inside SCOPE. For each fix, write or strengthen a test, then prove it.
Revert the fix, run the test, and confirm it fails. Then restore the fix and confirm the test passes.
Record that mutation for the report. A finding outside SCOPE goes in the report unfixed.

Record every finding in the ledger `OFFICE_SELF_REVIEW.md` (worktree root, untracked; format in the brief's `LEDGER` line:
`COMMIT <full sha of HEAD>`, `ROUND <1-3>`, a `LENS` line per lens, `FINDING <severity> <lens> <file:line> | <summary> | <disposition>`).
On `focused` the ledger is `ROUND 1` and the `edge-cases`, `test-strength` and `platform` lenses only. Dispositions: `fixed <test path> mutation=failed`, `out-of-scope`, `dismissed <reason>`, `contract-conflict accept=<n>`, `open`.
A medium or high fix names the test file that proves it and `mutation=failed` (you reverted the fix and the test failed);
a finding of any severity is `out-of-scope` only when its file is outside SCOPE.
Record severity as found: a fix never lowers it. Low findings are fixed but do not trigger a re-review. A medium or
high fix that changes behavior gets one fix-diff re-review, as the next round. At the 3-round cap, a medium or high
finding still open stops you. Run `office preflight` anyway so it records the stop for the orchestrator, then print
the status line and stop. `contract-conflict` stops you with the ACCEPT line quoted. Preflight reports a missing,
stale, or malformed ledger and any open finding as a fix. Submit refuses substantive work unless the ledger's `COMMIT`
is your HEAD, records it, and consumes it; inline tier only: `office submit --self-review-exempt trivial|mechanical -- "<reason>"`.

## 3. Checks

Run the brief's `CHECKS` lines. Check `uptime` first. When the load average is above twice the CPU count, use long timeouts (900s or more), never 270-290s caps. Record pass/fail counts.

## 4. Commit and push

Commit in-scope files only. When the brief has a `GIT` line, push to that branch, and never force-push. Never touch files another tool stamped outside SCOPE (`git checkout <base> -- <file>` restores them). Write or update the step 2 ledger after the final commit: its `COMMIT` must be HEAD, and each round needs a fresh file.

## 5. Preflight

Run `office preflight; echo "rc=$?"`. Verdicts: `ready` (0): run the `next:` line exactly as printed (`. <agent.env> && office submit`).
`fix` (1): apply each `fix:` line, then preflight again. `wait` (75): the task is held by a plan finding (RECHECK or INTAKE_GAP; v3.1: a
plan defect) or an amendment and you still hold the lease; poll as below. `stop` (4): terminal; go to step 7 with `SUBMIT=refused: <stop line>`.

To wait, start this with `Bash` `run_in_background` (or the Monitor tool) and keep the session open:

```bash
for i in $(seq 30); do office preflight >/dev/null; rc=$?; [ "$rc" -ne 75 ] && break; sleep 60; done; echo "rc=$rc"
```

It polls task state, not event numbers. On exit 0, submit once. On exit 4 or after 30 minutes, report and stop. If an `AMENDMENT <id>` arrives while you wait, apply it, run `office ack <id>`, and go back to step 1. Preflight never reacquires a lost lease; only the orchestrator moves a task to a new holder.

## 6. Submit (or raise)

Use the exact line from `next:`, sourcing `agent.env` and running `office submit` in the same `Bash` call (shell env does not persist).
`lease-lost`, `superseded-dispatch` or `task-paused` is terminal: do not retry or investigate. For `outside-scope`, revert the file or run
`office raise --kind scope-request --path <file> -- "<reason>"`.

**Raise, do not submit, when you cannot finish** (a decision, a blocker, a file outside SCOPE): `office raise [--kind question|blocker|scope-request] [--path <file>] -- "<text>"`, no commit needed. It blocks your task and wakes the orchestrator, and the same text again records nothing. Stop and wait: the answer arrives as a message (or in the next session's brief if you ended). It never changes your contract; a scope-request needs a contract amendment, which unblocks you. Then continue and resubmit.

## 7. Report

The brief's `FINAL REPORT` items come first. Then end the message with exactly one line:

```
TASK=<id> COMMIT=<sha> PUSHED=<yes|no> CHECKS=<pass|fail + counts> SUBMIT=<accepted Rn | refused: exact reason | not attempted> NEXT=<what the orchestrator must do>
```

Then stop.
