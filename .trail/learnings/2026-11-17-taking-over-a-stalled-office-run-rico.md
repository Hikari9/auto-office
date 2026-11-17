---
title: "Taking over a stalled Office run: compose task heads by hand, final review on pi Luna"
author: Rico
date: 2026-11-17
branch: c2-verdicts
id: l-6e19da20
scope: repo
status: active
src: agent
---

## Decided
When Rico asks to expedite an Office run (auto-takeover), stop dispatching and compose the integration branch by hand from origin/main. Merge the newest head of each task worktree. A stacked task's head already contains its dependencies, so merge only the top of each stack. Bump the version yourself, run the focused test files, then get one independent review of the whole diff on pi GPT-6 Luna high.

## Tried and ruled out
Waiting for Office to restack each stacked task after a lane repair. Re-accepting one task reopened three dependents, and the version-bump task stopped at preflight ("fix round ... but no open findings") because a restack-only reopen counts as a fix round.
Wrapping the pi reviewer in `timeout`: macOS has no `timeout` binary, so the command exits 127 at once and looks like a finished review.

## Watch out
Give the reviewer its own detached worktree at the branch head, so its writes cannot reach the branch.
Check the reviewer's exit code and output before trusting a fast "completed".

In the code: git worktree add --detach <dir> <branch>; pi --print --no-approve --no-extensions --no-mcp --no-session --model openai/gpt-6-luna --thinking high < brief.md
