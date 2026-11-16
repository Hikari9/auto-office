---
title: "Reviewer engine rewrite: stack the tasks that edit the reviewer loop, keep each test check to one test directory"
author: Rico
date: 2026-11-15
branch: fix/codex-visual-submit
cost: { minutes: 8, tokens: 101808 }
id: l-4ff7a094
scope: repo
status: active
src: agent
touches:
  - ".office/plans/2f15675a/PLAN.md"
  - src/office/gates.py
  - tests/v31/test_gates.py
  - SKILL.md
---

## Decided
- Run 2f15675a (#1191) splits the reviewer-dispatch rewrite into one stacked engine lane plus one parallel transport lane. The engine lane goes T1, then T2 and T3 in parallel, then T4, T5, T7. Transport is T6. Almost every reviewer asymmetry edits the same reviewer loop, so parallel lanes there would conflict when Office composes them. T2 (worktree guard) and T3 (pin lookup) touch hunks far enough apart to run in parallel after T1.
- One version bump for the whole run, owned by the final task (T7).

## Watch out
- Most reviewer tests live in tests/v31/, not tests/. A task check that mixes tests/ and tests/v31/ in one pytest run breaks the `env` fixture (conftest collision). Put each new reviewer test in tests/v31/ and keep each check to one directory.

In the code:
- run_reviewer is src/office/gates.py:785-1069. Returns at ~917, ~1017, ~1037 and ~1065 each build the convergence dict and the legacy dict separately. The pin lookup is at ~815 and the worktree guard at ~907-942.
- Existing reviewer tests: tests/v31/test_gates.py, test_issue_790_review_route.py, test_convergence_reviewed_commit.py, test_issue_1030_review_submit.py, test_issue_1186_reviewer_shell.py, test_herdr_review_capture.py, test_issue_740_review_correctness.py.
