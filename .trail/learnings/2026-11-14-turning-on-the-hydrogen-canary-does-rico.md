---
title: Turning on the Hydrogen canary does nothing until agy can report its model and effort
author: Rico
date: 2026-11-14
branch: main
cost: { minutes: 15, tokens: 229601 }
id: l-a941dd0f
scope: repo
status: active
src: agent
touches:
  - ".office/plans/1b3df3a3/PLAN.md"
  - uv.lock
  - src/office/hydrogen_trials.py
  - src/office/route_probe.py
  - src/office/routecmd.py
---

## Decided
On 2026-11-14 Rico chose to land the route-policy wording fix (#1147) without proving a Hydrogen dispatch. The canary stays enabled in the user config but is dormant. Proving a dispatch moves to the follow-up #1148, which adds agy identity readback.

## Tried and ruled out
- Manual dispatch with `--as agy/gemini-4-hydrogen@medium --quote ...` is refused with `hydrogen-manual-unqualified` because there is no conformance receipt.
- `office doctor --probe-route agy/gemini-4-hydrogen@medium` is refused with `no-identity-readback`. agy prints no model or effort the harness itself reports, so the exact probe can never pass.
- Role allows and override receipts cannot help. `route_refusal` is a hard veto, and both the manual and the automatic path need a passing probe.
- Disabling the canary again was rejected. Rico wants Hydrogen to actually dispatch.

## Watch out
- Even after the probe passes, automatic trials hit a cold start. They wait until 19 eligible S/M low-risk executor opportunities are recorded, and then allow 3 per rolling 20 and 1 per run.
- A task check written as `uv run python ...` rewrites uv.lock, and the gate then marks the check stale. Use `uv run --frozen`.

In the code: src/office/hydrogen_trials.py (route_refusal, manual_conformance, trial_refusal "conformance-missing", snapshot cold_start), src/office/route_probe.py (no-identity-readback), config key routing.hydrogen_trials.enabled, runs.db table hydrogen_trial_opportunities at ~/.local/share/auto-office/runs.db.
