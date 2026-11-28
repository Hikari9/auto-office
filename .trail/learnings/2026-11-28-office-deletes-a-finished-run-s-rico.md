---
title: "Office deletes a finished run's worktree only when its work is provably on main; unmerged work is kept with a patch"
author: Rico
date: 2026-11-28
branch: main
cost: { minutes: 140, tokens: 878397 }
id: l-0525d211
scope: repo
status: active
src: agent
touches:
  - src/office/cleanup.py
  - tests/v31/test_issue_1384_worktree_gc.py
  - src/office/prune.py
  - tests/v31/test_prune.py
  - CHANGELOG.md
  - SKILL.md
  - docs/orchestrator-reference.md
  - plugin-dist/docs/orchestrator-reference.md
  - plugin-dist/skills/auto-office/SKILL.md
---

## Decided
Office removes a finished run's worktree or local branch only on a content proof that the work is on the live default branch:
- ancestry of the default branch tip;
- every changed path equal on the tip;
- a squash or per-commit patch-id match (per-commit only when there are no merge commits);
- a merge-only branch whose merge into the default branch changes nothing;
- a PR from that exact commit merged into the default branch.

A landed run's own commits that are reachable from any live origin tip count as superseded. Everything else is kept, with a .patch and a full .diff saved under worktree-patches/. `office prune -f` keeps unmerged work too. Rico's deputy confirmed this as requirements r3 on 2026-11-28 (#1384).

## Tried and ruled out
- Commit-title matching: an unrelated commit titled like a main commit counted as merged.
- Removing commits reachable from any origin branch: an abandoned PR branch is recoverable but not merged.
- prune -f deleting unmerged work after saving a patch: the review blocked it (F1), because the brief keeps unmerged worktrees.

## Watch out
- git merge-tree --write-tree writes objects. Point GIT_OBJECT_DIRECTORY at a temporary directory, with GIT_ALTERNATE_OBJECT_DIRECTORIES set to the repo's objects, so a dry run writes nothing.
- The destructive recheck must query herdr fresh, not reuse the cached pane list.
- gc --dry-run took about 90 minutes on a host with 700 worktrees because it sizes every folder (#1387).

In the code: src/office/cleanup.py merge_proof, _merged_by_content, save_patch, gc, auto_gc; src/office/prune.py _prune_one; tests/v31/test_issue_1384_worktree_gc.py.
