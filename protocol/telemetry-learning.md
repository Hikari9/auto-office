# Telemetry, attribution, learning, maturity

Store private local structured evidence in SQLite WAL mode. Core tables: runs, dispatches, findings, validations, routing_decisions, artifact_versions, ownership_events, outcome_labels, lineage. Keep writes append-oriented/deterministic.

Failure attribution must distinguish `model`, `harness`, `adapter`, `quota/account`, `environment/network`, `planner`, `brief`, `repository`, `verification`, `unknown`. Do not automatically punish the model for failures attributed elsewhere.

Outcome labels: `pending`, `verified_no_observed_failure`, `recurrence_failure`, `revert_failure`, `material_post_merge_defect`, `abandoned`, `environment_failure`. Observation ends at the earlier of 14 days after merge or the next three runs touching the relevant repo/surface. Label lazily on the next runtime maintenance pass.

Negative recurrence/revert/post-merge defects are stronger evidence than no-observed-failure is positive. Keep money cost, quota burn, wall clock, dispatch count, and review rounds as separate raw terms.

Memory tiers: hot = current triple/generation/full authority; warm = once-superseded/stale/reduced authority; dreamt = superseded twice, >90 days, or compacted/no numeric routing authority.

Maturity: apply difficulty/event weights to lineage evidence, clamp `P=max(0,cumulative points)`, then `age=100*(1-exp(-P/60))`. Failures move age backward. Maturity cannot remove absolute floors/no-self-approval/human merge/destructive safeguards. Scrutiny reduction requires >=30 labeled runs across >=3 repos/task shapes plus held-out predictive value.

Policy changes to reward weights, capability floors, maturity weights, or major routing thresholds require replay over comparable hot/warm rows. Refit only after >=20 labeled structured v3 rows globally; fit oldest ~80%, evaluate newest ~20%, never tune on holdout.
