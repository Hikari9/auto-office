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

## 2. One-round self-review (#938)

Follow the brief's single-tier self-review contract, regardless of gear size or blast radius. Run the task's
targeted deterministic checks and examine the actual diff against its acceptance criteria yourself, once.
Office funds at most **one separate read-only combined audit**, not a four-specialist swarm.
Do not start a second executor subagent or repeatedly review the fix diff.

The mandatory producer lenses are **edge cases** (invalid/empty inputs, error/restart/retry paths) and
**test strength** (tests that only assert success trivially, missing negative cases, tests that do not reach the
change). Review **platform/build** only when the changed diff touches scripts, CI, packaging, OS or
process code. **Security** (auth/permissions, secrets, input parsing, production writes) is no longer a
producer lens; Office enforces a separate independent **convergence security pass** on sensitive lanes.

Record findings in untracked OFFICE_SELF_REVIEW.md after the final commit using the brief's format:
COMMIT full-SHA, ROUND 1, LENS edge-cases reviewed, LENS test-strength reviewed, LENS platform
reviewed or skipped with reason, LENS security skipped reviewed at convergence, followed by FINDING
severity lens file:line | summary | disposition lines. Keep the original severity. Fix medium/high
findings within scope, add or strengthen a test, and record fixed <test path> mutation=failed
(the fix must make the test fail when reverted); other dispositions are out-of-scope, dismissed <reason>,
contract-conflict accept=<n> or open. Open medium/high findings stop at ROUND 1; there is no round 2.

**Automatic main-change proof:** office preflight creates a disposable checkout of the task commit, reverts
only non-test source changes, runs a targeted test command from the task's checks and requires a failure,
then restores the commit and requires a pass. The test-strength lens remains mandatory: this proves the
tests reach the implementation, not that they catch every subtle bug. A missing test command or failed
red/green proof blocks substantive source submission. Never perform destructive cleanup in the executor
worktree; Office owns the disposable proof checkout.

**Budget:** self-review targets 10–15% of task tokens, capped at 15%. If the budget is exhausted, stop
auditing and add TRUNCATED self-review truncated: budget cap to the ledger. Convergence must scrutinize the
truncated task. Instrumented per-task spend is a separate future capability; do not claim exact metering.
An empty or read-only task needs no producer ledger. If in doubt, follow the current brief and preflight
messages, not instructions from older runs or skill snapshots.

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
