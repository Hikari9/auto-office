# Policy-driven runtime contract suite

The core suite is in `tests/contracts/core/`. It contains 41 named test functions (51 collected pytest nodes, including parametrized cases) across configuration integrity, policy precedence, runtime selection, safety invariants, evidence replay, and three actual CLI subprocess smokes. Route selection cases use synthetic candidate identities and hand-written expected outcomes; changing a preference fixture changes the selected route without editing assertions.

## Twelve #871 §4A properties

| # | Contract | Core test evidence | Retained end-to-end evidence |
|---|---|---|---|
| 1 | A and B are equally eligible; reversing only preference data reverses the winner. | `test_preference_mutation_changes_the_runtime_winner_without_test_edits`, `test_reversing_only_preference_data_reverses_selection` | — |
| 2 | A role deny affects that role and leaves an independently qualified reviewer unaffected. | `test_executor_deny_does_not_deny_independent_reviewer`, `test_reversing_role_scope_reverses_only_the_targeted_effect` | — |
| 3 | Exact effort and size scopes do not leak; wildcard rules widen only when explicitly marked. | `test_deny_is_effort_exact`, `test_deny_is_size_scoped`, `test_explicit_wildcard_is_broader_than_exact_effort` | — |
| 4 | Unlisted neutral candidates remain eligible subject to technical gates. | `test_unlisted_candidate_remains_neutral_and_selectable`, `test_missing_historical_trust_does_not_veto_neutral_route` | — |
| 5 | Authorized user policy is auditable; forged quotes or candidate fields cannot create authority. | `test_exact_allow_overrides_a_wildcard_deny`, `test_explicit_user_allow_has_auditable_provenance`, `test_orchestrator_quote_cannot_forge_user_override`, `test_explicit_override_is_not_authorized_by_a_candidate_field` | `tests/v31/test_issue_845_user_allow.py::test_a_user_allow_skips_the_benchmark_floor_but_not_a_deny` |
| 6 | Real launch failure attempts the next eligible route and records the failure; a declared route does not silently switch. | `test_quota_ordering_selects_safe_fallback` covers selection-time fallback. | `tests/v31/test_issue_845_user_allow.py::test_a_runtime_failure_is_logged_with_the_route_and_reason_then_falls_back`, `::test_a_declared_route_is_logged_and_demoted_but_never_swapped_silently` |
| 7 | Changing a configured floor affects only eligibility within its declared size. | `test_changing_the_configured_floor_changes_only_that_candidate_gate` | `tests/contracts/test_executor_policy.py::test_temporary_floor_policy_changes_runtime_eligibility` |
| 8 | Reviewer selection follows configured preferences; mandatory review still follows independent gate rules. | `test_preference_mutation_changes_the_runtime_winner_without_test_edits` | `tests/v31/test_issue809_xl_review.py::test_xl_initial_plan_review_runs_three_goldfish_then_one_distinct_lead`, `tests/v31/test_gates.py::test_waiving_failed_checks_still_requires_independent_review`, `::test_waiving_failed_checks_never_accepts_before_the_review_verdict` |
| 9 | New configuration does not rewrite a run's pinned policy or route history. | — | `tests/v31/test_executor_binding_config_drift.py::test_start_reads_each_config_file_once_so_pin_and_baseline_agree`, `::test_start_overrides_are_not_drift_but_file_edits_are` |
| 10 | Missing, altered, or mismatched check evidence cannot attest a PASS. | `test_valid_artifact_attests_only_its_bound_revision`, `test_tampered_or_false_pass_evidence_is_rejected`, `test_gate_must_be_bound_to_the_recorded_revision` | `tests/v31/test_issue_563_artifact_integrity.py`, `tests/v31/test_verification_coverage.py` |
| 11 | Input order cannot change deterministic winners; ineligible additions cannot displace valid candidates. | `test_input_permutation_preserves_the_unique_preferred_winner`, `test_equal_candidates_use_stable_tie_breaking_after_input_permutation`, `test_ineligible_route_cannot_change_a_valid_winner` | — |
| 12 | A routing override cannot grant missing capability or self-approval. | `test_user_allow_does_not_create_missing_capability`, `test_role_allow_does_not_override_a_separate_technical_failure` | `tests/v31/test_convergence_cap_waiver.py::test_the_producing_session_cannot_review_its_own_work`, `tests/v31/test_gates.py::test_waiving_failed_checks_still_requires_independent_review` |

## Faults that must fail

| Injected fault | Test that detects it |
|---|---|
| Ignore an executor user deny | `tests/contracts/core/test_policy_precedence.py::test_executor_deny_does_not_deny_independent_reviewer` |
| Reverse the configured preference order | `tests/contracts/core/test_runtime_selection.py::test_reversing_only_preference_data_reverses_selection` |
| Skip required independent review after checks are waived | `tests/v31/test_gates.py::test_waiving_failed_checks_still_requires_independent_review` |
| Report fallback without attempting and recording the next route | `tests/v31/test_issue_845_user_allow.py::test_a_runtime_failure_is_logged_with_the_route_and_reason_then_falls_back` |
| Accept forged or changed PASS evidence | `tests/contracts/core/test_receipts_replay.py::test_tampered_or_false_pass_evidence_is_rejected` |

The three CLI smoke tests invoke the real `office` executable in child processes. Their adapter executable is a temporary script that answers only local preflight and launch prompts; each uses isolated `OFFICE_USER_CONFIG`, `OFFICE_USER_ADAPTERS`, `OFFICE_DATA_HOME`, `OFFICE_STATE_HOME`, and runs DB paths. The checks do not call a real provider.

## Measurement

Measured on this Mac (Apple Silicon, macOS, 18 logical CPUs; CPython 3.14.8) with `/usr/bin/time -l .venv/bin/python -m pytest -n 2 tests/contracts/core -q`:

- 41 named functions, 51 collected nodes; 51 passed.
- Wall time: 1.65 seconds (the host load averages were 22.36 / 41.36 / 39.05 during this run).
- Maximum resident set size: 62,685,184 bytes (59.8 MiB).

This is the new core suite measurement, not a measurement of the PR selector or the historical full suite.

## Legacy test-module disposition

Every test module tracked at the T2 base is listed below. All are **keep**: this task removes, renames, archives, or suppresses no test. The new core contracts add focused coverage while the existing modules retain their distinct historical, safety, and integration assertions.

| Legacy module | Disposition |
|---|---|
| `tests/contracts/test_executor_policy.py` | keep |
| `tests/test_adapters.py` | keep |
| `tests/test_agy_usage.py` | keep |
| `tests/test_alias_resolution.py` | keep |
| `tests/test_amend_checks_only.py` | keep |
| `tests/test_amend_contract_edits.py` | keep |
| `tests/test_amend_requirements_delivery.py` | keep |
| `tests/test_amendments.py` | keep |
| `tests/test_budget.py` | keep |
| `tests/test_catalog_archival.py` | keep |
| `tests/test_checks_stale.py` | keep |
| `tests/test_db_connect.py` | keep |
| `tests/test_derived_routing.py` | keep |
| `tests/test_dogfood.py` | keep |
| `tests/test_families.py` | keep |
| `tests/test_hooks.py` | keep |
| `tests/test_instruction_conformance.py` | keep |
| `tests/test_integration.py` | keep |
| `tests/test_landings.py` | keep |
| `tests/test_monitor.py` | keep |
| `tests/test_packet_delivery_channel.py` | keep |
| `tests/test_pane_busy.py` | keep |
| `tests/test_pr_report.py` | keep |
| `tests/test_pr_test_selection.py` | keep |
| `tests/test_pre_push_hook.py` | keep |
| `tests/test_propose.py` | keep |
| `tests/test_release_identity.py` | keep |
| `tests/test_reuse_compact.py` | keep |
| `tests/test_review.py` | keep |
| `tests/test_route_policy.py` | keep |
| `tests/test_route_probe.py` | keep |
| `tests/test_runtime.py` | keep |
| `tests/test_schemas.py` | keep |
| `tests/test_scoring.py` | keep |
| `tests/test_session_end_hook.py` | keep |
| `tests/test_shipped_catalog_tables.py` | keep |
| `tests/test_spoke_receipt_integrity.py` | keep |
| `tests/test_trust_alias_625.py` | keep |
| `tests/test_trust_conformance.py` | keep |
| `tests/test_v3_instruction_contract.py` | keep |
| `tests/test_version_serves.py` | keep |
| `tests/test_version_single_source.py` | keep |
| `tests/test_worktree.py` | keep |
| `tests/v31/test_adaptive_dispatch.py` | keep |
| `tests/v31/test_adaptive_routing.py` | keep |
| `tests/v31/test_agent_questions.py` | keep |
| `tests/v31/test_agy_catalog_626.py` | keep |
| `tests/v31/test_agy_executor.py` | keep |
| `tests/v31/test_amend_accepted.py` | keep |
| `tests/v31/test_amend_additive.py` | keep |
| `tests/v31/test_argon_catalog_809.py` | keep |
| `tests/v31/test_benchmark_refresh.py` | keep |
| `tests/v31/test_bug_hint.py` | keep |
| `tests/v31/test_cap_diff.py` | keep |
| `tests/v31/test_cli_override_harness.py` | keep |
| `tests/v31/test_cli_runtimes.py` | keep |
| `tests/v31/test_cli_usage_hints.py` | keep |
| `tests/v31/test_close_external.py` | keep |
| `tests/v31/test_closeout.py` | keep |
| `tests/v31/test_codex_pane_session.py` | keep |
| `tests/v31/test_codex_startup.py` | keep |
| `tests/v31/test_commands_ledger.py` | keep |
| `tests/v31/test_commit_identity.py` | keep |
| `tests/v31/test_compat_hooks_install.py` | keep |
| `tests/v31/test_composer_held.py` | keep |
| `tests/v31/test_config_command.py` | keep |
| `tests/v31/test_config_sandbox.py` | keep |
| `tests/v31/test_context_snapshot.py` | keep |
| `tests/v31/test_convergence_cap_waiver.py` | keep |
| `tests/v31/test_convergence_contract.py` | keep |
| `tests/v31/test_convergence_reviewed_commit.py` | keep |
| `tests/v31/test_ctx_alert.py` | keep |
| `tests/v31/test_declared_route_426.py` | keep |
| `tests/v31/test_defect_criteria.py` | keep |
| `tests/v31/test_defect_redirect.py` | keep |
| `tests/v31/test_dependency_readiness.py` | keep |
| `tests/v31/test_deploy_env_files.py` | keep |
| `tests/v31/test_discovery_dispatch.py` | keep |
| `tests/v31/test_discovery_pinning.py` | keep |
| `tests/v31/test_dispatch_fence_361.py` | keep |
| `tests/v31/test_dispatch_override.py` | keep |
| `tests/v31/test_doctor_repairs.py` | keep |
| `tests/v31/test_dormant_lease.py` | keep |
| `tests/v31/test_e2e_flow.py` | keep |
| `tests/v31/test_economics.py` | keep |
| `tests/v31/test_executor_binding_config_drift.py` | keep |
| `tests/v31/test_external_visual.py` | keep |
| `tests/v31/test_finding_identity.py` | keep |
| `tests/v31/test_fixture_source_path.py` | keep |
| `tests/v31/test_gates.py` | keep |
| `tests/v31/test_haiku_5_5.py` | keep |
| `tests/v31/test_harness_model_cli.py` | keep |
| `tests/v31/test_harness_version_memo.py` | keep |
| `tests/v31/test_headless_agent_env.py` | keep |
| `tests/v31/test_headless_fallback.py` | keep |
| `tests/v31/test_herdr_agent_launch.py` | keep |
| `tests/v31/test_herdr_landing.py` | keep |
| `tests/v31/test_herdr_panes.py` | keep |
| `tests/v31/test_herdr_review_capture.py` | keep |
| `tests/v31/test_hook_ledger.py` | keep |
| `tests/v31/test_hostmetrics.py` | keep |
| `tests/v31/test_idle_final_status.py` | keep |
| `tests/v31/test_inprocess_launch.py` | keep |
| `tests/v31/test_inspect_route_discovery.py` | keep |
| `tests/v31/test_install_drift.py` | keep |
| `tests/v31/test_integrated_review.py` | keep |
| `tests/v31/test_intelligence_floor.py` | keep |
| `tests/v31/test_issue809_executor_slates.py` | keep |
| `tests/v31/test_issue809_inline_dispatch.py` | keep |
| `tests/v31/test_issue809_pane_size_label.py` | keep |
| `tests/v31/test_issue809_real_routes.py` | keep |
| `tests/v31/test_issue809_regression.py` | keep |
| `tests/v31/test_issue809_rollout_metrics.py` | keep |
| `tests/v31/test_issue809_run_gear.py` | keep |
| `tests/v31/test_issue809_self_review_subagents.py` | keep |
| `tests/v31/test_issue809_xl_review.py` | keep |
| `tests/v31/test_issue811_terra_supersession.py` | keep |
| `tests/v31/test_issue846_size_seeds.py` | keep |
| `tests/v31/test_issue_267.py` | keep |
| `tests/v31/test_issue_268.py` | keep |
| `tests/v31/test_issue_399_dispatch_recovery.py` | keep |
| `tests/v31/test_issue_494_scenarios.py` | keep |
| `tests/v31/test_issue_530_stale_waiver.py` | keep |
| `tests/v31/test_issue_537_integrity.py` | keep |
| `tests/v31/test_issue_537_shared_append.py` | keep |
| `tests/v31/test_issue_538_safe_pr_sync.py` | keep |
| `tests/v31/test_issue_541_upgrade.py` | keep |
| `tests/v31/test_issue_563_artifact_integrity.py` | keep |
| `tests/v31/test_issue_571_shell_safety.py` | keep |
| `tests/v31/test_issue_573_visual_fallback.py` | keep |
| `tests/v31/test_issue_580_rebase_bases.py` | keep |
| `tests/v31/test_issue_629_integrity.py` | keep |
| `tests/v31/test_issue_644_wrapped_requirements.py` | keep |
| `tests/v31/test_issue_651_playwright_doctor.py` | keep |
| `tests/v31/test_issue_653_markdown_prose.py` | keep |
| `tests/v31/test_issue_654_already_merged_land.py` | keep |
| `tests/v31/test_issue_655_identical_new_file.py` | keep |
| `tests/v31/test_issue_659_pr_title_slug.py` | keep |
| `tests/v31/test_issue_662_manual_deploy.py` | keep |
| `tests/v31/test_issue_664_native_select_steps.py` | keep |
| `tests/v31/test_issue_668_prose_git_guard.py` | keep |
| `tests/v31/test_issue_669_visual_readiness.py` | keep |
| `tests/v31/test_issue_678_unsafe_run_base.py` | keep |
| `tests/v31/test_issue_696_amend_restack.py` | keep |
| `tests/v31/test_issue_700_shared_dependency.py` | keep |
| `tests/v31/test_issue_703_effective_checks.py` | keep |
| `tests/v31/test_issue_704_repository_root.py` | keep |
| `tests/v31/test_issue_705_squash_close.py` | keep |
| `tests/v31/test_issue_707_route_authority.py` | keep |
| `tests/v31/test_issue_709_invocation_guidance.py` | keep |
| `tests/v31/test_issue_712_deploy_artifacts.py` | keep |
| `tests/v31/test_issue_714_topological_restack.py` | keep |
| `tests/v31/test_issue_721_accepted_route.py` | keep |
| `tests/v31/test_issue_734_fetched_base.py` | keep |
| `tests/v31/test_issue_740_recovery.py` | keep |
| `tests/v31/test_issue_740_review_correctness.py` | keep |
| `tests/v31/test_issue_783_cleanup.py` | keep |
| `tests/v31/test_issue_783_herdr_smoke.py` | keep |
| `tests/v31/test_issue_790_held_pointer.py` | keep |
| `tests/v31/test_issue_790_review_route.py` | keep |
| `tests/v31/test_issue_817_effort_ceiling.py` | keep |
| `tests/v31/test_issue_845_invocation_table.py` | keep |
| `tests/v31/test_issue_845_user_allow.py` | keep |
| `tests/v31/test_job_ownership.py` | keep |
| `tests/v31/test_kilo_registration.py` | keep |
| `tests/v31/test_land.py` | keep |
| `tests/v31/test_land_recovery.py` | keep |
| `tests/v31/test_landing_gaps.py` | keep |
| `tests/v31/test_lane_gate_revoke.py` | keep |
| `tests/v31/test_launch_argv_evidence.py` | keep |
| `tests/v31/test_lineage_supersession_811.py` | keep |
| `tests/v31/test_live_delivery_stall.py` | keep |
| `tests/v31/test_model_floor.py` | keep |
| `tests/v31/test_model_lineages_610.py` | keep |
| `tests/v31/test_multi_dependency_base.py` | keep |
| `tests/v31/test_new_runs_default.py` | keep |
| `tests/v31/test_notice_priority.py` | keep |
| `tests/v31/test_office_prompt.py` | keep |
| `tests/v31/test_onboarding.py` | keep |
| `tests/v31/test_opencode_registration.py` | keep |
| `tests/v31/test_opencode_reviewer.py` | keep |
| `tests/v31/test_opencode_worker_pane.py` | keep |
| `tests/v31/test_orchestrator_fallback_and_reconcile.py` | keep |
| `tests/v31/test_orphaned_dispatch.py` | keep |
| `tests/v31/test_outcome_labels.py` | keep |
| `tests/v31/test_pane_reclaim.py` | keep |
| `tests/v31/test_pi_registration.py` | keep |
| `tests/v31/test_plan_diagram.py` | keep |
| `tests/v31/test_plan_path.py` | keep |
| `tests/v31/test_plan_review_lifecycle.py` | keep |
| `tests/v31/test_planner_amend_serial.py` | keep |
| `tests/v31/test_preexisting_checks.py` | keep |
| `tests/v31/test_preflight.py` | keep |
| `tests/v31/test_prompt_landed_baseline.py` | keep |
| `tests/v31/test_prune.py` | keep |
| `tests/v31/test_quota_probe.py` | keep |
| `tests/v31/test_quota_routing.py` | keep |
| `tests/v31/test_raise.py` | keep |
| `tests/v31/test_refused_submit_scope.py` | keep |
| `tests/v31/test_request_scope_paths.py` | keep |
| `tests/v31/test_rerun.py` | keep |
| `tests/v31/test_retired_mimo_catalog.py` | keep |
| `tests/v31/test_review_reroute.py` | keep |
| `tests/v31/test_review_stopping_rule.py` | keep |
| `tests/v31/test_reviewer_files.py` | keep |
| `tests/v31/test_reviewer_read_scope.py` | keep |
| `tests/v31/test_revoke_accepted.py` | keep |
| `tests/v31/test_revoke_stops.py` | keep |
| `tests/v31/test_risk_floor_lightweight.py` | keep |
| `tests/v31/test_role_override_837.py` | keep |
| `tests/v31/test_role_override_dispatch_837.py` | keep |
| `tests/v31/test_role_policy_837.py` | keep |
| `tests/v31/test_role_policy_cli_837.py` | keep |
| `tests/v31/test_role_seed_routing.py` | keep |
| `tests/v31/test_rolling_review.py` | keep |
| `tests/v31/test_root_wake_723.py` | keep |
| `tests/v31/test_round_cap_stop.py` | keep |
| `tests/v31/test_route_change.py` | keep |
| `tests/v31/test_route_discovery_consumer_contract.py` | keep |
| `tests/v31/test_route_discovery_routing.py` | keep |
| `tests/v31/test_route_learning_discovery.py` | keep |
| `tests/v31/test_route_policy_config.py` | keep |
| `tests/v31/test_route_probe_live_shape.py` | keep |
| `tests/v31/test_routing_evidence_convergence.py` | keep |
| `tests/v31/test_routing_evidence_dispatch.py` | keep |
| `tests/v31/test_routing_evidence_report.py` | keep |
| `tests/v31/test_routing_evidence_tags.py` | keep |
| `tests/v31/test_routing_properties.py` | keep |
| `tests/v31/test_run_checks.py` | keep |
| `tests/v31/test_run_routing_optin.py` | keep |
| `tests/v31/test_scheduler.py` | keep |
| `tests/v31/test_schema_migration.py` | keep |
| `tests/v31/test_schema_v6_migration.py` | keep |
| `tests/v31/test_scope_attribution.py` | keep |
| `tests/v31/test_scope_none.py` | keep |
| `tests/v31/test_scope_none_evidence.py` | keep |
| `tests/v31/test_scoring_properties.py` | keep |
| `tests/v31/test_self_improve.py` | keep |
| `tests/v31/test_self_review_ledger.py` | keep |
| `tests/v31/test_self_review_receipt.py` | keep |
| `tests/v31/test_session_capture.py` | keep |
| `tests/v31/test_sonnet_5_5.py` | keep |
| `tests/v31/test_stacked_restack.py` | keep |
| `tests/v31/test_start_trust.py` | keep |
| `tests/v31/test_startup_prompts.py` | keep |
| `tests/v31/test_startup_trust_short_pane.py` | keep |
| `tests/v31/test_state_core.py` | keep |
| `tests/v31/test_task_aware_routing.py` | keep |
| `tests/v31/test_task_prs.py` | keep |
| `tests/v31/test_task_prs_stacking.py` | keep |
| `tests/v31/test_trust_major.py` | keep |
| `tests/v31/test_upstream_reaccept.py` | keep |
| `tests/v31/test_verification_coverage.py` | keep |
| `tests/v31/test_visual.py` | keep |
| `tests/v31/test_visual_port_conflict.py` | keep |
| `tests/v31/test_visual_preflight.py` | keep |
| `tests/v31/test_wait.py` | keep |
| `tests/v31/test_worktree_setup.py` | keep |
| `tests/web/browser/test_agents.py` | keep |
| `tests/web/browser/test_allocation.py` | keep |
| `tests/web/browser/test_chat.py` | keep |
| `tests/web/browser/test_e2e.py` | keep |
| `tests/web/browser/test_issues.py` | keep |
| `tests/web/browser/test_layout.py` | keep |
| `tests/web/browser/test_model_registry_610.py` | keep |
| `tests/web/browser/test_routing.py` | keep |
| `tests/web/browser/test_settings.py` | keep |
| `tests/web/browser/test_shell.py` | keep |
| `tests/web/browser/test_smoke.py` | keep |
| `tests/web/browser/test_workbench.py` | keep |
| `tests/web/test_activity.py` | keep |
| `tests/web/test_api_commands.py` | keep |
| `tests/web/test_capabilities.py` | keep |
| `tests/web/test_chat.py` | keep |
| `tests/web/test_github.py` | keep |
| `tests/web/test_identity.py` | keep |
| `tests/web/test_land_via_office.py` | keep |
| `tests/web/test_launcher.py` | keep |
| `tests/web/test_model_registry_610.py` | keep |
| `tests/web/test_observer.py` | keep |
| `tests/web/test_perf.py` | keep |
| `tests/web/test_projection.py` | keep |
| `tests/web/test_readback.py` | keep |
| `tests/web/test_reconnect.py` | keep |
| `tests/web/test_repos.py` | keep |
| `tests/web/test_server.py` | keep |
| `tests/web/test_settings_api.py` | keep |
| `tests/web/test_target_validation.py` | keep |
