---
name: auto-closeout
description: Internal Auto Office v3 closeout spoke. Use after implementation/review to reconcile findings, validate completion evidence, report branch/PR state and real blockers, record pinned runtime hashes and telemetry, enforce the human merge-to-main boundary, and finish family state without claiming success from model confidence alone.
---

# Auto Closeout

A family may report implementation complete only with evidence for: Outcome; Key implementation; Validation; browser/runtime evidence when applicable; Review result; PR/branch state; Remaining real blockers; Pinned runtime hashes.

Reconcile all pending findings and dispositions, stale holders/leases, plan/packet versions, uncommitted state, and validation evidence.

Record telemetry/outcomes before declaring completion when the recorder is available; telemetry write failure may fail soft, but surface it.

Never merge policy/self-improvement changes to main autonomously. Human merge-to-main remains mandatory.

**Reclaim the dispatch surface.** A run that ends leaving its workers parked is not closed out.
Once a ticket's work is merged and its evidence lives somewhere durable (the PR body, the
resolution comment, a findings file), close the panes that produced it — a finished worker's
scrollback is not a reason to keep a pane, because the evidence should already have been lifted
out of it. Close, in this order:

- panes whose agent produced nothing (a mis-routed dispatch, an agent that died at startup);
- panes whose work is merged and whose findings are recorded;
- the run's tab or workspace once every pane in it is closed.

Keep a pane only while its agent may still be resumed for another round — a reviewer mid-round,
an executor awaiting fixes. Never close a pane you did not create, and never close one hosting a
`working` agent. Run this reclamation on **every** closeout, not only the last one in a session:
panes accumulate silently across waves, and an operator who cannot read the layout cannot
supervise the dispatch, which is the entire justification for visible workers.

After closeout, run eligible lazy maintenance and, when justified, hand sanitized proposal candidates to `auto-self-improve` in a separate worktree/branch.

**Cleanup post-merge.** Once the plan is approved, merge the PR to `main` unless there is an active blocker. After merge, clean up the local environment: switch back to `main`, pull the latest changes, remove the temporary worktree (`git worktree remove <path> && git worktree prune`), and delete the local feature branch (`git branch -d <branch>`). Do not remove worktrees or force-delete branches with uncommitted state without explicit instruction.
