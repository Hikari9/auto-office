# Telemetry, attribution, learning, maturity

Store private local structured evidence in SQLite WAL mode. Core tables: runs, dispatches, findings, validations, routing_decisions, artifact_versions, ownership_events, outcome_labels, lineage. Route learning (#300) adds route_audit, route_attributions and learned_eligibility. Keep writes append-oriented/deterministic. Learned routing state is private to the machine; it never rewrites the committed catalog.

Failure attribution must distinguish `model`, `harness`, `adapter`, `quota/account`, `environment/network`, `planner`, `brief`, `repository`, `verification`, `unknown`. Do not automatically punish the model for failures attributed elsewhere.

Outcome labels: `pending`, `verified_no_observed_failure`, `recurrence_failure`, `revert_failure`, `material_post_merge_defect`, `abandoned`, `environment_failure`. Observation ends at the earlier of 14 days after merge or the next three runs touching the relevant repo/surface. Label lazily on the next runtime maintenance pass.

Negative recurrence/revert/post-merge defects are stronger evidence than no-observed-failure is positive. Keep money cost, quota burn, wall clock, dispatch count, and review rounds as separate raw terms.

## Route learning (#300)

`src/office/route_learning.py` derives one outcome per ended executor/worker dispatch from runs.db (success: a revision it produced became the task's accepted revision). It groups them into task-route episodes, the learning unit: a fix round on the same route is one episode with two attempts. A failed episode is attributed before it teaches: `route` (material code-review or check rejections; weight 1.0), `mixed` (route and plan evidence, or a harness that exited before submitting; 0.5), `unknown` (0.25), and `plan`, `environment`, `reviewer` (0). Weight is further scaled by attribution confidence. A recorded outcome-label attribution outranks the heuristics. Successes carry full weight.

Evidence is pooled per route (harness × invocation model × effort) and weighted by context comparability (another playbook 0.5, another size class 0.75, fresh vs fix 0.75), by recency (half-life 120 days), and by staleness: outcomes recorded under another harness major count 0.25. A different model version is a different route. The scorer reads this as a Beta posterior whose prior is the pinned benchmark, so benchmark authority falls as `prior_strength / (prior_strength + n)`.

Learned eligibility changes automatically, at run close or abandon, when all of these hold: at least 10 route-attributed episodes across at least 3 runs; a 90% posterior bound past the threshold (lower ≥ 0.60 promotes to `learned-eligible`; upper ≤ 0.30 demotes to `learned-ineligible`; prior worth 4 pseudo-outcomes); and a held-out replay (the oldest outcomes fit, the newest ~20% held out) whose outcome rate agrees and whose log loss is no worse than the benchmark prior's. Each change is an append-only `learned_eligibility` row and a `learner.eligibility` event. A later row reverses it when mature evidence stops supporting it, and a recorded user override (stage 7) bypasses it.

The learner cannot redefine success, the attribution classes or weights, the maturity and replay rules, factual gates (capabilities, floors, quota, quarantine), or adapter trust acts: those are code, changed only through review. `office inspect learner` shows outcomes, standing changes, and the changes the evidence would make at the next close. `eval/routing_replay.py --runs-db` replays local history read-only and prints aggregates.

## Memory and maturity

Memory tiers: hot = current triple/generation/full authority; warm = once-superseded/stale/reduced authority; dreamt = superseded twice, >90 days, or compacted/no numeric routing authority.

Maturity: apply difficulty/event weights to lineage evidence, clamp `P=max(0,cumulative points)`, then `age=100*(1-exp(-P/60))`. Failures move age backward. Maturity cannot remove absolute floors/no-self-approval/human merge/destructive safeguards. Scrutiny reduction requires >=30 labeled runs across >=3 repos/task shapes plus held-out predictive value.

Policy changes to reward weights, capability floors, maturity weights, or major routing thresholds require replay over comparable hot/warm rows. Refit only after >=20 labeled structured v3 rows globally; fit oldest ~80%, evaluate newest ~20%, never tune on holdout. Adaptive routing coefficients (`routing.adaptive`) are calibration values held to the same rule: change them with `eval/routing_replay.py` evidence.
