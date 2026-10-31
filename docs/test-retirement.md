# Test retirement and traceability (#871 PR C)

Base: `origin/main` `d1402697`. Scope: tests that assert specific shipped model, harness, effort, floor or slate values, and tests that duplicate another retained test. Nothing under `src/`, `scripts/`, `catalog/`, `config/` or `adapters/` changed.

`PDC` below is `tests/v31/test_policy_data_contracts.py`, added by this change. Every expectation in it is read from the shipped policy data (`executor_slates.POLICY`, `route_policy.SEED_ROLE_RULES`, the shipped catalog) or is hand-written arithmetic on that data. It names no model, and it never computes an expected route with the selector it checks. It lives in `tests/v31/`, not `tests/contracts/`, because every file under `tests/contracts/` is digest-pinned in `src/office/verification_coverage.py` (amendment A2).

## Counts

### Same-host comparison

Measured on 2026-10-31 on an Apple M5 Pro Mac, macOS 26.7.1, 18 logical CPUs, CPython 3.14.8. The baseline is the run's `origin/main` base `d14026977392289164d1e09fe454728c71f65b28`; the after revision is T1's accepted `ae49b25a3e71fa89632e3cb028a0ee14a862e1c4`. A separate checkout with its own dependencies was used for the baseline. The timing runs overlapped each other and a separate full validation on the host, so the wall times are load-dependent and should not be read as a controlled speedup.

| Tier and measure | Before | After |
|---|---:|---:|
| Default: test modules with collected nodes | 202 | 200 |
| Default: test functions with collected nodes | 2,169 | 2,135 |
| Default: collected nodes | 4,016 | 3,856 |
| Default: wall time | 322.57 s | 308.54 s |
| `--all`: test modules with collected nodes | 305 | 303 |
| `--all`: test functions with collected nodes | 4,317 | 4,283 |
| `--all`: collected nodes | 6,407 | 6,247 |

From each checkout, collection used `uv run --frozen --extra test pytest -n 2 --collect-only -q tests` and `uv run --frozen --extra test pytest -n 2 --all --collect-only -q tests`. Module counts are distinct paths in the resulting node IDs; function counts are distinct node IDs after removing parametrization suffixes. These are collected functions, not a count of every function defined in source.

Wall time was measured once per checkout with `/usr/bin/time -p uv run --frozen --extra test pytest -n 2 -q tests`. The baseline reported 4,013 passed, 2 failed, 2 skipped; the after revision reported 3,854 passed, 1 failed, 2 skipped. Both failed `tests/v31/test_lineage_supersession_811.py::test_terra_61_rows_match_the_documented_scores_and_stay_gated` with a missing `dispatchable` key; the baseline also failed `tests/v31/test_issue809_run_gear.py::test_sonnet_claude_to_agy_self_fallback_applies_only_to_xs_xl_runs`. Each pytest result summary totals one more than its collection count; that discrepancy was not investigated. The default-tier run above is not the full suite. The full-suite result is under [Full suite](#full-suite).

### Full suite

`scripts/validate.sh --full` on T2 head `e0f9145f7a7e` (2026-10-31, same host as the table above): 6,084 passed, 10 failed, 154 skipped, 130 subtests passed in 2,269.60 s. This run is not a clean pass. The 10 failing nodes below also fail with `uv run --frozen --extra test pytest -n 2 --all` on base `origin/main` `d1402697`, with the same node set. None is in a file this PR changes. The result was recorded by the orchestrator; it was not re-executed in this worktree. Commits after `e0f9145f` change only documentation and `tests/test_test_retirement_record.py`, a parity check that was not part of the recorded run.

- `tests/test_release_identity.py::test_skill_checks_for_the_cli_before_anything_else`
- `tests/v31/test_issue_703_effective_checks.py::test_amended_run_check_replaces_superseded_integration_command`
- `tests/v31/test_issue_705_squash_close.py::test_owned_squash_pr_close_proves_heads_and_checks_without_fake_ancestry_or_consent`
- `tests/v31/test_issue_705_squash_close.py::test_squash_close_preserves_the_pinned_v31_review_contract`
- `tests/v31/test_issue_790_held_pointer.py::test_enter_that_cannot_submit_says_so_and_marks_one_same_route_retry`
- `tests/v31/test_landing_gaps.py::test_run_checks_failing_on_a_moved_base_rebase_and_continue`
- `tests/v31/test_review_reroute.py::test_the_review_reprompt_is_sent_only_inside_the_pane_identity_fence`
- `tests/v31/test_review_reroute.py::test_a_reprompt_that_stays_unsubmitted_names_the_review_only_command`
- `tests/v31/test_lineage_supersession_811.py::test_terra_61_rows_match_the_documented_scores_and_stay_gated`
- `tests/v31/test_rolling_review.py::test_plan_review_ends_at_first_pass_even_on_an_older_version`

### Draft comment for #796

Keep #796 focused on improving the performance of optional historical and full diagnostic runs. Ordinary PRs use the scoped gate in [validation.md](validation.md); this issue does not require preserving every historical test node as a mandatory per-PR check. Full validation remains required for the cases specified there.

### T1 inventory

| | Before | After |
|---|---:|---:|
| Test modules outside `tests/contracts/` | 298 | 295 |
| Collected nodes (`pytest --all --collect-only tests`) | 6,407 | 6,247 |
| Nodes removed | 224 | |
| Nodes added | | 64 (all in `PDC`) |
| Test functions deleted (rows below) | | 55 (0 rewritten) |

Check the row count against `git diff --stat`: 3 modules deleted (16 functions) plus 39 functions removed from 7 retained modules is 55 removed `def test_` lines, and no function is rewritten. `PDC` adds 21 functions (64 nodes).

## Fault injection

Each mutation was applied in a scratch copy of the tree (`PYTHONPATH` pointed at the copy), the listed tests failed, and the file was restored from the checked-out tree. The restored copy then passed all of `PDC`.

| Failure class | Mutation | Failing covering test |
|---|---|---|
| Size floor boundary | `executor_slates.benchmark_gate`: `raw < floor` to `raw <= floor` | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value` (5 nodes), `PDC::test_a_probe_is_gated_by_the_size_floor_like_a_dispatch` |
| Missing or non-numeric score passes a size floor | `benchmark_gate`: numeric-score check disabled | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value`, `PDC::test_a_floor_exception_waives_only_its_exact_scope` |
| Slate order | `ideal_seed` returns the slate reversed | `PDC::test_the_router_follows_the_preferred_slate_order_and_falls_through_it` (5 nodes), `PDC::test_the_preference_summary_reports_each_slate_route_once` (5 nodes) |
| Planner gear table | `candidates._preferred_seed`: planner branch disabled | `PDC::test_a_planner_follows_its_gear_table_in_data_order` (3 nodes) |
| Restriction by harness | `model_restriction`: `allowed_harnesses` check disabled | `PDC::test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data[claude-sonnet-5-5]` |
| Restriction by size | `model_restriction`: `allowed_sizes` check disabled | `PDC::test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data[claude-opus-5-5]` |
| Floor exception ignores effort | `floor_exception`: effort comparison removed | `PDC::test_a_floor_exception_waives_only_its_exact_scope` (2 nodes) |
| Floor exception ignores size | `floor_exception`: size comparison removed | `PDC::test_a_floor_exception_waives_only_its_exact_scope` (2 nodes) |
| Floor exception accepts a published score | `floor_exception`: missing-score branch accepts a published score | `PDC::test_a_floor_exception_waives_only_its_exact_scope[argon-medium-l-intelligence-floor]` |
| Legacy gear inherits a size policy | `new_gear_size`: gear membership check disabled | `PDC::test_historical_gears_and_a_missing_gear_never_inherit_a_size_policy` (8 nodes) |
| Route summary mislabels the selected route | `route_summary`: `selected` status renamed | `PDC::test_the_preference_summary_reports_each_slate_route_once` (5 nodes) |
| Shipped role rule applies to another role | `effective_role_state`: role match removed | `PDC::test_size_scoped_shipped_rules_do_not_apply_when_the_size_is_unknown`, `PDC::test_a_shipped_role_rule_never_leaks_into_a_role_it_does_not_name`, `PDC::test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data[claude-opus-5-5]` |
| Size-scoped rule applies to an unknown size | `_in_scope`: unknown size treated as in scope | `PDC::test_size_scoped_shipped_rules_do_not_apply_when_the_size_is_unknown` |
| Size-scoped rule leaks to other sizes | `_in_scope`: always true | `PDC::test_a_size_scoped_shipped_rule_does_not_leak_to_other_sizes`, `PDC::test_every_shipped_role_rule_holds_at_its_own_scope`, `PDC::test_size_scoped_shipped_rules_do_not_apply_when_the_size_is_unknown` |
| Router ignores a shipped role deny | `routing.route`: role-deny branch disabled | `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope` |
| Discovery probe skips the size floor | `routing._pool_gate`: size floor check disabled | `PDC::test_a_probe_is_gated_by_the_size_floor_like_a_dispatch` |
| Catalog status ignores `dispatchable: false` | `route_policy.row_status`: always `available` | `PDC::test_every_shipped_route_is_either_dispatchable_or_a_documented_discovery_candidate` |
| Candidate build keeps shipped denies | `candidates.build_candidates`: role deny skip disabled | `PDC::test_built_executor_candidates_never_include_an_unscoped_shipped_deny` |
| A user deny does not beat the preferred route | `routing.route`: role-deny branch disabled | `PDC::test_a_user_deny_beats_the_preferred_route_and_emits_no_waiver_for_it`, `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope` |
| Slate route below its size floor | `catalog/executor-policy.yaml`: XS floor raised to 90 | `PDC::test_every_dispatchable_slate_route_clears_its_size_floor_or_holds_an_exception` |
| Sonnet hoist leaks to legacy gears | `adaptive`: `new_gear = True` | `tests/v31/test_issue809_run_gear.py::test_sonnet_claude_to_agy_self_fallback_applies_only_to_new_gear_runs` |
| Sonnet hoist removed | `adaptive`: the `rows.insert(pos + 1, alt)` reorder removed | `tests/v31/test_issue809_run_gear.py::test_sonnet_claude_to_agy_self_fallback_applies_only_to_new_gear_runs` |

## Open pull requests that edit the same test files

Listed by `gh pr list --repo Hikari9/auto-office --state open` and each open PR's files (`gh api repos/Hikari9/auto-office/pulls/<n>/files`) on 2026-10-31. Every open PR other than this one was checked against all 12 test files this change deletes, edits or adds. Only the row below overlaps. That run needs a rebase after this lands.

| PR | File |
|---|---|
| #951 | `tests/v31/test_issue809_regression.py` |

## Function-level traceability

Node counts are collected pytest nodes at base for that function.

| Old node id | Nodes | Disposition | Covering node ids, or why obsolete |
|---|---:|---|---|
| `tests/v31/test_issue809_executor_slates.py::test_pinned_benchmark_per_real_subtask_size` | 5 | deleted (module) | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value` (one node per size in the policy floors) |
| `tests/v31/test_issue809_executor_slates.py::test_terra_effort_thresholds_do_not_claim_qualification` | 6 | deleted (module) | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value`; `PDC::test_every_shipped_route_is_either_dispatchable_or_a_documented_discovery_candidate`; `tests/test_route_policy.py::test_status_derivations`. Obsolete part: the per-effort Terra score table is catalog data. |
| `tests/v31/test_issue809_executor_slates.py::test_sonnet_self_fallback_order_and_same_effort` | 1 | deleted (module) | `PDC::test_the_router_follows_the_preferred_slate_order_and_falls_through_it`; `tests/v31/test_issue809_real_routes.py::test_real_sonnet_harness_self_fallback_and_unavailable_first_route` (guards the adaptive hoist, retained); `tests/contracts/test_route_parity.py::test_effective_shipped_routes_match_main` |
| `tests/v31/test_issue809_executor_slates.py::test_l_argon_medium_remains_conditional_and_xl_high_distinct` | 1 | deleted (module) | `PDC::test_a_floor_exception_waives_only_its_exact_scope` [argon-medium-l-intelligence-floor]; `tests/contracts/test_route_parity.py::test_effective_shipped_routes_match_main` |
| `tests/v31/test_issue809_executor_slates.py::test_opus_is_xl_only_and_sonnet_harness_is_limited` | 1 | deleted (module) | `PDC::test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data` (one node per restriction in the policy) |
| `tests/v31/test_issue809_executor_slates.py::test_conditional_missing_and_unqualified_ideal_routes_are_audited` | 1 | deleted (module) | `PDC::test_the_preference_summary_reports_each_slate_route_once`; `tests/contracts/test_route_parity.py::test_conditional_m_routes_match_main_even_when_absent_from_catalog` |
| `tests/v31/test_issue809_executor_slates.py::test_old_pinned_gears_do_not_inherit_new_task_size_restrictions` | 1 | deleted (module) | `PDC::test_historical_gears_and_a_missing_gear_never_inherit_a_size_policy`; `PDC::test_run_gears_hand_their_task_size_to_the_size_policy` |
| `tests/v31/test_issue809_executor_slates.py::test_missing_gear_is_not_silently_upgraded_from_task_size` | 1 | deleted (module) | `PDC::test_historical_gears_and_a_missing_gear_never_inherit_a_size_policy` |
| `tests/v31/test_issue809_executor_slates.py::test_issue861_m_slate_and_exact_floor_exception` | 1 | deleted (module) | `tests/contracts/test_route_parity.py::test_effective_shipped_routes_match_main`; `tests/contracts/test_route_parity.py::test_m_executor_haiku_high_keeps_its_floor_exception`; `PDC::test_a_floor_exception_waives_only_its_exact_scope` [haiku-high-m-owner-authorized-floor] |
| `tests/v31/test_issue809_executor_slates.py::test_missing_score_exception_rejects_malformed_benchmark_maps` | 2 | deleted (module) | `PDC::test_a_floor_exception_waives_only_its_exact_scope` (malformed `benchmark_indexes` loop) |
| `tests/v31/test_haiku_5_5.py::test_haiku_5_5_rows_capture_all_effort_scores_and_long_context_pricing` | 1 | deleted (module) | Obsolete: pins catalog scores, prices and a release date; those are facts in `catalog/*.tsv`. Structure stays guarded by `tests/test_shipped_catalog_tables.py::test_shipped_identity_and_benchmark_index_are_complete` and `tests/test_shipped_catalog_tables.py::test_shipped_files_are_the_only_registry`. |
| `tests/v31/test_haiku_5_5.py::test_haiku_alias_tracks_5_5_and_medium_is_the_vendor_default_cold_start` | 1 | deleted (module) | `tests/test_alias_resolution.py::test_alias_is_offered_at_every_effort_of_the_newest_member`; `tests/test_alias_resolution.py::test_haiku_alias_preserves_concrete_effort_dispatch_gate`. Obsolete part: the alias target, documented source, score and price of the shipped Haiku row are catalog data and are no longer asserted. |
| `tests/v31/test_haiku_5_5.py::test_haiku_executor_floor_starts_at_medium_not_low` | 1 | deleted (module) | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value`; `PDC::test_every_dispatchable_slate_route_clears_its_size_floor_or_holds_an_exception`; `tests/v31/test_intelligence_floor.py::test_executor_and_reviewer_floors_use_intelligence`; `tests/v31/test_intelligence_floor.py::test_low_effort_high_intelligence_passes_executor_floor` |
| `tests/v31/test_haiku_5_5.py::test_benchmark_rows_do_not_claim_local_dispatch_conformance` | 1 | deleted (module) | `PDC::test_every_shipped_route_is_either_dispatchable_or_a_documented_discovery_candidate`; `tests/test_route_policy.py::test_status_derivations` |
| `tests/v31/test_argon_catalog_809.py::test_argon_catalog_rows_exact_and_not_yet_dispatchable` | 1 | deleted (module) | Obsolete: pins two catalog rows and their source URLs. Status and discovery rules: `PDC::test_every_shipped_route_is_either_dispatchable_or_a_documented_discovery_candidate`; `PDC::test_a_floor_exception_waives_only_its_exact_scope`; `tests/contracts/test_executor_policy.py::test_shipped_policy_is_resolved_against_catalog_facts` |
| `tests/v31/test_argon_catalog_809.py::test_argon_exception_remains_l_executor_only_and_high_is_xl` | 1 | deleted (module) | `PDC::test_a_floor_exception_waives_only_its_exact_scope`; `tests/contracts/test_route_parity.py::test_effective_shipped_routes_match_main` |
| `tests/v31/test_issue809_regression.py::test_size_floors_are_pinned_and_an_equal_score_passes` | 5 | deleted | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value` |
| `tests/v31/test_issue809_regression.py::test_each_gear_slate_leads_with_its_documented_route` | 5 | deleted | `PDC::test_the_router_follows_the_preferred_slate_order_and_falls_through_it`; `tests/contracts/test_route_parity.py::test_effective_shipped_routes_match_main` |
| `tests/v31/test_issue809_regression.py::test_sonnet_falls_back_claude_to_agy_at_the_same_effort_before_any_other_model` | 1 | deleted | `PDC::test_the_router_follows_the_preferred_slate_order_and_falls_through_it`; `PDC::test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data`; `tests/v31/test_issue809_real_routes.py::test_real_sonnet_harness_self_fallback_and_unavailable_first_route`; `tests/v31/test_issue809_run_gear.py::test_sonnet_claude_to_agy_self_fallback_applies_only_to_new_gear_runs` (owned by main `1e22deca`) |
| `tests/v31/test_issue809_regression.py::test_opus_is_reserved_for_xl_subtasks` | 4 | deleted | `PDC::test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data` |
| `tests/v31/test_issue809_regression.py::test_argon_medium_l_exception_is_exact_and_audited` | 1 | deleted | `PDC::test_a_floor_exception_waives_only_its_exact_scope` [argon-medium-l-intelligence-floor] |
| `tests/v31/test_issue809_regression.py::test_terra_effort_boundaries_pass_exactly_the_sizes_whose_floor_they_meet` | 6 | deleted | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value`. Obsolete part: the per-effort Terra score table. |
| `tests/v31/test_issue809_real_routes.py::test_real_routing_enforces_index_floor_on_subtask_not_run` | 9 | deleted | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value` |
| `tests/v31/test_issue809_real_routes.py::test_opus_xl_only_even_within_xl_parent_run_and_alias` | 1 | deleted | `PDC::test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data` (includes the alias path) |
| `tests/v31/test_issue809_real_routes.py::test_argon_medium_unpublished_score_passes_the_l_floor_by_explicit_exception` | 1 | deleted | `PDC::test_a_floor_exception_waives_only_its_exact_scope` [argon-medium-l-intelligence-floor] (selected, audited, no fabricated score) |
| `tests/v31/test_issue809_real_routes.py::test_argon_exception_is_exactly_model_effort_and_size_scoped` | 5 | deleted | `PDC::test_a_floor_exception_waives_only_its_exact_scope` |
| `tests/v31/test_issue809_real_routes.py::test_argon_exception_never_waives_a_published_below_floor_score_or_other_gates` | 1 | deleted | `PDC::test_a_floor_exception_waives_only_its_exact_scope`; `PDC::test_the_router_follows_the_preferred_slate_order_and_falls_through_it` (published below-floor score; an untried route alone is rejected) |
| `tests/v31/test_issue809_real_routes.py::test_discovery_never_trials_a_known_below_size_floor_or_non_xl_opus` | 1 | deleted | `PDC::test_a_probe_is_gated_by_the_size_floor_like_a_dispatch`; `PDC::test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data` |
| `tests/v31/test_issue809_real_routes.py::test_issue861_m_haiku_high_selected_with_audited_waiver` | 1 | deleted | `tests/contracts/test_route_parity.py::test_m_executor_haiku_high_keeps_its_floor_exception`; `PDC::test_a_floor_exception_waives_only_its_exact_scope` [haiku-high-m-owner-authorized-floor] |
| `tests/v31/test_issue809_real_routes.py::test_issue861_missing_or_unqualified_glm_f1_skips_to_terra_f2_without_fake_dispatch` | 1 | deleted | `PDC::test_the_router_follows_the_preferred_slate_order_and_falls_through_it`; `PDC::test_the_preference_summary_reports_each_slate_route_once`; `tests/contracts/test_route_parity.py::test_conditional_m_routes_match_main_even_when_absent_from_catalog` |
| `tests/v31/test_issue809_real_routes.py::test_issue861_m_haiku_probe_gate_uses_exact_authorized_waiver_only` | 1 | deleted | `PDC::test_a_floor_exception_waives_only_its_exact_scope` [haiku-high-m-owner-authorized-floor] (includes the probe gate) |
| `tests/v31/test_issue809_real_routes.py::test_issue861_policy_deny_precedes_preference_and_is_role_and_size_scoped` | 1 | deleted | `tests/contracts/core/test_policy_precedence.py::test_executor_deny_does_not_deny_independent_reviewer`; `::test_deny_is_size_scoped`; `::test_exact_allow_overrides_a_wildcard_deny`; `tests/contracts/core/test_safety_invariants.py::test_user_deny_remains_effective_after_reordering_rules`; `tests/v31/test_role_seed_routing.py::test_user_allows_reports_which_sizes_a_scoped_allow_covers`; `PDC::test_a_user_deny_beats_the_preferred_route_and_emits_no_waiver_for_it` (user deny beats the preferred slate route, falls to the next, no waiver for the denied route) |
| `tests/v31/test_issue846_size_seeds.py::test_each_size_selects_its_slate_primary_when_allowed` | 5 | deleted | `PDC::test_the_router_follows_the_preferred_slate_order_and_falls_through_it` |
| `tests/v31/test_issue846_size_seeds.py::test_each_size_falls_to_the_next_slate_route_when_the_primary_cannot_run` | 5 | deleted | `PDC::test_the_router_follows_the_preferred_slate_order_and_falls_through_it` |
| `tests/v31/test_issue846_size_seeds.py::test_an_explicit_planner_follows_the_run_gear_table` | 3 | deleted | `PDC::test_a_planner_follows_its_gear_table_in_data_order`; `tests/contracts/test_route_parity.py::test_effective_shipped_routes_match_main` (planner rows) |
| `tests/v31/test_issue846_size_seeds.py::test_xs_s_and_legacy_gears_have_no_gear_planner_table` | 4 | deleted | `PDC::test_a_gear_without_a_planner_table_keeps_the_flat_seed` |
| `tests/v31/test_issue846_size_seeds.py::test_no_new_gear_queues_a_dedicated_planner_by_default` | 1 | deleted | Duplicate of `tests/v31/test_issue809_regression.py::test_every_gear_row_matches_its_funded_gates` and `tests/v31/test_issue809_run_gear.py::test_new_gear_funding_is_independent_of_low_safety_risk`, which assert `dedicated_planner` for every gear |
| `tests/v31/test_role_seed_routing.py::test_executor_matrix_is_size_scoped` | 45 | deleted | `PDC::test_every_shipped_role_rule_holds_at_its_own_scope`; `PDC::test_a_size_scoped_shipped_rule_does_not_leak_to_other_sizes`; `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope` The shipped rule data itself is no longer pinned: removing or editing a deny is a policy edit, not a test edit. |
| `tests/v31/test_role_seed_routing.py::test_astra_and_5_6_are_never_executors_at_any_size` | 72 | deleted | `PDC::test_every_shipped_role_rule_holds_at_its_own_scope`; `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope` The shipped rule data itself is no longer pinned: removing or editing a deny is a policy edit, not a test edit. |
| `tests/v31/test_role_seed_routing.py::test_astra_denial_does_not_spill_into_planning` | 1 | deleted | `PDC::test_a_shipped_role_rule_never_leaks_into_a_role_it_does_not_name` |
| `tests/v31/test_role_seed_routing.py::test_size_scoped_rules_do_not_apply_to_an_unknown_size` | 1 | deleted | `PDC::test_size_scoped_shipped_rules_do_not_apply_when_the_size_is_unknown`; `PDC::test_every_shipped_role_rule_holds_at_its_own_scope` |
| `tests/v31/test_role_seed_routing.py::test_sonnet_high_is_denied_for_s_and_only_a_users_size_scoped_rule_re_allows_it` | 1 | deleted | `tests/contracts/core/test_policy_precedence.py::test_exact_allow_overrides_a_wildcard_deny`; `tests/v31/test_role_seed_routing.py::test_user_rules_outrank_shipped_and_the_scope_precedence_holds` (retained) |
| `tests/v31/test_role_seed_routing.py::test_reviewers_section_2` | 2 | deleted | `PDC::test_every_shipped_role_rule_holds_at_its_own_scope`; `PDC::test_a_shipped_role_rule_never_leaks_into_a_role_it_does_not_name`; `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope`; `tests/contracts/core/test_policy_precedence.py::test_deny_is_effort_exact` The shipped rule data itself is no longer pinned: removing or editing a deny is a policy edit, not a test edit. |
| `tests/v31/test_role_seed_routing.py::test_visual_reviewer_section_3` | 1 | deleted | `PDC::test_every_shipped_role_rule_holds_at_its_own_scope`; `PDC::test_a_shipped_role_rule_never_leaks_into_a_role_it_does_not_name`; `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope` The shipped rule data itself is no longer pinned: removing or editing a deny is a policy edit, not a test edit. |
| `tests/v31/test_role_seed_routing.py::test_the_5_6_executor_deny_is_not_a_cross_role_ban` | 1 | deleted | `PDC::test_a_shipped_role_rule_never_leaks_into_a_role_it_does_not_name` The shipped rule data itself is no longer pinned: removing or editing a deny is a policy edit, not a test edit. |
| `tests/v31/test_role_seed_routing.py::test_default_config_reviewer_and_visual_seed_order` | 1 | deleted | Obsolete: pins the order and every entry of user-owned preference values in `config.default.yaml`. The lead of every role seed (including the visual reviewer) must still resolve: `PDC::test_the_lead_of_every_default_preferred_seed_resolves_to_a_shipped_route`; also `tests/test_alias_resolution.py::test_default_reviewer_seeds_resolve_to_catalog_candidates`. Order-follows-data: `tests/contracts/core/test_runtime_selection.py::test_preference_mutation_changes_the_runtime_winner_without_test_edits` |
| `tests/v31/test_role_seed_routing.py::test_routing_rejects_a_shipped_executor_deny_at_every_size` | 3 | deleted | `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope` |
| `tests/v31/test_role_seed_routing.py::test_routing_applies_the_size_scope_of_a_shipped_deny` | 1 | deleted | `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope`; `PDC::test_a_size_scoped_shipped_rule_does_not_leak_to_other_sizes` |
| `tests/v31/test_role_seed_routing.py::test_routing_rejects_a_shipped_reviewer_deny` | 1 | deleted | `PDC::test_the_router_rejects_every_shipped_deny_in_its_role_and_scope` |
| `tests/v31/test_role_seed_routing.py::test_candidate_building_excludes_shipped_denied_executor_routes` | 1 | deleted | `PDC::test_built_executor_candidates_never_include_an_unscoped_shipped_deny` |
| `tests/v31/test_sonnet_5_5.py::test_sonnet_5_5_high_row_is_dispatchable_with_a_sourced_score` | 1 | deleted | Obsolete: pins one catalog row's score and source URL. Status rule: `PDC::test_every_shipped_route_is_either_dispatchable_or_a_documented_discovery_candidate`; structure: `tests/test_shipped_catalog_tables.py::test_shipped_identity_and_benchmark_index_are_complete` |
| `tests/v31/test_sonnet_5_5.py::test_sonnet_5_5_high_clears_the_executor_floor` | 1 | deleted | `PDC::test_a_size_floor_is_enforced_exactly_at_its_value`; `PDC::test_every_dispatchable_slate_route_clears_its_size_floor_or_holds_an_exception`; `tests/v31/test_intelligence_floor.py::test_executor_and_reviewer_floors_use_intelligence` |
| `tests/v31/test_sonnet_5_5.py::test_sonnet_alias_high_now_invokes_sonnet_5_5` | 1 | deleted | `tests/test_alias_resolution.py::test_alias_is_offered_at_every_effort_of_the_newest_member` (generic alias resolution). Obsolete part: the shipped Sonnet alias's invocation target and score are catalog data and are no longer asserted. |
| `tests/v31/test_issue811_terra_supersession.py::test_six_terra_rows_are_present_and_only_probed_efforts_dispatch` | 1 | deleted | Obsolete: pins six catalog rows and their scores. Eligible-but-undispatchable rule: `PDC::test_every_shipped_route_is_either_dispatchable_or_a_documented_discovery_candidate` |
| `tests/test_route_policy.py::test_shipped_discovery_catalog_exactly_matches_locked_choices` | 1 | deleted | Obsolete: the literal set of discovery-eligible routes. Its two generic loops moved to `PDC::test_every_shipped_route_is_either_dispatchable_or_a_documented_discovery_candidate` |

## Module inventory

Every test module under `tests/` except `tests/contracts/`, at the base commit. `keep` means no change; the reason column is filled for every other disposition. `historical-diagnostic` is a classification only: nothing was moved, marked or skipped.

| Module | Disposition | Reason |
|---|---|---|
| `tests/test_adapters.py` | keep |  |
| `tests/test_agy_usage.py` | keep |  |
| `tests/test_alias_resolution.py` | keep |  |
| `tests/test_amend_checks_only.py` | keep |  |
| `tests/test_amend_contract_edits.py` | keep |  |
| `tests/test_amend_requirements_delivery.py` | keep |  |
| `tests/test_amendments.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_budget.py` | keep |  |
| `tests/test_catalog_archival.py` | keep |  |
| `tests/test_check_ecosystem.py` | keep |  |
| `tests/test_checks_stale.py` | keep |  |
| `tests/test_db_connect.py` | keep |  |
| `tests/test_derived_routing.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_dogfood.py` | keep |  |
| `tests/test_families.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_hooks.py` | keep |  |
| `tests/test_instruction_conformance.py` | keep |  |
| `tests/test_integration.py` | keep |  |
| `tests/test_landings.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_monitor.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_packet_delivery_channel.py` | keep |  |
| `tests/test_pane_busy.py` | keep |  |
| `tests/test_plugin_skill_entry.py` | keep |  |
| `tests/test_pr_report.py` | keep |  |
| `tests/test_pr_test_selection.py` | keep |  |
| `tests/test_pre_push_hook.py` | keep |  |
| `tests/test_propose.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_release_identity.py` | keep |  |
| `tests/test_reuse_compact.py` | keep |  |
| `tests/test_review.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_route_policy.py` | consolidate | 1 node pinned the exact set of discovery-eligible shipped routes; the rest stay |
| `tests/test_route_probe.py` | keep |  |
| `tests/test_runtime.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_schemas.py` | keep |  |
| `tests/test_scoring.py` | historical-diagnostic | exercises the retained 3.0 helper surface (`scripts/office_*`) that only pinned 3.0 runs use; kept for on-demand pinned-run diagnosis |
| `tests/test_session_end_hook.py` | keep |  |
| `tests/test_shipped_catalog_tables.py` | keep |  |
| `tests/test_spoke_receipt_integrity.py` | keep |  |
| `tests/test_trust_alias_625.py` | keep |  |
| `tests/test_trust_conformance.py` | keep |  |
| `tests/test_v3_instruction_contract.py` | keep |  |
| `tests/test_version_serves.py` | keep |  |
| `tests/test_version_single_source.py` | keep |  |
| `tests/test_worktree.py` | keep |  |
| `tests/v31/test_adaptive_dispatch.py` | keep |  |
| `tests/v31/test_adaptive_routing.py` | keep |  |
| `tests/v31/test_agent_questions.py` | keep |  |
| `tests/v31/test_agy_catalog_626.py` | keep |  |
| `tests/v31/test_agy_executor.py` | keep |  |
| `tests/v31/test_amend_accepted.py` | keep |  |
| `tests/v31/test_amend_additive.py` | keep |  |
| `tests/v31/test_argon_catalog_809.py` | retire | pinned Argon catalog rows and the Argon slate tail; replaced by the data-driven exception and catalog-status contracts |
| `tests/v31/test_benchmark_refresh.py` | keep |  |
| `tests/v31/test_bug_hint.py` | keep |  |
| `tests/v31/test_cap_diff.py` | keep |  |
| `tests/v31/test_cli_override_harness.py` | keep |  |
| `tests/v31/test_cli_runtimes.py` | keep |  |
| `tests/v31/test_cli_usage_hints.py` | keep |  |
| `tests/v31/test_close_external.py` | keep |  |
| `tests/v31/test_closeout.py` | keep |  |
| `tests/v31/test_codex_pane_session.py` | keep |  |
| `tests/v31/test_codex_startup.py` | keep |  |
| `tests/v31/test_commands_ledger.py` | keep |  |
| `tests/v31/test_commit_identity.py` | keep |  |
| `tests/v31/test_compat_hooks_install.py` | keep |  |
| `tests/v31/test_composer_held.py` | keep |  |
| `tests/v31/test_config_command.py` | keep |  |
| `tests/v31/test_config_sandbox.py` | keep |  |
| `tests/v31/test_conformance_vision_routing.py` | keep |  |
| `tests/v31/test_context_snapshot.py` | keep |  |
| `tests/v31/test_convergence_cap_waiver.py` | keep |  |
| `tests/v31/test_convergence_contract.py` | keep |  |
| `tests/v31/test_convergence_reviewed_commit.py` | keep |  |
| `tests/v31/test_ctx_alert.py` | keep |  |
| `tests/v31/test_declared_route_426.py` | keep |  |
| `tests/v31/test_defect_criteria.py` | keep |  |
| `tests/v31/test_defect_redirect.py` | keep |  |
| `tests/v31/test_delegation_shadow.py` | keep |  |
| `tests/v31/test_dependency_readiness.py` | keep |  |
| `tests/v31/test_deploy_env_files.py` | keep |  |
| `tests/v31/test_discovery_dispatch.py` | keep |  |
| `tests/v31/test_discovery_pinning.py` | keep |  |
| `tests/v31/test_dispatch_fence_361.py` | keep |  |
| `tests/v31/test_dispatch_override.py` | keep |  |
| `tests/v31/test_doctor_repairs.py` | keep |  |
| `tests/v31/test_dormant_lease.py` | keep |  |
| `tests/v31/test_e2e_flow.py` | keep |  |
| `tests/v31/test_economics.py` | keep |  |
| `tests/v31/test_economics_efficiency.py` | keep |  |
| `tests/v31/test_executor_binding_config_drift.py` | keep |  |
| `tests/v31/test_external_visual.py` | keep |  |
| `tests/v31/test_finding_identity.py` | keep |  |
| `tests/v31/test_fixture_source_path.py` | keep |  |
| `tests/v31/test_gates.py` | keep |  |
| `tests/v31/test_haiku_5_5.py` | retire | pinned Haiku catalog scores, prices and dates, which are catalog facts owned by the TSV files; structure and gates covered by retained catalog, alias and floor tests |
| `tests/v31/test_harness_model_cli.py` | keep |  |
| `tests/v31/test_harness_version_memo.py` | keep |  |
| `tests/v31/test_headless_agent_env.py` | keep |  |
| `tests/v31/test_headless_fallback.py` | keep |  |
| `tests/v31/test_herdr_agent_launch.py` | keep |  |
| `tests/v31/test_herdr_landing.py` | keep |  |
| `tests/v31/test_herdr_panes.py` | keep |  |
| `tests/v31/test_herdr_review_capture.py` | keep |  |
| `tests/v31/test_hook_ledger.py` | keep |  |
| `tests/v31/test_hostmetrics.py` | keep |  |
| `tests/v31/test_idle_final_status.py` | keep |  |
| `tests/v31/test_inprocess_launch.py` | keep |  |
| `tests/v31/test_inspect_route_discovery.py` | keep |  |
| `tests/v31/test_install_drift.py` | keep |  |
| `tests/v31/test_integrated_review.py` | keep |  |
| `tests/v31/test_intelligence_floor.py` | keep |  |
| `tests/v31/test_issue502_strategy_efficiency.py` | keep |  |
| `tests/v31/test_issue809_executor_slates.py` | retire | every node pinned shipped floors, slates, restrictions or exceptions; replaced by data-driven tests in `PDC` |
| `tests/v31/test_issue809_inline_dispatch.py` | keep |  |
| `tests/v31/test_issue809_pane_size_label.py` | keep |  |
| `tests/v31/test_issue809_real_routes.py` | consolidate | 10 of 18 nodes pinned floors, restrictions and exceptions for named models; the receipt, discovery, legacy-pinned and self-fallback nodes stay |
| `tests/v31/test_issue809_regression.py` | consolidate | 6 of 16 nodes pinned floors, slate heads and exceptions; gear funding, subagent funding, inline ownership and legacy resume stay |
| `tests/v31/test_issue809_rollout_metrics.py` | keep |  |
| `tests/v31/test_issue809_run_gear.py` | keep | unchanged by this change; main `1e22deca` owns the fix to `test_sonnet_claude_to_agy_self_fallback_applies_only_to_new_gear_runs` |
| `tests/v31/test_issue809_self_review_subagents.py` | keep |  |
| `tests/v31/test_issue809_xl_review.py` | keep |  |
| `tests/v31/test_issue811_terra_supersession.py` | consolidate | 1 of 6 nodes pinned the six Terra catalog rows; the supersession nodes stay |
| `tests/v31/test_issue846_size_seeds.py` | consolidate | 5 of 16 nodes duplicated slate order or the gear tables; slate anchoring, reroute and seed precedence stay |
| `tests/v31/test_issue_267.py` | keep |  |
| `tests/v31/test_issue_268.py` | keep |  |
| `tests/v31/test_issue_399_dispatch_recovery.py` | keep |  |
| `tests/v31/test_issue_494_scenarios.py` | keep |  |
| `tests/v31/test_issue_530_stale_waiver.py` | keep |  |
| `tests/v31/test_issue_537_integrity.py` | keep |  |
| `tests/v31/test_issue_537_shared_append.py` | keep |  |
| `tests/v31/test_issue_538_safe_pr_sync.py` | keep |  |
| `tests/v31/test_issue_541_upgrade.py` | keep |  |
| `tests/v31/test_issue_563_artifact_integrity.py` | keep |  |
| `tests/v31/test_issue_571_shell_safety.py` | keep |  |
| `tests/v31/test_issue_573_visual_fallback.py` | keep |  |
| `tests/v31/test_issue_580_rebase_bases.py` | keep |  |
| `tests/v31/test_issue_629_integrity.py` | keep |  |
| `tests/v31/test_issue_644_wrapped_requirements.py` | keep |  |
| `tests/v31/test_issue_651_playwright_doctor.py` | keep |  |
| `tests/v31/test_issue_653_markdown_prose.py` | keep |  |
| `tests/v31/test_issue_654_already_merged_land.py` | keep |  |
| `tests/v31/test_issue_655_identical_new_file.py` | keep |  |
| `tests/v31/test_issue_659_pr_title_slug.py` | keep |  |
| `tests/v31/test_issue_662_manual_deploy.py` | keep |  |
| `tests/v31/test_issue_664_native_select_steps.py` | keep |  |
| `tests/v31/test_issue_668_prose_git_guard.py` | keep |  |
| `tests/v31/test_issue_669_visual_readiness.py` | keep |  |
| `tests/v31/test_issue_678_unsafe_run_base.py` | keep |  |
| `tests/v31/test_issue_696_amend_restack.py` | keep |  |
| `tests/v31/test_issue_700_shared_dependency.py` | keep |  |
| `tests/v31/test_issue_703_effective_checks.py` | keep |  |
| `tests/v31/test_issue_704_repository_root.py` | keep |  |
| `tests/v31/test_issue_705_squash_close.py` | keep |  |
| `tests/v31/test_issue_707_route_authority.py` | keep |  |
| `tests/v31/test_issue_709_invocation_guidance.py` | keep |  |
| `tests/v31/test_issue_712_deploy_artifacts.py` | keep |  |
| `tests/v31/test_issue_714_topological_restack.py` | keep |  |
| `tests/v31/test_issue_721_accepted_route.py` | keep |  |
| `tests/v31/test_issue_734_fetched_base.py` | keep |  |
| `tests/v31/test_issue_740_recovery.py` | keep |  |
| `tests/v31/test_issue_740_review_correctness.py` | keep |  |
| `tests/v31/test_issue_783_cleanup.py` | keep |  |
| `tests/v31/test_issue_783_herdr_smoke.py` | keep |  |
| `tests/v31/test_issue_790_codex_pane_submit.py` | keep |  |
| `tests/v31/test_issue_790_held_pointer.py` | keep |  |
| `tests/v31/test_issue_790_review_route.py` | keep |  |
| `tests/v31/test_issue_817_effort_ceiling.py` | keep |  |
| `tests/v31/test_issue_827_opencode_reviewer_crash.py` | keep |  |
| `tests/v31/test_issue_836_close_cancels_jobs.py` | keep |  |
| `tests/v31/test_issue_845_invocation_table.py` | keep |  |
| `tests/v31/test_issue_845_user_allow.py` | keep |  |
| `tests/v31/test_issue_856_stale_accepted_restack.py` | keep |  |
| `tests/v31/test_issue_860_quota_pool_reroute.py` | keep |  |
| `tests/v31/test_job_ownership.py` | keep |  |
| `tests/v31/test_kilo_registration.py` | keep |  |
| `tests/v31/test_land.py` | keep |  |
| `tests/v31/test_land_recovery.py` | keep |  |
| `tests/v31/test_landing_gaps.py` | keep |  |
| `tests/v31/test_lane_gate_revoke.py` | keep |  |
| `tests/v31/test_launch_argv_evidence.py` | keep |  |
| `tests/v31/test_lineage_supersession_811.py` | keep |  |
| `tests/v31/test_live_delivery_stall.py` | keep |  |
| `tests/v31/test_model_floor.py` | keep |  |
| `tests/v31/test_model_lineages_610.py` | keep |  |
| `tests/v31/test_multi_dependency_base.py` | keep |  |
| `tests/v31/test_new_runs_default.py` | keep |  |
| `tests/v31/test_notice_priority.py` | keep |  |
| `tests/v31/test_office_prompt.py` | keep |  |
| `tests/v31/test_onboarding.py` | keep |  |
| `tests/v31/test_opencode_registration.py` | keep |  |
| `tests/v31/test_opencode_reviewer.py` | keep |  |
| `tests/v31/test_opencode_worker_pane.py` | keep |  |
| `tests/v31/test_orchestrator_fallback_and_reconcile.py` | keep |  |
| `tests/v31/test_orphaned_dispatch.py` | keep |  |
| `tests/v31/test_outcome_labels.py` | keep |  |
| `tests/v31/test_pane_reclaim.py` | keep |  |
| `tests/v31/test_pi_registration.py` | keep |  |
| `tests/v31/test_plan_diagram.py` | keep |  |
| `tests/v31/test_plan_path.py` | keep |  |
| `tests/v31/test_plan_review_lifecycle.py` | keep |  |
| `tests/v31/test_planner_amend_serial.py` | keep |  |
| `tests/v31/test_preexisting_checks.py` | keep |  |
| `tests/v31/test_preflight.py` | keep |  |
| `tests/v31/test_prompt_landed_baseline.py` | keep |  |
| `tests/v31/test_prune.py` | keep |  |
| `tests/v31/test_quota_probe.py` | keep |  |
| `tests/v31/test_quota_routing.py` | keep |  |
| `tests/v31/test_raise.py` | keep |  |
| `tests/v31/test_refused_submit_scope.py` | keep |  |
| `tests/v31/test_request_scope_paths.py` | keep |  |
| `tests/v31/test_rerun.py` | keep |  |
| `tests/v31/test_retired_mimo_catalog.py` | keep |  |
| `tests/v31/test_review_reroute.py` | keep |  |
| `tests/v31/test_review_stopping_rule.py` | keep |  |
| `tests/v31/test_reviewer_files.py` | keep |  |
| `tests/v31/test_reviewer_read_scope.py` | keep |  |
| `tests/v31/test_revoke_accepted.py` | keep |  |
| `tests/v31/test_revoke_stops.py` | keep |  |
| `tests/v31/test_risk_floor_lightweight.py` | keep |  |
| `tests/v31/test_role_override_837.py` | keep |  |
| `tests/v31/test_role_override_dispatch_837.py` | keep |  |
| `tests/v31/test_role_policy_837.py` | keep |  |
| `tests/v31/test_role_policy_cli_837.py` | keep |  |
| `tests/v31/test_role_seed_routing.py` | consolidate | 13 of 22 nodes re-listed the shipped allow/deny matrix per model; user-rule, CLI and scoped-allow nodes stay |
| `tests/v31/test_rolling_review.py` | keep |  |
| `tests/v31/test_root_wake_723.py` | keep |  |
| `tests/v31/test_round_cap_stop.py` | keep |  |
| `tests/v31/test_route_change.py` | keep |  |
| `tests/v31/test_route_discovery_consumer_contract.py` | keep |  |
| `tests/v31/test_route_discovery_routing.py` | keep |  |
| `tests/v31/test_route_learning_discovery.py` | keep |  |
| `tests/v31/test_route_policy_config.py` | keep |  |
| `tests/v31/test_route_probe_live_shape.py` | keep |  |
| `tests/v31/test_routing_evidence_convergence.py` | keep |  |
| `tests/v31/test_routing_evidence_dispatch.py` | keep |  |
| `tests/v31/test_routing_evidence_report.py` | keep |  |
| `tests/v31/test_routing_evidence_tags.py` | keep |  |
| `tests/v31/test_routing_properties.py` | keep |  |
| `tests/v31/test_run_checks.py` | keep |  |
| `tests/v31/test_run_routing_optin.py` | keep |  |
| `tests/v31/test_scheduler.py` | keep |  |
| `tests/v31/test_schema_migration.py` | keep |  |
| `tests/v31/test_schema_v6_migration.py` | keep |  |
| `tests/v31/test_scope_attribution.py` | keep |  |
| `tests/v31/test_scope_none.py` | keep |  |
| `tests/v31/test_scope_none_evidence.py` | keep |  |
| `tests/v31/test_scoring_properties.py` | keep |  |
| `tests/v31/test_self_improve.py` | keep |  |
| `tests/v31/test_self_review_ledger.py` | keep |  |
| `tests/v31/test_self_review_receipt.py` | keep |  |
| `tests/v31/test_session_capture.py` | keep |  |
| `tests/v31/test_sonnet_5_5.py` | consolidate | 3 of 4 nodes pinned Sonnet catalog rows; the trust-baseline node stays |
| `tests/v31/test_stacked_restack.py` | keep |  |
| `tests/v31/test_start_trust.py` | keep |  |
| `tests/v31/test_startup_prompts.py` | keep |  |
| `tests/v31/test_startup_trust_short_pane.py` | keep |  |
| `tests/v31/test_state_core.py` | keep |  |
| `tests/v31/test_task_aware_routing.py` | keep |  |
| `tests/v31/test_task_prs.py` | keep |  |
| `tests/v31/test_task_prs_stacking.py` | keep |  |
| `tests/v31/test_trust_major.py` | keep |  |
| `tests/v31/test_upstream_reaccept.py` | keep |  |
| `tests/v31/test_verification_coverage.py` | keep |  |
| `tests/v31/test_visual.py` | keep |  |
| `tests/v31/test_visual_port_conflict.py` | keep |  |
| `tests/v31/test_visual_preflight.py` | keep |  |
| `tests/v31/test_wait.py` | keep |  |
| `tests/v31/test_worktree_setup.py` | keep |  |
| `tests/web/browser/test_agents.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_allocation.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_chat.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_e2e.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_issues.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_layout.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_model_registry_610.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_routing.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_settings.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_shell.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_smoke.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/browser/test_workbench.py` | historical-diagnostic | drives a real browser through Playwright and skips without it; kept for on-demand UI diagnosis |
| `tests/web/test_activity.py` | keep |  |
| `tests/web/test_api_commands.py` | keep |  |
| `tests/web/test_capabilities.py` | keep |  |
| `tests/web/test_chat.py` | keep |  |
| `tests/web/test_github.py` | keep |  |
| `tests/web/test_identity.py` | keep |  |
| `tests/web/test_land_via_office.py` | keep |  |
| `tests/web/test_launcher.py` | keep |  |
| `tests/web/test_model_registry_610.py` | keep |  |
| `tests/web/test_observer.py` | keep |  |
| `tests/web/test_perf.py` | keep |  |
| `tests/web/test_projection.py` | keep |  |
| `tests/web/test_readback.py` | keep |  |
| `tests/web/test_reconnect.py` | keep |  |
| `tests/web/test_repos.py` | keep |  |
| `tests/web/test_server.py` | keep |  |
| `tests/web/test_settings_api.py` | keep |  |
| `tests/web/test_target_validation.py` | keep |  |

Totals: {'keep': 266, 'historical-diagnostic': 21, 'consolidate': 8, 'retire': 3}.

## Left alone on purpose

- Modules and nodes named in `SAFETY_SMOKE` or `SPECIAL` in `scripts/select_pr_tests.py`, the nodes cited in `docs/contract-suite.md`, and everything in `tests/contracts/`.
- `tests/test_derived_routing.py` is synthetic (`'m'`, `'fallback'`) and tests the retained 3.0 runtime, so it is classified historical-diagnostic, not retired.
- `tests/v31/test_issue809_real_routes.py::test_real_sonnet_harness_self_fallback_and_unavailable_first_route`: `adaptive.py` hard-codes the Sonnet claude-to-agy hoist, so this is its only guard.
- `tests/v31/test_issue809_real_routes.py::test_issue861_unconfirmed_haiku_uses_real_terra_then_sonnet_fallback`: it is the only test of the `Temporary Sonnet Medium M fallback` disclosure.
- `tests/test_alias_resolution.py` and `tests/v31/test_intelligence_floor.py` still pin some shipped model names. They are `SPECIAL` modules for `catalog/` and `config/` changes, so they stay.
