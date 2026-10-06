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

### 0. Confirm target

Run from inside the run's repository. `office status` must name the run you mean; with several runs open, pass
`--run <id>`. A refused or missing run still ends with an `office close done — stopped early (...)` line: read it,
do not guess another target.

### 1. Verify the gate

Close needs every task accepted, integration verified, and the landing recorded. `office close` refuses
(`close-blocked`) otherwise and its `next:` names the step. Under `convergence-v1` every APPROVED finding needs an
`office disposition` and every required lane or shared-scope review is APPROVED or waived by landing authority.
Uncommitted work, an un-run gate, or a branch with no PR is a defect of the loop: report it. Close never commits
leftovers for you.

### 2. PR state and the merge boundary

`main` stays the human's unless the user chose `merge` or `e2e` at intake (or answered `office land --merge|--e2e
--quote`). Plan approval alone never authorizes a merge; `office close` itself never merges or arms automerge.

- Merged by `office land`: `office close`.
- Merged through a PR Office did not open: `office close --landed-externally <pr-url>` (add `--quote` with the
  user's words when the merge commit does not contain every accepted revision).
- Not authorized to merge: `office close --handoff <pr-url>`. The PR is marked ready, and base sync and worktree
  removal wait until the user merges.

### 3. Document

Before closing, update the docs the repo already maintains for this change (CHANGELOG, README, doc comments) and
make the PR body describe the change accurately. Do not invent a new docs file. `office close` prints a
non-blocking `warning:` when the diff changes code but touches no CHANGELOG/README the repo keeps; act on it or
say why it does not apply.

### 4. Sync base and remove worktrees (runtime)

On a merged close the runtime:

1. `git fetch origin <base>` and fast-forwards local `<base>`: by ref when no worktree has it checked out,
   `git merge --ff-only` in the checkout when that tree is clean, then verifies local `<base>` == `origin/<base>`.
2. If `<base>` is checked out in a tree with uncommitted changes it is left untouched, the sync is reported as
   skipped, and `next:` tells you to ask the user (native question tool) whether to fast-forward it. Ask.
3. Removes only the worktrees Office created for this run (its worktree root) and its `office/<run>/` branches
   with `git branch -d`, then `git worktree prune`. A worktree with changes no revision recorded is kept and
   named; a branch `-d` refuses is kept and named. Never `-D`, never someone else's worktree.

### 5. Close loops

- Panes: `office close` snapshots then closes the run's dispatch panes and tab. Afterwards read `herdr pane
  list`; a surviving pane needs a reason you can state. Never close a pane you did not create or one hosting a
  `working` agent (`herdr-close-panes` for the ledger sweep).
- Route defects: a route the harness could not invoke goes to `auto-self-improve` in its own worktree/branch;
  policy or self-improvement changes never merge autonomously.
- Issues this run resolves (`Closes #N` only fires on the default branch), scratch files under
  `<state_dir>/tmp`, and any user question never answered: surface them now.

### 6. Report

Every `office close` ends its output with one line: `office close done — <summary>` (for example
`PR #12 merged, main synced, 2 worktree(s) removed` or `handed off <pr>, worktrees kept`). Relay it as the final
line of your report, on every path, including a refused close.

## Quick Reference

| Situation | Action |
|---|---|
| Several runs open | `office close --run <id>`; never guess |
| `close-blocked` | Follow its `next:`; finish the loop, do not force |
| Merged via `office land` | `office close` |
| Merged via a PR Office did not open | `office close --landed-externally <pr-url>` |
| Merge not authorized at intake | `office close --handoff <pr-url>`; worktrees kept |
| Docs warning | Update the CHANGELOG/README the repo keeps, or say why not |
| Base checked out and dirty | Sync skipped; ask the user (native question tool) |
| Worktree kept as dirty | Report it as a defect; never auto-commit |
| Branch kept (`-d` refused) | Report it; never `-D` |
| Any path | End with `office close done — <summary>` |

## Common Mistakes

- **Merging because the plan was approved.** Only an intake `merge`/`e2e` choice or the user's quoted answer
  authorizes it.
- **Handing off, then deleting worktrees.** A handoff PR may need another round; keep its workspace.
- **Committing leftovers to make close pass.** Uncommitted work is a loop defect to report, not to hide.
- **Fast-forwarding a dirty base checkout.** Ask the user; their uncommitted work outranks a tidy sync.
- **Reporting "merged" when only GitHub moved.** Done means local `<base>` == `origin/<base>` too.
- **Dropping the report line on a refused close.** The `office close done` line says what state was reached.
