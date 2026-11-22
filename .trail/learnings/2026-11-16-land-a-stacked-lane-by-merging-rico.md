---
title: Land a stacked lane by merging only its last accepted head onto main
author: Rico
date: 2026-11-16
branch: office/2f15675a-land
id: l-6e3fe2f3
scope: repo
status: active
src: agent
touches:
  - src/office/land.py
  - scripts/build_plugin_distribution.py
---

## Decided
A serial lane where every task stacks on the one before it (T1>...>T7) can be composed by hand in one merge. Office's automatic rebase onto a moved main refused with a conflict. The tip task's accepted head already contains every earlier task, so merge only that head onto origin/main in a scratch worktree. Then prove the merge is correct in two steps: every task's current accepted revision is an ancestor of that head, and that head's tree equals the commit the lane convergence reviewer approved (REVIEWED_COMMIT in its reply). Only the conflict resolutions then need a fresh independent review, so review `git show --remerge-diff` instead of the whole change.

## Tried and ruled out
- `office land --rebase`: also refuses on a conflict and only prints the steps for composing by hand.
- Cherry-picking each task: the accepted revisions are merge commits from restacks, so cherry-picks duplicate or drop work.

## Watch out
A long-running run's version bump can collide with releases that land on main in the meantime. When composing, move the run's CHANGELOG entry above main's newest one and take the next free version. plugin-dist must be re-synced after the merge. The build script requires `--sync` or `--check`.

In the code: src/office/land.py raises Refused("rebase-conflict") when the automatic rebase fails. For the ancestry check, take each task's `office --run <id> inspect task <T>` "revision ... current" sha and run `git merge-base --is-ancestor <sha> <tip>`. For tree equality, check that `git diff --stat <REVIEWED_COMMIT> <tip>` is empty. Then `python3 scripts/build_plugin_distribution.py --sync`, and use `office close --handoff <pr-url>` after the PR merges.
