---
name: auto-routing
description: Auto Office routing reference. Use when selecting or explaining a role route across harness, model, and effort; reading an executor route slate (primary + fallbacks); evaluating trust, capabilities, floors, quota, expected cost to success, speed, learned evidence, preference or exploration; or explaining a routing decision. The office runtime routes; this skill explains the scheme it applies (protocol/routing.md).
---

# Auto Routing

The `office` runtime routes every role. Do not route by hand; read the decision it records. The normative
scheme is `../../protocol/routing.md` (learning: `../../protocol/telemetry-learning.md`). Route
`harness@major × model_id × effort`, not model brand alone.

## The scheme

**Qualification (every role):** hard exclusions → derived adapter trust → required capabilities → absolute
role floor → task shape → quota reserve. Unknown quota is not unlimited. A successful probe is cached per harness for 5 minutes (OFFICE_QUOTA_CACHE_TTL seconds); a timeout under host load is retried once and reported as unavailable; `office status` lists live tasks routed on unknown or unavailable quota. Never lower a floor to save money or
quota. Only a valid, unexpired user `RecordedOverride` passes a derived gate; `--as`/`--route` are explicit user
authority.

**Executor and worker recommendation (#300):** after qualification, learned eligibility (stage 7) and an
absolute budget ceiling on expected cost to success (stage 8) apply. Every remaining model × effort route is
scored on success probability (benchmark prior, overtaken by comparable local evidence), expected cost to
success, expected time to success, quota headroom and weighted user preference. The result is a slate: a primary
and up to two fallbacks, each with a reason, one strength and one weakness.
- Close calls (within 5% utility) are settled by a seeded, reproducible draw; a clearly weaker route never wins it.
- Exploration of under-tested qualifying routes is small and capped.
- No harness or family diversity is forced; parallel tasks in one wave spread softly when routes are close.
- `preferred_seed` is weighted evidence for these roles, never the winner by itself.

**Planner and reviewer routes** still use the advisory `preferred_seed` anchor and the `cost_policy` order
(including `balanced_money_band_percent`) after qualification, until their routing is redesigned.

## Who decides what

- **Planner:** accepts the slate by default; may set `route: <primary>, <fallback>, <fallback>` and `route_why:`
  on a task in PLAN.md. A primary outside the close-call band needs a concrete `route_why`, or the ranked slate
  stands.
- **Dispatch:** re-checks live quota, trust and learned eligibility, runs the planned primary, and falls back in
  the recorded order only when fresh evidence rules a route out, naming why. It never runs an unplanned route: an
  exhausted slate stops, and `office dispatch <task> --reroute` routes from current evidence.
- **Rerun and effective route (#426):** the task records the route it runs; a rerun or redispatch keeps it, and `office rerun <task> --fresh --reroute` routes again. Any swap logs a `route.changed` event (old, new, reason, actor, time); `office amend route <task> --as <h>/<m>[@e] --quote ".."` declares a pending task's route (no fallback; redispatch waives only unverified adapter trust; quarantine and every later routing stage, floors included, still apply, unlike a first `--as`) or re-records a live one. See `protocol/routing.md`.
- **Learner:** attributes failures (route, plan, environment, reviewer, mixed, unknown) before learning, decays
  stale evidence, and changes learned eligibility only after maturity plus a held-out replay. It cannot change
  success definitions, attribution rules, factual gates or trust acts.

## Reading a decision

- The plan diagram shows each task's Inline Slate. Show it to the user as printed.
- `office inspect route <task>` adds the evidence matrix and what dispatch did; `--json` is the full audit
  record (`../../schemas/routing-decision.schema.json`). `office inspect learner` shows what was learned.
- Publish `selection_disclosure` before any executor or reviewer launch and keep it in the dispatch record. The
  reason must name the deciding evidence; "best model" is not a reason.

**Legacy:** runs pinned to Auto Office 3.0 use the `scripts/office_runtime.py` router
(`references/legacy-3.0-router.md`); never use it for a 3.1+ run.
