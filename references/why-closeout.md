# Why: closeout and learning rationale

Rationale for pinned rules in the "Closeout and learning" section of `SKILL.md`.

## Why every closeout reorganizes the dispatch surface, not only the last

Herdr panes accumulate across every closeout in a run, not only the final one. An agent that
only tidies panes at the very end leaves finished panes open through every intermediate closeout,
which is how the dispatch surface silently fills up with dead panes over a long run. Running
`close_finished_panes.mjs` at each closeout keeps the surface accounted for continuously instead
of relying on a final sweep to catch everything.

## Why the sweep syncs the local base branch

A PR merge lands on the remote only. Until the local copy of the base branch is pulled it still
points at the pre-merge commit, so a checkout looks as though it lost the work that just merged.
`merged` is not done until local and `origin/<base>` name the same commit. Observed on 2026-09-14:
after PR #91 merged, `git checkout v3-restoration` showed a tree with none of the merged changes,
which reads as a failed merge rather than an unpulled branch.

## Why worktree removal refuses a dirty tree instead of forcing

`git worktree remove` without `--force` refuses when the tree holds uncommitted or untracked
files. That refusal is the finding, not an obstacle: it names work that may exist in no other
place. The same reasoning makes branch deletion `-d` rather than `-D` — `-d` refuses an unmerged
branch, and on 2026-09-14 that refusal was the only thing standing between
`office/v3-restore/wave10/y1`, whose commit existed on no remote, and permanent loss.

`office_worktree.sh cleanup` took `--force` unconditionally until this was noticed, which meant
the documented tool did the opposite of the documented rule.

## Why self-improvement is propose-and-prove, not propose-and-ship

Agents may propose a policy change and prove it with replay evidence, but the maintainer decides
what becomes shipped policy. This split exists because a policy change made inside a live run
would let an agent alter the rules it is currently being judged against — the same conflict of
interest the no-self-approval invariant closes elsewhere in the lifecycle.
