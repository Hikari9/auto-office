---
name: auto-closeout
description: Internal Auto Office v3 closeout spoke. Use after implementation/review to reconcile findings, validate completion evidence, report branch/PR state and real blockers, record pinned runtime hashes and telemetry, enforce the human merge-to-main boundary, and finish family state without claiming success from model confidence alone.
---

# Auto Closeout

A family may report implementation complete only with evidence for: Outcome; Key implementation; Validation; browser/runtime evidence when applicable; Review result; PR/branch state; Remaining real blockers; Pinned runtime hashes.

Reconcile all pending findings and dispositions, stale holders/leases, plan/packet versions, uncommitted state, and validation evidence.

Record telemetry/outcomes before declaring completion when the recorder is available; telemetry write failure may fail soft, but surface it.

**Close recorded routing defects.** Run `python3 scripts/office_runtime.py check-route-defects
--state-dir <run-state-dir>`. Do not report complete on exit 2. Read which exit 2 you got:

- `error: no_run_state` — there is no `state.json` at that path, so nothing about this run was
  gated: no pinned hashes, no spoke receipts, no defect record. Either you passed the wrong
  `--state-dir`, or the lifecycle never ran `start` and is not a run at all. Do not paper over it
  by reporting the closeout as gated; say plainly that run state is absent and which gates
  therefore did not apply.
- an `unresolved` list — this run emitted a route the harness could not invoke and nothing has
  amended the catalog yet: dispatch the `auto-self-improve` subagent, then
  `resolve-route-defect --id <id> --proposal-ref <branch-or-PR>`.

Exit 0 means a started run with every recorded defect amended. It never means "no run here".

**Reclaim the dispatch surface.** A run that ends leaving its workers parked is not closed out.
Once a ticket's work is merged and its evidence lives somewhere durable (the PR body, the
resolution comment, a findings file), close the panes that produced it — a finished worker's
scrollback is not a reason to keep a pane, because the evidence should already have been lifted
out of it. Close, in this order:

- panes whose agent produced nothing (a mis-routed dispatch, an agent that died at startup);
- panes whose work is merged and whose findings are recorded;
- the run's tab or workspace once every pane in it is closed.

Do this mechanically, not from memory. `scripts/hooks/close_finished_panes.mjs` (a Stop hook from
`install_hooks.sh`) closes ledger panes whose agent is `done` or gone, and is safe to run by hand:
`node scripts/hooks/close_finished_panes.mjs < /dev/null`. It reads only the
`office_spawn.sh --pane-id` ledger, never `herdr pane list`, so it cannot touch the user's own
panes, and no-ops without `herdr`. Run it, then read `herdr pane list` yourself — a surviving pane
needs a reason you can state.

Keep a pane only while its agent may still be resumed for another round — a reviewer mid-round,
an executor awaiting fixes. Never close a pane you did not create, and never close one hosting a
`working` agent. Run this reclamation on **every** closeout, not only the last one in a session:
panes accumulate silently across waves, and an operator who cannot read the layout cannot
supervise the dispatch, which is the entire justification for visible workers.

After closeout, run eligible lazy maintenance and, when justified, hand sanitized proposal candidates to `auto-self-improve` in a separate worktree/branch.

**Cleanup post-merge.** The run stops at a ready PR; `main` remains the human boundary. Merging on
the run's own initiative is prohibited — plan approval alone does not authorize it. Merge only with
an explicit per-run user statement authorizing it; absent that, mark the PR ready and stop. A policy
or self-improvement change needs that statement too, and never merges autonomously. After an
authorized merge, propose the sweep below and wait for separate explicit authorization for it.

**The sweep** runs inline, in this skill. Why: `references/why-closeout.md`.

1. Uncommitted work, an un-run gate, or a branch with no PR means `auto-loop` did not finish.
   Name that as a defect; the pre-merge validation evidence already covers a clean tree.
2. Sync the local base branch to match `origin/<base>`.
3. Remove only worktrees this run created:
   `scripts/office_worktree.sh cleanup --worktree <path>`, then `prune`, then `git branch -d`.
4. Close the loops — issues whose premise this run changed, scratch files outside the repo, and
   any question put to the user that never got an answer.
