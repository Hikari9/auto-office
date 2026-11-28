---
title: "Executor routing follows the November scoreboard: Sonnet high for M and L, no Opus at L, every task must declare its size"
author: Rico
date: 2026-11-26
branch: main
cost: { minutes: 7, tokens: 35412 }
id: l-f7f4b897
scope: repo
status: active
src: agent
touches:
  - plugin-dist/docs/contract-suite.md
  - plugin-dist/docs/test-retirement.md
  - CHANGELOG.md
  - docs/contract-suite.md
  - docs/test-retirement.md
  - src/office/_version.py
  - src/office/route_learning.py
  - src/office/state.py
  - src/office/task_descriptors.py
  - tests/v31/test_routing_evidence_dispatch.py
---

## Decided
- Executor slates follow the 2026-11-26 executor scoreboard (first-try accept). Sonnet 5.5 high is the M/L workhorse (M 47%, L 34%). Sonnet medium at M was 26%, Haiku high at M 28% and Terra medium at M 12%, so those are denied at M and L.
- L has no Opus rung on the owner's words: "Just sonnet high first, then fallback to hermes sonnet if it's available, then agy/codex". Opus medium is denied at L, and Opus stays for XL only.
- Unsized plan tasks are refused at submit (owner's choice). Inheriting the run gear as the size was the other option and was rejected. Tasks already left unsized on an approved plan are grandfathered on resubmit and amendment.
- S stays Sonnet medium > Terra medium > Hydrogen medium. Haiku xhigh at S is a trial (#1360) and is not shipped yet.

## Tried and ruled out
- A per-run `office route override` to trial Haiku xhigh at S: it is scoped to one run with a 24h TTL. Haiku xhigh also has a shipped deny, a non-dispatchable catalog row and no Engineering Index score.
- Adding pi Sonnet as an automatic M/L fallback: it has no catalog row, and the owner said no.

## Watch out
- Neon medium is not denied at S, so a reroute can still pick it for an S task. Each task already routes by its own size.
- A hand-composed run landed by squash merge cannot be proven by ancestry: `office close --landed-externally` refuses. Close it with `--abandon "landed as #N"` or with the user's quote.
- A long run's version bump collides with releases on main. Bump past main's version at compose time.

In the code: catalog/executor-policy.yaml (executor_preferred, model_restrictions), catalog/role-policy.yaml (executor block), src/office/greenfield.py sizing_problems, called from plans.py submit_plan and amend.py.
