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
role floor → task shape → quota reserve. Unknown quota is not unlimited. Quota probes cache per harness for 5 minutes (OFFICE_QUOTA_CACHE_TTL seconds); a host-load timeout is retried once and reported unavailable; `office status` lists affected live tasks. Never lower factual safety gates to save money or quota. User denial blocks automatic selection, probing and convenience manual paths; scoped user overkill affects matching automatic role/size choices only. Free models (`free_model` rows, or an uncatalogued opencode `-free` slug) are excluded unless the user opted in with `office onboard --enable-free-models`; the router reads that consent itself when a caller passes no `allow_free_models`, while an explicit caller value takes precedence. This consent is an eligibility gate, not a preference veto or a routing advantage. Once opted in, a qualified free model competes under the normal routing policy: preferred-seed membership is a ranking signal where that policy uses it, and cost is one factor alongside effectiveness, capability fit, reliability, speed, quota and user preference. A free model can beat an available preferred route when the full assessment favors it, lose when another route scores better, and remain eligible as a fallback when the preferred route is unavailable. Opted-in free routes can also enter discovery trials under the ordinary discovery policy, role floors, hard gates and trial restrictions; preferred-seed membership does not exclude a free route from trials. Without opt-in, free models remain excluded from automatic selection and discovery trials subject to existing explicit authorization and override rules. Explicitly named routes and free routes in the preferred list retain their normal treatment subject to consent and factual gates. `--as` with no effort is free if any matching catalog row is.
Valid user `RecordedOverride` and declared routes retain explicit authority within factual gates and denial policy. Policy-derived `trial_eligible` is a separate builder-only path, never a forged override or trust act. User trust promotion via `office approve trust <harness/model@effort>` (with or without `@version`) resolves aliases through catalog expansion, recording on every triple routing reads for that route (both the alias row and its `alias_resolved_to` concrete row when both are candidates). Trust approval checks and reports any remaining gate that still blocks the route (conformance probe required, discovery off or blocked, not dispatchable) along with its remedy, and does not claim the route is usable when gates remain. For an existing run with discovery disabled in its pinned policy, the remedy directs the user to enable discovery at the applicable config tier and then authorize `office config --run <id> --apply-routing --quote "<words>"` for this run; if config resolution fails, it reports how to repair the unreadable config instead of suggesting that enabling discovery alone clears the gate.

**Executor and worker recommendation (#300):** after qualification, learned eligibility (stage 7) and an
user-configured budget ceiling on expected cost to success (stage 8) apply. Without a genuine run/repo/user ceiling, cost ranks rather than excludes; disclose its source and retain old pinned policy. Every remaining model × effort route is
scored on success probability (benchmark prior, overtaken by comparable local evidence), expected cost to
success, expected time to success, quota headroom and weighted user preference. The result is a slate: a primary
and up to two fallbacks, each with a reason, one strength and one weakness.
- Close calls (within 5% utility) are settled by a seeded, reproducible draw; a clearly weaker route never wins it.
- Exploration of under-tested qualifying routes is small and capped.
- No harness or family diversity is forced; parallel tasks in one wave spread softly when routes are close.
- `preferred_seed` is weighted evidence for these roles, never the winner by itself. Lineage supersession (#811): within one named lineage and effort, a qualifying newer generation (6.1 Sol over 5.6 Sol, 6.1 Terra over 5.6 Terra) takes its predecessor's slot and the predecessor follows as its fallback. Only qualified routes move; a missing effort, a failed gate, a declared route or a user seed that ranks the older one first leaves 5.6 where it was. Different lineages keep the utility order.

**Discovery (#494):** keep invocation support, learned quality and adapter trust separate. A fresh exact probe may qualify bounded reversible S/M executor/worker trials with local/repo blast radius, isolated worktrees and normal tests, independent review and verification. Caps: 2 probes/run, 1 trial/run, 15% of rolling 20 builder decisions, reserved atomically. Require a recorded known-working fallback, current safety gates and bounded allocation; manual pins skip discovery. An unreadable user or repo routing policy refuses with `policy-unreadable` (nothing offered, declared routes included). Planner/reviewer/verifier/final gate roles and production, irreversible or protected-write work never trial. See `../../docs/route-discovery.md` for fingerprints, failure classes, config, audit and rollback; only that shared policy governs trials. Discovery ships enabled for executor/worker; runs pinned earlier stay off, and `routing.discovery.enabled: false` at user or repo tier rolls it back. The first live trial ended `abandoned` (headless launch did not write `agent.env`; fixed); a second reached `trial-accepted` with no manual step.

**Planner and reviewer routes** still use the advisory `preferred_seed` anchor and the `cost_policy` order (including `balanced_money_band_percent`) after qualification, until their routing is redesigned.

Quota-wall cooldowns block only the exact route that hit the wall, across runs and Office processes. A provider reset time sets the expiry; otherwise `quota.cooldown_minutes` applies (15 minutes by default). A successful dispatch on that route clears the cooldown early.

## Who decides what

- **Planner:** accepts the slate by default; may set `route: <primary>, <fallback>, <fallback>` and `route_why:` on
  a task in PLAN.md. A primary outside the close-call band needs a concrete `route_why`, or the ranked slate stands.
- **Dispatch:** re-checks live quota, trust and learned eligibility, runs the planned primary, and falls back in
  the recorded order only when fresh evidence rules a route out, naming why. It never runs an unplanned route: an
  exhausted slate stops, and `office dispatch <task> --reroute` routes from current evidence.
- **Trial recovery:** use only the recorded known-working fallback after confirmed failed-worker process-tree exit and lease/session release; unconfirmed exit blocks replacement. Preserve started work and manual pins; never run a second live writer.
- **Route authority (#707):** `dispatch --as` and `rerun --as` without `--quote` record an orchestrator choice with ordinary trust, capability, quota and user-policy gates. Add `--quote "<user's words>"` only for an actual user override; preserve those words in the audit trail.
- **Rerun and effective route (#426, #721):** the task records the route it runs; a rerun or redispatch keeps it, and `office rerun <task> --fresh --reroute` routes again. Any swap logs a `route.changed` event (old, new, reason, actor, time); `office amend route <task> --as <h>/<m>[@e] --quote ".."` declares a pending or accepted task's future route or re-records a live one. Accepted producers stay immutable; the future pin applies on the next legal reopen. Declared-route redispatch waives only unverified adapter trust; quarantine and every later routing stage, floors included, still apply. See `protocol/routing.md`.
- **Learner:** attributes failures (route, plan, environment, reviewer, mixed, unknown) before learning, decays stale evidence,
  and changes learned eligibility only after maturity plus a held-out replay. It cannot change success definitions, attribution rules, factual gates or trust acts.

## Reading a decision
- The plan diagram shows each task's Inline Slate. Show it to the user as printed.
- `office inspect route <task>` adds the evidence matrix and what dispatch did; `--json` is the full audit
  record (`../../schemas/routing-decision.schema.json`), including immutable attempt evidence, reason classes, freshness, caps, preference source and recovered launches. `office inspect learner` shows quality learning, never trust promotion.
- Publish `selection_disclosure` before any executor or reviewer launch and keep it in the dispatch record. The reason must name the deciding evidence; "best model" is not a reason.

**Legacy:** runs pinned to Auto Office 3.0 use the `scripts/office_runtime.py` router
(`references/legacy-3.0-router.md`); never use it for a 3.1+ run.
