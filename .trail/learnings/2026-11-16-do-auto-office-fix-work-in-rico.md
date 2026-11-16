---
title: "Do auto-office fix work in a separate worktree, never in the shared checkout Office installs from"
author: Rico
date: 2026-11-16
branch: fix/1213-prefail-tests
cost: { minutes: 2, tokens: 14997 }
id: l-ef47d09d
scope: repo
status: active
src: agent
touches:
  - docs/contract-suite.md
  - docs/test-retirement.md
  - src/office/_version.py
  - src/office/rerun.py
  - tests/v31/test_executor_context.py
---

## Decided
Make code changes in a git worktree off origin/main, not in ~/Git/auto-office. The live `office` tool is installed from that shared checkout. Any agent that runs a reinstall or a doctor "repair" builds whatever branch and WIP sit there. On 2026-11-16, a deputy repaired a "stale" install while an unmerged fix branch (3.28.1) was checked out. That fix became the live Office for 4 active runs before it was reviewed.

## Tried and ruled out
Trusting that nobody reinstalls mid-task. The doctor "repair" path reinstalls without checking which branch or dirty state the checkout holds.

## Watch out
The install is a non-editable copy (uv receipt `directory = ~/Git/auto-office`), so switching branches afterwards does not change the live tool. Only a reinstall does. If an unmerged build goes live, either merge quickly and reinstall from merged main, or reinstall from a clean main worktree. Then run `office doctor` and confirm `office --run <id> status` for each live run.
Also: the global git ignore lists `.trail/`, so new learnings need `git add -f`.

In the code:
- worktree: `git worktree add ../auto-office-<topic> -b <branch> origin/main`
- install receipt: ~/.local/share/uv/tools/auto-office/uv-receipt.toml
