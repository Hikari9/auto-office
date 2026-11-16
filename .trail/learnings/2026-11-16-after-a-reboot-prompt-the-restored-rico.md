---
title: "After a reboot, prompt the restored executor instead of rerunning it fresh"
author: Rico
date: 2026-11-16
branch: main
cost: { minutes: 1, tokens: 1243 }
id: l-57068373
scope: repo
status: active
src: agent
touches:
  - src/office/dispatch.py
  - src/office/cli.py
---

## Decided
After the Mac rebooted, Herdr restored the T5 executor's Claude pane and session. Office still counted that worker as live, so `office rerun T5 --fresh` refused to run. Prompting the restored session with a full brief (open findings, keep the uncommitted WIP, rerun the tests) restarted the work without losing its context or its uncommitted edits.

## Tried and ruled out
- `office rerun T5 --fresh --stash-wip`: blocked with `worker-live`.
- `office revoke T5`: would end a session that still holds the uncommitted F-2 work.

## Watch out
Pytest runs and background shells die on reboot, but the Herdr pane comes back. Before assuming an executor is dead, list the panes and look for one labeled with the task.

In the code: the worker-live check in src/office/dispatch.py blocks rerun with "blocked: worker-live: T5 still has a live worker (Dee3f8e4a)". Check with `herdr pane list` and look for a pane label "auto-office T5 ...". Then `office --run <run> prompt T5 -- "<brief>"` (src/office/cli.py prompt).
