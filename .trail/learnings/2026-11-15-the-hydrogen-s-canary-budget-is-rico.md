---
title: "The Hydrogen S canary budget is read from live config so open runs join it, and its rows are hidden from the old trial path"
author: Rico
date: 2026-11-15
branch: main
cost: { minutes: 34, tokens: 233616 }
id: l-82d70d93
scope: repo
status: active
src: agent
touches:
  - adapters/seed/agy.yaml
  - docs/route-discovery.md
  - plugin-dist/adapters/seed/agy.yaml
  - plugin-dist/docs/route-discovery.md
  - src/office/candidates.py
  - src/office/db.py
  - src/office/dispatch.py
  - src/office/hydrogen_trials.py
  - src/office/routecmd.py
  - src/office/routing.py
---

## Decided
On 2026-11-15 (#1168, #1169) Rico wanted the 10-task Hydrogen S canary to fill fast, including from runs that were already open. The budget `routing.hydrogen_trials.canary_tasks` (0..10) is read from the live config, because it is one global cap counted in runs.db. Everything that grants access stays pinned per run: the `enabled` flag, the exact user allow for agy/gemini-4-hydrogen@medium at S, run_safe, the exact probe pass, and one canary per run. So the live budget only reorders routes a run already admits.

## Tried and ruled out
- Booking canary rows as `experimental=1` and letting the old trial path count them. Luna flagged that canary dispatches then blocked later discovery trials (one-trial-per-run, rolling-cap).
- Booking them as `experimental=0` instead. That would have shortened the trial path's 19-opportunity cold start.

## Watch out
- `hydrogen_trial_opportunities.canary`: 1 = counted canary, 0 = canary launch that lost the budget race (not counted), NULL = trial-path row. The trial path's snapshot() and book() read only `canary IS NULL`.
- Open runs that started before Hydrogen was enabled (2026-11-14) never join the canary. Admitting them would mean overriding their pinned policy, which is an access widening.
- The shipped default config lacks `canary_tasks` (#1171), so `office config --user` needs `--force` to set it.
- Changing agy's effort mapping changes adapter_hash. Re-probe every agy effort after a reinstall; all four passed fresh on 2026-11-15.

In the code: src/office/hydrogen_trials.py (live_budget, canary_active, book_canary, snapshot), routing._adaptive (canary preference), dispatch.settle_discovery, candidates.route_role.
