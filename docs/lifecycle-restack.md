# Dependency bases, restacks, leases and the moved run base

Office 3.3.x lifecycle rules for tasks that depend on several tasks, restack, are revoked, or are open when
`office land --rebase` moves the run onto a newer default branch (#398, #307, #361).

## A base that contains every dependency

A task with one or more `depends:` starts (dispatch, stacked start) and restacks (`office rerun`) from a base that
contains each dependency's accepted revision, else its current one:

- the dependency head that already contains the others, when one does (T3 depends T1,T2 and T2 is built on T1: the
  base is T2's head);
- otherwise an Office merge commit of them, made with `git merge-tree --write-tree` and `git commit-tree`, never
  checked out. The brief names it: `BUILDS ON T1, T2 (already in this worktree's base 1a2b3c4d5e6f, an Office merge
  of T1, T2)`.

When the dependency revisions conflict with each other, the dispatch or restack refuses with `dependency-conflict`,
naming the tasks and the paths, before anything is launched or merged. A task whose acceptance releases a stacked
start that hits this is paused and the orchestrator is signalled; the acceptance itself still commits.

On a rerun, Office merges each dependency revision the worktree lacks (never a rebase, so no force-push); a merge
that conflicts is aborted and the brief opens with `RESTACK FIRST` listing the merges to make in order.

## Leases between ordered tasks

A live lease on a dependent never blocks its prerequisite: tasks ordered by `depends:` may overlap, as in plan
validation, so the prerequisite's lease is not refused for the dependent's scope. Amending or rerunning the prerequisite proceeds; the dependent is
reported stale (`T7 waiting: built on T6 R1, but T6 was accepted on R2`) once the prerequisite is accepted again, and
`office rerun T7 --resume|--fresh` restacks it. Unordered tasks with overlapping scopes still exclude each other, and a
dependent still waits for its prerequisite's live lease.

## Preflight on a fix round

A fix round is ready when it has any of: open findings, a delivered amendment (to be applied and acknowledged), or a
restack (the packet records merged or conflicting dependency revisions). With none of the three it still stops.

## Revoking a worker on an accepted task

`office revoke T3` on a task that has an accepted revision and no newer one (the relaunched worker's submit was
refused, or it never submitted) releases the lease and keeps the task `accepted`. Late submits from the revoked
session are still rejected. An amendment the revoked session never applied is named in the output and stays unapplied:
`office amend T3` delivers it again.

## A task open after `office land --rebase`

`office land --rebase` moves the run base; a task reopened afterwards still has its worktree on the old base. The
orchestrator chooses per task, and Office never chooses:

| command | what it does |
| --- | --- |
| `office rebase T1 --move` | re-applies T1's change since its old base on the new base as one commit on it (a version bump is made again there). The branch is rewritten: force-push with lease if it was pushed. |
| `office rebase T1 --merge` | merges the new default branch into T1's branch. History is kept. |
| `office rebase T1 --record` | T1's worktree was moved or merged by hand: verify it holds the new base and record it. |

`--move` and `--merge` need the worker to have ended and the worktree clean (`office revoke T1` first). Every path
records the new base on the task's dispatch, so preflight and submit diff against it and never report the default
branch's own files as the task's edits. Until it is recorded, preflight of a worktree that already holds the new base
waits (and signals the orchestrator), and submit refuses with `base-not-recorded`.

A file both sides changed (a version field both bumped, say) refuses with `rebase-collision`, naming the files and
both ways to settle it by hand, then `--record`. Office does not resolve it, and leaves the worktree untouched.

## Landing a criss-cross branch

Restacks can leave a task branch and the default branch with several merge bases. GitHub reports such a PR
CONFLICTING although `git merge-tree` merges it cleanly. `office land` checks before each merge: with several merge
bases and a clean `git merge-tree`, it merges the default branch into the pushed task branch (a fast-forward push)
and continues; a real conflict refuses with `merge-conflict` naming the files, and nothing is merged.

## Requirements

The merge-tree based steps (the merged base, `office rebase --move`, the criss-cross check) need git 2.40 or newer
(`git merge-tree --write-tree`, `--merge-base`); an older git refuses with `git-too-old`.
