---
title: Choose fix or follow-up for a review finding once; switching fix to follow-up leaves the task reopened with nothing to fix
author: Rico
date: 2026-11-14
branch: main
cost: { minutes: 23, tokens: 59077 }
id: l-ac0b50ea
scope: repo
status: active
src: agent
touches:
  - tests/test_routecmd_hydrogen.py
  - CHANGELOG.md
  - src/office/_version.py
  - src/office/routecmd.py
  - adapters/seed/agy.yaml
  - docs/route-discovery.md
  - plugin-dist/adapters/seed/agy.yaml
  - plugin-dist/docs/route-discovery.md
  - src/office/route_probe.py
  - tests/test_route_probe.py
---

## Decided
Decide a convergence finding's disposition before running anything. `office disposition <lane>:F<n> fix` reopens the owning task at once. Re-dispositioning the same finding as `follow-up` afterwards does not restore the task to accepted. The task stays `changes_required`, `office land` refuses with not-ready, and a rerun worker stops at preflight with "fix round ... but no open findings or amendments are recorded". The way out is `office amend <task> -- "<what to fix>"`, which hands the fix to the worker.

## Tried and ruled out
- Flipping `fix` to `follow-up` to skip a rerun that crashed (a manual Hydrogen route rerun hits #1156). The task stayed reopened.
- Rerunning on the recorded Hydrogen route. It crashes in the probe cache lookup (#1156), so the fix round went to another route with `rerun --fresh --as <route>`.

## Watch out
Low test-only findings after the first fix round give diminishing returns. Mark them `follow-up` from the start instead of `fix`.

In the code: office disposition / office rerun / office amend flow. Run ff8047b2 hit it on 2026-11-14. Rerun crash: route_probe._cache_row dict(row) on a tuple row (#1156).
