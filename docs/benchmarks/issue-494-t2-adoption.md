# Issue 494, T2 adoption record

Plan p11, requirements r4, task T2 (core: exact conformance probe, fingerprint cache, candidate qualification, trial eligibility, discovery allocation).
Base is T1 at `341b48b`. This record maps every T2 accept item to code and tests in the composed tree, names each gap T2 fixed, lists the adopted commits with their composed SHAs, and records the failing-before and passing-after output of the B1, B2 and C3 regressions.

## 1. Adopted `issue-494/core` commits (D8)

Each adopted commit was cherry-picked with authorship and author date kept. `git patch-id --stable` of every original equals the patch-id of its composed commit, so the content is unchanged.

| issue-494/core | composed | subject |
| --- | --- | --- |
| `4f7314f` | `c0b1cf5` | feat(routing): exact route conformance probe and fingerprint cache (T2) |
| `e0e2066` | `fcce455` | feat(routing): candidate qualification, trial eligibility and discovery allocation (T3) |
| `f2a1ce5` | `13b675e` | fix(routing): fail closed on unreadable policy config, find repo-tier denials, keep deduped aliases listed |
| `b2aac9c` | `14bdb0f` | fix(routing): read denial policy outside the start config snapshot |
| `d7ab931` | `25656a7` | fix(routing): preserve unknown invocation status without admitting routes |
| `60323f3` (`60323f3276df6f7948f4abb40ecff495a884f6ac`) | `407e8e2` | fix(routing): stop treating unconfirmed non-eligible rows as probe-eligible or confirmed unsupported |

Commits added on top inside T2 scope:

| composed | fixes |
| --- | --- |
| `e1e3f82` | core-review C1, C2, C3 |
| `ba7707f` | A9 B1, B2 |
| the commit that adds this record | F1 (missing adoption evidence) and the self-review fixes in section 2a |

## 2. Gaps T2 fixed

| id | gap | fix |
| --- | --- | --- |
| F1 foundation | T1's `route_policy.row_status` mapped a `dispatchable: false` row without explicit eligibility to `confirmed-unsupported`, and unknown invocation status could be treated as admitted or probe-eligible (D7). | `25656a7` and `407e8e2` plus the D7 status correction in `src/office/route_policy.py:40-47` (`discovered-unconfirmed`, reason "catalog invocation unconfirmed"). Such rows stay excluded with category `not-eligible`, are refused by the probe as `not-eligible`, and are never labelled unsupported. |
| C1 | A model reply could be read as identity evidence. | Model and effort pass only from the harness's own fenced header, first block of the stream. Harnesses without a metadata readback are not probed. |
| C2 | A probe could write outside its disposable workspace, and descendants could escape cleanup. | Probes run under `sandbox-exec` limited to the workspace. Platforms or profiles with no boundary are refused. Descendants are attributed by env tag, remembered lineage and workspace handles, and terminated individually. |
| C3 | `db.connect` raised `database is locked` on a concurrent first-open WAL switch (D10). | `src/office/db.py:176` `_enter_wal` retries a bounded number of times with jittered backoff, then raises the existing `RuntimeError`. `reserve` answers lock contention with a refusal. |
| B1 | The probe read denial from the run's pinned config, so a denial added after `office start` did not stop a probe (D11). | `route_probe` reads denial through `candidates.current_user_policies` (the live path `route_role` uses). The refusal precedes any reservation row, cap use or launch and appends `probe-refused`. An unreadable live policy refuses. |
| B2 | `dispatch.planned_route` applied overkill to a recorded declared route, so `office amend route --as` followed by `office dispatch` was blocked (D11). | Declared or manual routes are re-qualified as manual. Overkill does not remove them. Denial, quarantine, capability, permission, archive and quota gates still apply. Automatic primaries and fallbacks keep overkill skipping. |

### 2a. Fixes from the F1 round's self-review

| id | gap | fix | test |
| --- | --- | --- | --- |
| R1 | `ps -axeww` does not show process environments on macOS, so the probe's nonce tag never attributed a `setsid` descendant that had left the workspace. | `route_probe._PS_ENV_FLAGS` is `-axEww` on darwin and `axeww` elsewhere. | RP `a_setsid_escapee_with_no_workspace_handle_is_attributed_by_the_real_ps_environment` (real `ps`, real escapee) |
| R2 | `classify_text` matched `model ... unavailable` as terminal `unsupported-model-effort`, so an overload or "temporarily unavailable" message would block the fingerprint permanently. | The bare `unavailable` alternative is dropped from the unsupported pattern. | RP `a_capacity_message_that_mentions_the_model_is_transient_never_terminal` |
| R3 | `route_role` and `declared_candidate` read the live policy through `current_user_policies`, which returns `[]` for an unreadable config: a denial in a corrupt user or repo file was ignored and discovery stayed on. | `candidates.required_user_policies` refuses with `Refused("policy-unreadable")`. `route_role` and `declared_candidate` use it. `build_candidates` keeps the lenient reader because routing passes it `[]` and decides denial itself. | RD `route_role_honors_a_live_denial_and_fails_closed_on_an_unreadable_policy` |
| R4 | A malformed `routing.*` value (for example `denied_models` as a string) was dropped by the config merge with only a warning, so the live policy read it as "no denials". | `live_user_policies` raises `ValueError` on a `type-mismatch-ignored` warning for `routing` or `routing.user_policy*` (the subtree a denial lives in). A mistyped `routing.adaptive.*` value stays a warning. Every caller above then fails closed. | RD `route_role_honors_a_live_denial_and_fails_closed_on_an_unreadable_policy` (string and `routing: 5` cases), `a_mistyped_value_outside_the_denial_subtree_does_not_hide_or_refuse_a_policy` |
| R5 | When the WAL switch stayed locked for every retry, `_enter_wal` re-raised `sqlite3.OperationalError`, not the existing `RuntimeError` the contract names. | The last lock failure raises `RuntimeError("runs.db could not enter WAL mode ...")` chained from the lock error. | DB `a_database_that_stays_locked_raises_after_a_bounded_number_of_tries`, `a_database_that_will_not_switch_to_wal_raises_runtime_error` |
| R6 | The concurrent first-open test checked only the journal mode. | It now also checks the schema version, no drift and the `route_discovery_events` triggers after the race. | DB `concurrent_first_opens_of_one_database_all_succeed_in_wal_mode` |
| R7 | "Byte-identical to HEAD" was compared against the new code, not against the pre-change commit. | `routing.route` was run at `341b48b` and at this tree on four requests with no discovery input. `decision_hash` and a digest of the whole result are equal, and are pinned as golden values. | RD `routing_output_is_byte_identical_to_the_pre_change_commit_when_discovery_is_not_in_play` |
| R8 | No test showed that a stale or other-fingerprint pass fails to attach to a candidate in `build_candidates`. | A pass older than `probe_ttl_days` and a pass for another harness version are inserted. Neither attaches. | RD `a_stale_or_other_fingerprint_pass_never_attaches_to_a_candidate` |
| R9 | The seeded-draw test bound (`0 < hits < 60`) admitted almost any rate. | Bound is now `15 <= hits <= 45` of 200 at 15 percent. | RD `the_draw_is_seeded_and_bounded_by_the_percentage` |

Each of R1 to R5 and R7 and R8 was proven by reverting the fix, seeing its test fail, and restoring it (recorded in the final report).

## 3. Accept item to code and tests

Code is under `src/office/`. Tests are `tests/test_route_probe.py` (RP), `tests/v31/test_route_discovery_routing.py` (RD), `tests/v31/test_route_probe_live_shape.py` (LS), `tests/test_route_policy.py` (POL), `tests/test_db_connect.py` (DB), `tests/v31/test_declared_route_426.py` (DR). Names are the test function names with the `test_` prefix dropped.

| accept item | code | tests |
| --- | --- | --- |
| Adoption D8 | commits in section 1 | `git log 341b48b..HEAD`, patch-id table above |
| Adoption record D14 | this file | this file |
| `route_policy.py` and its test change only for D7 | `route_policy.py:40-47` (4 changed lines, nothing else) | POL `status_derivations`, `missing_invocation_evidence_is_unknown_and_not_probe_eligible` |
| Probe launches exact harness, model, effort, adapter worker profile, proves identity readback, response, write inside a temp worktree and none outside | `route_probe.py` `_launch`, `_evaluate`, `_Workspace`, `_boundary_argv`, `_codex_header`, `probe_prompt`, `_Tracker`, `_process_table` | RP `harness_header_readback_is_authoritative_and_accepted`, `probe_runs_in_a_disposable_git_worktree_and_removes_it`, `the_probe_uses_the_adapters_own_worker_profile_unchanged`, `the_boundary_stops_a_write_to_a_users_file_outside_the_workspace`, `the_boundary_admits_the_workspace_and_nothing_wider`, `a_forged_header_in_the_reply_cannot_override_the_harnesss_own`, `a_harness_with_no_metadata_readback_is_never_probed`, `a_setsid_escapee_with_no_workspace_handle_is_attributed_by_the_real_ps_environment` |
| Five reason classes, including an unsupported effort whose sibling effort passes | `route_probe.py` `classify_text`, `_evaluate` | RP `each_failure_class_is_recorded_precisely`, `a_capacity_message_that_mentions_the_model_is_transient_never_terminal`, `an_unsupported_effort_leaves_the_models_other_effort_routable`, `a_wrong_effort_in_the_harness_header_is_the_terminal_negative` |
| Cache key and TTL (pass, temporary failure, terminal unsupported, fingerprint change) | `route_probe.py` `fingerprint`, `key`, `_is_fresh`, `status` | RP `key_names_every_part_of_the_fingerprint`, `pass_qualifies_only_the_exact_effort_model_and_harness`, `records_expire_after_the_ttl`, `an_unsupported_effort_negative_is_not_resurrected_by_ttl_expiry`, `only_the_unsupported_negative_survives_expiry_and_classes_stay_distinct`, `transient_failures_are_retried_sooner_than_the_ttl` |
| Status derivation D7 | `route_policy.py` `row_status`, `alias_status` | POL `status_derivations`, `alias_inherits_target_status_with_target_reason`, `alias_cannot_enable_disabled_target`, `alias_cannot_launder_disabled_target_status_metadata`; RD `an_unconfirmed_row_without_discovery_metadata_is_excluded_and_never_called_unsupported`; `tests/test_alias_resolution.py` `alias_does_not_reenable_non_dispatchable_target` |
| Reserve atomically before launch: pending before launch | `route_probe.py` `reserve`, `_reserve`, `_reserved_event` | RP `the_reservation_exists_before_the_harness_starts` |
| Parallel cap | same | RP `concurrent_distinct_fingerprints_never_exceed_the_run_cap` |
| Exact-fingerprint dedup, no `database is locked` escape | `route_probe.py` `_acquire`, `_wait_inflight`, `ensure` | RP `concurrent_callers_of_one_fingerprint_share_one_launch`, `a_lock_that_outlasts_the_wait_is_a_refusal_not_a_crash_or_a_launch`, `first_open_races_share_one_launch_and_hold_the_cap` |
| Failed and abandoned count toward the cap | `route_probe.py` `_close`, `abandon` | RP `failed_and_abandoned_attempts_both_consume_the_cap`, `an_interrupted_probe_is_abandoned_and_still_counted`, `cached_results_do_not_consume_the_cap` |
| Timeout cleanup | `route_probe.py` `_terminate_tree`, `_Tracker`, `_run_probe` | RP `timeout_kills_the_process_group_and_closes_the_reservation`, `a_descendant_that_leaves_the_process_group_is_still_terminated`, `a_cleanup_that_cannot_be_observed_is_never_reported_clean` |
| Stale pending reservation | `route_probe.py` `expire_stale`, `sweep` | RP `stale_pending_reservation_of_a_dead_owner_is_expired_without_a_second_probe`, `an_overdue_reservation_is_expired_even_if_its_owner_is_alive`, `sweep_expires_stale_reservations_for_any_command`, `sweep_does_not_create_a_database`; LS `the_next_office_command_expires_a_dead_owners_reservation` |
| `ensure` refuses denied, archived, non-eligible, over-budget, quota-reserve routes with no launch and adds no permission or install. A terminal `unsupported-model-effort` record is returned as a cached fail with no launch (it is not a `Refused`). | `route_probe.py` `_static_refusal`, `_quota_refusal`, `ensure`, `_is_fresh` | RP `denied_routes_are_never_probed`, `denying_a_harness_blocks_every_route_on_it`, `archived_and_unsupported_rows_are_never_probed` (archived and `not-eligible`), `an_unsupported_effort_negative_is_not_resurrected_by_ttl_expiry` (terminal record, no relaunch), `a_forged_candidate_cannot_widen_what_the_catalog_allows`, `a_probe_that_would_cross_the_protected_quota_reserve_is_refused`, `the_probe_uses_the_adapters_own_worker_profile_unchanged` (adapter argv only, no added flags beyond the sandbox wrapper), `the_probe_child_carries_no_office_identity` |
| Adapter changes pass `validate-adapter`, no capability granted | `adapters/seed/` unchanged (`git diff 341b48b..HEAD -- adapters/` is empty) | CHECKS line: `validate-adapter` over every `adapters/seed/*.yaml` |
| `office doctor --probe-route` | `doctor.py:179` `probe_route_record`, `cli.py:365`, `cli.py:692-702` | LS `a_manual_probe_prints_the_record_and_audits_an_unbound_attempt`, `a_named_run_is_bound_and_its_probe_cap_holds`, `an_unnamed_run_is_not_silently_bound`, `a_failed_probe_exits_nonzero_with_its_reason_and_spares_sibling_efforts` |
| Immutable attempt evidence D6 | `route_probe.py` `_event`, `_record`; `route_policy.py` `record_event` | RP `every_outcome_appends_its_event_under_the_callers_attempt`, `probe_result_events_carry_the_outcome_and_reason_class`, `the_same_key_probed_twice_keeps_both_results`; LS `a_repeat_is_a_cached_record_and_a_cache_hit_event` |
| Standalone manual probe audit | `doctor.py` `probe_route_record`, `route_probe.py` `ensure` | RP `a_standalone_manual_probe_is_audited_with_explicit_nulls`, `a_bound_manual_probe_records_the_run_and_consumes_its_cap`; LS `a_manual_probe_prints_the_record_and_audits_an_unbound_attempt`, `a_repeat_is_a_cached_record_and_a_cache_hit_event` |
| `route_discovery_events` append-only and validated | T1 schema and `route_policy.record_event`, unchanged | POL `discovery_events_are_append_only_and_survive_migration`, `discovery_event_rejects_missing_or_invalid_context`, `event_requires_transaction_and_rolls_back_with_state_change`, `trial_terminal_outcome_is_unique_and_cache_hits_link_attempts` |
| `db.connect` WAL race D10 | `db.py:176` `_enter_wal`, `db.py:203` `connect` | DB `concurrent_first_opens_of_one_database_all_succeed_in_wal_mode` (8 processes behind a start barrier, three fresh files, then schema and trigger checks), `a_locked_wal_switch_is_retried_until_it_succeeds`, `a_database_that_stays_locked_raises_after_a_bounded_number_of_tries` (`RuntimeError`), `a_database_that_will_not_switch_to_wal_raises_runtime_error`, `an_unrelated_error_is_not_retried`, `a_database_already_in_wal_mode_is_not_switched_again` |
| `build_candidates` and `resolve_aliases` | `candidates.py:362` `build_candidates`, `candidates.py:51` `resolve_aliases` (uses `route_policy.alias_status`) | RD `no_active_catalog_row_is_dropped_silently`, `discovery_on_returns_eligible_rows_as_candidates_with_status_and_a_probe_key`, `discovery_off_keeps_every_discovery_row_out_of_the_candidates_and_names_it_untried`, `an_alias_over_a_disabled_target_is_never_available_and_names_the_target`, `an_alias_over_a_discovery_eligible_target_inherits_eligibility_not_availability`; `tests/test_alias_resolution.py` |
| Declared route refuses a denied route, overkill does not block | `candidates.py:502` `declared_candidate`, `candidates.py:551` `declared_decision` | RD `a_declared_route_refuses_a_denied_route_naming_the_source_and_the_way_back`, `denial_forms_all_block_a_declared_route`, `a_declared_alias_route_is_denied_through_its_target`, `overkill_never_blocks_a_declared_route`, `with_no_user_policy_declared_routes_are_unchanged` |
| `routing.route` stage 1 denial and overkill, stage 2 trial admission | `routing.py:252` `route`, `routing.py:707` `_adaptive` | RD `a_denied_route_is_rejected_at_stage_one_for_every_role_with_its_source`, `overkill_skips_a_route_only_in_automatic_selection_inside_its_scope`, `size_s_and_m_local_and_repo_tasks_are_inside_the_trial_bounds`, `authority_roles_never_see_a_trial_or_probe_intent`, `a_quarantined_candidate_never_takes_a_trial`, `an_unavailable_status_is_never_eligible_without_discovery_for_any_role` |
| Denial read live, unreadable policy fails closed, caps and ceiling pinned | `candidates.py` `live_user_policies`, `required_user_policies`, `route_role`, `declared_candidate` | RD `route_role_honors_a_live_denial_and_fails_closed_on_an_unreadable_policy` (denial added after start, corrupt YAML, wrong-typed value), `the_users_current_config_denies_routes_for_callers_that_pass_no_policy`, `route_role_rejects_a_denied_route_with_its_source_and_a_plan_route_choice_is_refused` (pinned-config denial); RP `a_user_denial_written_after_start_refuses_the_probe`, `an_unreadable_live_policy_refuses_the_probe` |
| Executor floor, unknown benchmark score for a trial | `scoring.py:314` `evaluate_capability_floor` | RD `an_unbenchmarked_trial_candidate_neither_fails_closed_nor_scores_high`, `a_known_low_score_or_low_effort_still_rejects_a_trial_candidate`, `floors_still_fail_closed_for_ordinary_candidates_on_a_missing_score`; `tests/test_scoring.py`, `tests/v31/test_intelligence_floor.py`, `tests/v31/test_model_floor.py` |
| `adaptive` budget ceiling only with a non-shipped source or pre-provenance pin | `adaptive.py:255` `recommend`, `POLICY_VERSION` | RD `the_shipped_economic_scale_is_not_a_ceiling_for_a_new_run`, `a_user_set_ceiling_is_hard_and_its_source_is_disclosed`, `no_cost_removes_a_route_without_a_user_ceiling`, `a_run_pinned_before_provenance_keeps_its_old_ceiling_unchanged`; `tests/v31/test_adaptive_routing.py` |
| Discovery allocation separate from exploration, seeded draw, caps, margin and cost bounds, fallback, `discovery.blocked` | `routing.py` `_adaptive`; `route_probe.py` `rolling`, `allocation` | RD `the_draw_is_seeded_and_bounded_by_the_percentage`, `a_candidate_far_behind_the_primary_fails_the_margin_bound`, `the_exploration_cost_bound_applies_to_a_trial`, `exploration_taking_the_slot_blocks_discovery`, `cold_start_rolling_warmup_blocks_and_is_disclosed`, `the_first_call_does_not_ask_for_a_probe_when_a_cap_or_gate_blocks`; RP `rolling_cap_cold_start_blocks_trials_until_enough_real_decisions_exist`, `rolling_counts_only_trials_inside_the_window_and_only_builder_roles`, `allocation_reports_per_run_probe_and_trial_use` |
| Cold start D5 (probe intent, fresh pass gives trial, fail gives none, gate change drops, never a different route, no probe for planner, reviewer, declared, pinned pre-change) | `routing.py` `route` (`discovery_input`), `_adaptive` | RD `cold_start_yields_a_probe_intent_and_keeps_the_known_working_primary`, `a_probe_intent_is_never_a_trial_source`, `a_stale_or_other_fingerprint_pass_is_not_on_the_candidate_so_it_yields_probe_not_trial` (routing side), `a_stale_or_other_fingerprint_pass_never_attaches_to_a_candidate` (`build_candidates` side), `recompute_with_a_fresh_pass_yields_a_trial_for_that_candidate_only`, `recompute_with_a_failed_probe_drops_the_candidate_and_keeps_the_known_working_route`, `every_probe_failure_class_blocks_a_trial_with_its_own_reason`, `recompute_re_evaluates_every_gate_and_drops_the_candidate_with_its_reason`, `recompute_never_selects_a_different_untried_route`, `route_role_drives_cold_start_probe_then_trial_without_minting_trust`, `route_role_with_discovery_off_a_pinned_run_a_manual_route_or_a_reviewer_never_discovers` |
| Discovery off or pinned pre-change run is byte-identical | `routing.py` (all new paths gated on discovery enablement) | RD `routing_output_is_byte_identical_to_the_pre_change_commit_when_discovery_is_not_in_play` (golden values from `341b48b`), `discovery_off_ignores_pool_candidates_and_hashes_exactly_as_before`, `routing_output_is_unchanged_for_a_request_with_no_discovery_inputs`; `tests/v31/test_adaptive_routing.py` |
| No path writes `adapter_trust_acts` or `recorded_overrides` | `route_probe.py` and the new `candidates.py` and `routing.py` paths write neither table | RP `no_probe_path_writes_trust_or_overrides`; RD `route_role_drives_cold_start_probe_then_trial_without_minting_trust` |
| B1 live denial at the probe | `route_probe.py` `_denial`, `_static_refusal`; `candidates.py:155` `current_user_policies` | RP `a_user_denial_written_after_start_refuses_the_probe`, `a_repo_denial_written_after_start_refuses_the_probe`, `an_unbound_manual_probe_also_honors_the_live_denial`, `the_live_policy_cache_survives_a_concurrent_clear`, `an_unreadable_live_policy_refuses_the_probe`; LS `a_user_denied_route_is_never_probed_through_the_cli` |
| B2 declared route under overkill | `dispatch.py:229` `planned_route` (manual or declared handling only) | DR `a_declared_route_under_an_overkill_rule_is_dispatched`, `the_same_overkill_route_stays_skipped_for_an_undeclared_task`, `a_declared_route_denied_later_is_refused_not_dispatched` |
| B1 and B2 shown failing before the fix, run through the bounded wrapper, none skipped | section 4 | no unconditional `skip` or `xfail` in any file in scope. Four tests carry a conditional `skipif` that applies only where `write_boundary_reason()` reports no OS write boundary (not darwin): the adapter-profile test and the three `@boundary` tests in `tests/test_route_probe.py`. Nothing is skipped on this platform, and the OS-boundary evidence is darwin-only. |

## 4. Regression output

All runs use the run's bounded check wrapper: `bounded-check.py -- .venv/bin/python -m pytest -n 2 ...`. Failing-before runs restore the named source files to the earlier commit (`git checkout <sha> -- <files>`) and keep the tests as committed at HEAD. The working tree was restored afterwards.

### B1: live denial at the probe

Before (`src/office/{route_probe,candidates,dispatch}.py` at `e1e3f82`), `tests/test_route_probe.py -k "live or written_after or unbound_manual or unreadable_live or concurrent_clear"`:

```
FAILED tests/test_route_probe.py::test_a_repo_denial_written_after_start_refuses_the_probe
FAILED tests/test_route_probe.py::test_a_user_denial_written_after_start_refuses_the_probe
FAILED tests/test_route_probe.py::test_the_live_policy_cache_survives_a_concurrent_clear
FAILED tests/test_route_probe.py::test_an_unbound_manual_probe_also_honors_the_live_denial
FAILED tests/test_route_probe.py::test_an_unreadable_live_policy_refuses_the_probe
5 failed, 1 passed
```

Excerpt: the probe ran and returned a passing record where `Refused('route-denied')` and `Refused('policy-unreadable')` were required.

After (HEAD of the B1/B2/C3 commits): `tests/test_route_probe.py tests/test_db_connect.py` gave `102 passed in 26.06s`.

### B2: declared route under overkill

Before (`src/office/dispatch.py` at `e1e3f82`), `-n 2 --all tests/v31/test_declared_route_426.py`:

```
FAILED tests/v31/test_declared_route_426.py::test_a_declared_route_under_an_overkill_rule_is_dispatched
E   AssertionError: blocked: no-route: T1: every planned route is unavailable now (codex@1/gpt-6-astra@low: overkill by user routing.user_policy.overkill_rules: codex/gpt-6-astra@low)
1 failed, 11 passed
```

After (HEAD): `12 passed in 8.06s`.

### C3: concurrent first-open WAL race

Before (`src/office/db.py` at `e1e3f82~1`), `tests/test_db_connect.py`:

```
src/office/db.py:181: in connect
    mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
E   sqlite3.OperationalError: database is locked
FAILED tests/test_db_connect.py::test_a_database_that_stays_locked_raises_after_a_bounded_number_of_tries
FAILED tests/test_db_connect.py::test_a_database_already_in_wal_mode_is_not_switched_again
FAILED tests/test_db_connect.py::test_concurrent_first_opens_of_one_database_all_succeed_in_wal_mode
FAILED tests/test_db_connect.py::test_a_locked_wal_switch_is_retried_until_it_succeeds
4 failed, 1 passed
```

After: `tests/test_db_connect.py` is part of the run above.

### Final run on the composed tree (after the section 2a fixes)

Through the bounded wrapper, one suite at a time:

```
pytest -n 2 tests/test_db_connect.py tests/test_route_policy.py tests/test_catalog_archival.py tests/test_route_probe.py \
  tests/test_adapters.py tests/test_alias_resolution.py tests/test_derived_routing.py tests/test_scoring.py \
  tests/test_trust_conformance.py                                                   184 passed
pytest -n 2 tests/v31/test_route_probe_live_shape.py tests/v31/test_adaptive_routing.py \
  tests/v31/test_intelligence_floor.py tests/v31/test_model_floor.py tests/v31/test_dispatch_override.py \
  tests/v31/test_declared_route_426.py tests/v31/test_route_discovery_routing.py     129 passed
pytest -n 2 --all tests/v31/test_declared_route_426.py                              12 passed
pytest -n 2 --all tests/v31/test_route_discovery_routing.py tests/v31/test_route_probe_live_shape.py \
  tests/v31/test_adaptive_routing.py tests/v31/test_dispatch_override.py           151 passed
pytest -n 2 (default unit tier, whole repo)                                         1629 passed, 1 skipped
validate-adapter over adapters/seed/*.yaml                                           5 of 5 exit 0
```

The race and dedup tests (`first_open`, `concurrent`, `share_one_launch`, `cap` in `tests/test_db_connect.py` and `tests/test_route_probe.py`) passed on three serialized `-n 2` runs in a row (13 passed each).

## 5. Known limits, unchanged by this round

These were raised by the self-review. None contradicts an accept line. They are listed so the limits are visible.

- The write boundary is a `sandbox-exec` file-write deny. It stops direct writes outside the workspace. A write made on the probe's behalf by a privileged daemon (a Mach service such as `cfprefsd`, a `launchctl` job, a unix-socket service) is not blocked, and the post-run snapshot covers only the probe's temp area. Closing this needs a deny-default profile with a per-harness allowlist, which cannot be validated here without a real harness.
- The probe child inherits the parent environment minus the Office identity variables. A per-harness environment allowlist needs each adapter to declare its auth variables.
- A sidecar's workspace is trusted only under the current process's `tempfile.gettempdir()`. A sweeper started with a different `TMPDIR` skips workspace attribution and relies on the process tree and environment tag.
- If `runs.db` stays locked beyond `busy_timeout` while a probe records its verdict, the caller sees the lock error, the reservation is expired by the next command and stays counted.
- Non-darwin hosts have no write boundary, so probes are refused there and four boundary tests are skipped.

`OFFICE_SELF_REVIEW.md` is the per-round self-review ledger, which Office consumes at submit. It is not evidence for this item.
