---
name: auto-closeout
description: >-
  Internal Auto Office closeout spoke for 3.x runs. Use when a run's work is accepted and landed (or ready to hand
  off) and it is time to run `office close`: confirm the target run, verify the gate, respect the PR merge boundary,
  update the docs the repo keeps, sync the local base branch and remove the run's worktrees, close loops including
  panes, and end with the `office close done` report line. Never claims success from model confidence alone.
---

# Auto Closeout

`office close` is the closeout. The runtime owns the mechanics (archive receipt, base sync, worktree removal,
pane reclamation, the report line); this spoke owns the order and the judgment around it. Follow `office status`
and its `next:` line; never run the 3.0 `office_runtime.py` helpers for a 3.x run.

## The Process

0. **Confirm target.** Run from inside the run's repository. `office status` must name the run you mean; with several runs open pass
   `--run <id>`. A refused or missing run still ends with an `office close done — stopped early (...)` line: read it, do not guess.
1. **Verify the gate.** Close needs every task accepted, integration verified and the landing recorded; otherwise `office close` refuses
   (`close-blocked`) and `next:` names the step. Under `convergence-v1` every APPROVED finding needs an `office disposition`, and every
   required lane, shared-scope or integrated review is APPROVED or waived. A round-cap waiver (`office waive`) leaves the verdict RECHECK
   and is not landing authority; landing needs the user's authorization. Uncommitted work, an un-run gate or a branch with no PR is a loop
   defect: report it. Close never commits leftovers.
2. **PR state and the merge boundary.** `main` stays the human's unless the user chose `merge` or `e2e` at intake (or answered
   `office land --merge|--e2e --quote`). Plan approval alone never authorizes a merge; `office close` never merges or arms automerge.
   - Merged by `office land`: `office close`.
   - A land that stopped after merging some task PRs: rerun `office land` (merged PRs are skipped; `--rebase` refuses
     `already-merging`). When GitHub reports a PR not mergeable, land recovers it by merging the default branch into the task branch
     (leased push, `pr.recovered` event) only if the remote head is the recorded one, the merge is conflict-free, and the open PRs
     compose to the reviewed integration tree. `merge-conflict`, `branch-moved` or `recovery-tree-mismatch` push nothing: compose by hand
     as the refusal says, then `office close --landed-externally <pr-url>`.
   - Merged through a PR Office did not open: `office close --landed-externally <pr-url>` (add `--quote` when the merge lacks an accepted revision).
   - Not authorized to merge: `office close --handoff <pr-url>`; the PR is marked ready, and base sync and worktree removal wait for the user.
3. **Document.** Update the docs the repo already maintains (CHANGELOG, README, doc comments) and make the PR body accurate. Do not invent a
   docs file. `office close` prints a non-blocking `warning:` when code changed but no CHANGELOG/README did: act on it or say why not.
4. **Sync base and remove worktrees (runtime).** On a merged close the runtime fast-forwards local `<base>` to `origin/<base>` and verifies
   they match. If `<base>` is checked out with uncommitted changes it is left alone, the sync is reported skipped, and `next:` tells you to ask
   the user (native question tool). It removes only worktrees Office created for this run and its `office/<run>/` branches (`git branch -d`,
   then `git worktree prune`); a worktree with unrecorded changes or a branch `-d` refuses is kept and named. Never `-D`, never someone else's.
5. **Close loops.** `office close` snapshots then closes the run's panes and tab; read `herdr pane list` afterwards and justify any survivor.
   Never close a pane you did not create or one hosting a `working` agent (`herdr-close-panes` for the ledger sweep). A route the harness could
   not invoke is captured by issue-only `auto-self-improve` through a cheaper read-only investigator; never propose a catalog change, branch, or PR. Surface issues this run resolves
   (`Closes #N` fires only on the default branch), scratch files under `<state_dir>/tmp`, and any user question never answered.
6. **Report.** Every `office close` ends with one line, `office close done — <summary>` (for example `PR #12 merged, main synced, 2 worktree(s)
   removed`). Relay it as the final line of your report on every path, including a refused close.

## Common Mistakes

- Merging because the plan was approved, or treating a waiver as approval or landing authority.
- Deleting worktrees after a handoff (the PR may need another round), or committing leftovers to make close pass.
- Fast-forwarding a dirty base checkout, or reporting "merged" when only GitHub moved (local `<base>` must equal `origin/<base>`).
