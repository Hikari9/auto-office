# Self-review ledger: run c7f9f956 T1 (#1384)

One focused review round by an independent subagent (Sonnet) over the diff, then fixes.

| # | Severity | Finding | Disposition |
|---|----------|---------|-------------|
| 1 | high | Title-only proof treated unrelated work as merged when a commit subject matched any line in base history | fixed: title proof removed; replaced by a path-content proof (every changed path equals the default branch tip). Test: test_title_match_alone_never_proves_a_merge, mutation=failed (fails with the old title proof) |
| 2 | high | Branch deletion saved no .patch before dropping a ref proven merged by content | fixed: save_patch before update-ref -d for any non-ancestry proof |
| 3 | high | prune -f could force-remove worktrees without saving unmerged commits | fixed: prune -f (the explicit delete) runs the engine first, keeps dirty, live, locked and in-use trees, and saves a .patch for every unmerged worktree before removing it; no patch, no removal. Test: test_prune_saves_a_patch_for_unmerged_work_and_keeps_dirty_trees |
| 4 | medium | Exceptions (git timeouts) could abort gc or a close | fixed: per-run broad except in gc, auto_gc never fails a close, preview guards remote tips, merge_proof in branch deletion guarded |
| 5 | medium | Herdr pane without a cwd failed open | fixed: fail closed (None) |
| 6 | medium | Performance over ~700 worktrees | fixed: pane list cached 3 s, base patch ids cached per (repo, merge-base, tip), per-sweep deadline, budgeted pass orders least-recently-swept first |
| 7 | medium | PR proof ignored the PR's base | fixed: --base and baseRefName must be the default branch |
| 8 | medium | Per-commit patch proof ignored merge-commit content | fixed: skipped when HEAD has merges beyond the merge-base |
| 9 | low | Landed proof broader than its label | accepted: landed-run online commits are superseded; a .patch is now saved for worktrees and branches before removal |
| 10 | low | merge-tree --write-tree writes loose objects during a dry run | accepted: no refs, worktrees or runs.db change; dry-run test snapshots refs, worktree list, cleanup_runs and patches |

Test-strength gaps closed: closed-run worktree recorded by a live run's dispatch, pushed office/* remote branch survives, dry-run snapshot equality.

Changed existing expectations:
- test_closeout: merged branches are now deleted squash-aware, so the old git branch -d refusal line is gone.
- test_issue_783_cleanup: an autouse fixture sets OFFICE_NO_AUTO_GC=1 because those tests cover the opt-in cleanup command, and abandon now runs gc.
