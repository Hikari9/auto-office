# Auto Office 3.x — Unified Acceptance Inventory (DRAFT)

**Status: draft for maintainer markup. Not authoritative.** The authoritative sources remain
[`v3-acceptance.md`](v3-acceptance.md) (3.0), [`v3-acceptance-residue.md`](v3-acceptance-residue.md)
(3.0 failure analysis), and [`v31-implementation.md`](v31-implementation.md) §15–§17 (3.1).

Purpose: list every 3.0 and 3.1 acceptance claim in one table so the maintainer can choose, per
row, whether it is kept or superseded. Each 3.0 row sits next to its 3.1 counterpart, grouped by
area. Edit the "Proposed disposition" column.

Conventions:

- **Source row ID.** 3.0 rows use the matrix's Source & Decision ID. 3.1 rows use `§15.n` (nth row
  of the traceability table), `§16.n` (live conformance table), or `§17` (spec §24 evaluation).
- **Proof status.** `passes`: cited test exists and passed on this branch (2026-09-27; 3.0 suites
  run with `OFFICE_PINNED_LEGACY=1`, one suite at a time). `broken-cite`: the cited test name or
  file does not exist. `weak-proof`: the command runs but does not falsify the claim (whole-file
  run, grep of an instruction document, decision-record or config-literal check, or an assertion
  the residue says is missing). `none`: no command, or the implementation is missing.
- **Dispositions.** `KEEP`, `SUPERSEDED-BY <target>`, `MERGE-WITH <ID>`, `RETIRE`. `UNDECIDED
  (#n)` marks a row whose disposition depends on an open maintainer decision.
- **Instruction-layer proof (#117).** A text or regex search over instruction docs does not prove
  an instruction-layer row. Only a recorded live or fixture run counts (for example `v31` §16).
  Rows without one are `weak-proof` and say "needs recorded run".
- **Past-merge proof (#118).** Verify in order: (a) `git merge-base --is-ancestor <sha> origin/main`
  when the commit was not squashed; (b) otherwise the carrying PR is merged into main
  (`gh pr view N --json state,mergeCommit`); (c) otherwise status `attested` with the SHA.
- Maintainer decisions already applied: #127 and #129 are in scope for v3.x (KEEP, implementation
  missing); #128 fit_gear must select all six modes (KEEP); #120 CI stays empty and `validate.yml`
  will be removed, so the CI rows are superseded by that decision.
- Run observations: `tests/v31` gave 63 passed, 1 skipped, 1 failed
  (`test_compat_hooks_install.py::test_install_is_idempotent_backs_up_and_uninstall_removes_only_managed`,
  which no §15 row cites). The 3.0 suites used for the re-points (amendments, families, scoring,
  derived_routing, trust_conformance, review, propose, monitor, pr_report, integration) gave 188
  passed. Every re-point target in the table exists in the tree.

Short form: `v31` = `docs/v31-implementation.md`.

## Inventory

| ID | Source row ID | Source | Requirement | Current proof | Proof status | Proposed disposition | Related issue | Rationale |
|---|---|---|---|---|---|---|---|---|
| **A. Lifecycle roles and intake** | | | | | | | | |
| U001 | issue-35#decision-1 | 3.0 | Orchestrator captures provisional intent while the planner investigates and interviews | `pytest tests/test_v3_instruction_contract.py` (doc text assertion) | weak-proof | KEEP | #117 | needs recorded run: no §16 or fixture run exercises this; §16.9 (F01) planned without a user interview |
| U002 | issue-77#provisional-kickoff | 3.0 | Kickoff gathers only initial intent and launches planner with a provisional packet | `-k test_provisional_kickoff` (absent) | weak-proof | KEEP | #117 | needs recorded run: no §16 or fixture run exercises this |
| U003 | issue-77#interactive-planner | 3.0 | Dedicated planner does recon, interviews the user, reshapes the problem | `-k test_interactive_planner` (absent) | weak-proof | KEEP | #117 | needs recorded run: no §16 or fixture run exercises this; §16.9 exercised planning but not the interview |
| U004 | issue-35#child-47 | 3.0 | Orchestrator fully grills requirements; planner never talks to user | `git grep` of lifecycle spec | weak-proof | RETIRE | — | Already superseded by decision-1 / #77; doc-text-search-only proof, needs recorded run if reopened |
| U005 | issue-35#decision-3 | 3.0 | Executor owns code-review dispositions and evidence-backed refutations | `pytest tests/test_v3_instruction_contract.py` (doc text assertion) | weak-proof | KEEP | #117 | needs recorded run: no §16 or fixture run exercises this; §16.9 reviewed but recorded no refutation |
| U006 | issue-77#orchestrator-distribution | 3.0 | Orchestrator distributes executor packets by the planner's dependency graph | `-k test_orchestrator_distribution` | broken-cite | SUPERSEDED-BY U036 | #117 | 3.1 dispatch owns parallel/stacked distribution (v31:92 §5) |
| U007 | issue-93#structured-intake-fallback | 3.0 | Intake falls back to batched plain-text questions without ask_question | `-k test_intake_fallback` (absent) | weak-proof | KEEP | #117 | needs recorded run: no §16 or fixture run exercises this |
| U008 | issue-93#spoke-loading | 3.0 | Spoke loading is portable across Claude, Codex, agy with mark-spoke receipts | `-k test_spoke_loading` | broken-cite | SUPERSEDED-BY v31:234 (§14) | #117 | 3.1 retires `office spoke` and the spoke digest ritual |
| U009 | issue-35#child-81 | 3.0 | Five task-shape playbooks specialize procedure and evidence | `-k test_start_cli_can_select_full_fit_path` | weak-proof | KEEP | #122 | Test exists but does not assert the playbooks |
| **B. Amendments and versioning** | | | | | | | | |
| U010 | issue-35#decision-5 | 3.0 | requirements/plan/routing versions are independent; routing-only change keeps approval | re-point `tests/test_amendments.py::AmendmentTransitionMatrixTests` | broken-cite | SUPERSEDED-BY U013 | #121 | 3.1 versions amendments per task with delivery states (v31:144 §7) |
| U011 | issue-77#routing-amendment | 3.0 | Routing-only amendment bumps routing_version and retargets pending work | re-point `test_amendments.py::RoutingOnlyPreservesApprovalTests` | broken-cite | SUPERSEDED-BY v31:234 (§14) | #121 | 3.1 routing is automatic in dispatch; `office route` retired |
| U012 | issue-77#requirements-amendment-fits-plan | 3.0 | Fitting requirements amendment bumps requirements_version, notifies executors | re-point `test_amendments.py::test_matrix_requirements_delta_still_fitting_plan_does_not_wake_planner` | broken-cite | SUPERSEDED-BY v31:144 (§7 ordinary amendment) | #121 | Notify clause was never asserted; 3.1 delivery replaces it |
| U013 | §15.3 | 3.1 | Delivered-but-unapplied amendment cannot satisfy acceptance | `tests/v31/test_gates.py::test_amendment_delivered_but_not_applied_cannot_satisfy` | passes | KEEP | — | |
| U014 | §15.4 | 3.1 | Superseded amendment ack is rejected | `test_gates.py::test_superseded_amendment_ack_is_rejected` | passes | KEEP | — | |
| U015 | issue-77#plan-contract-amendment | 3.0 | Plan-contract amendment pauses affected scopes and wakes planner | re-point `test_amendments.py::test_matrix_plan_contract_delta_wakes_planner_and_pauses_only_affected_scopes` | broken-cite | SUPERSEDED-BY U017 | #121 | 3.1 contract amendments pause scopes and dependants (v31:144 §7) |
| U016 | issue-35#child-59 | 3.0 | PLAN DEFECT needs plan-compliance proof and contradicting evidence | `-k test_plan_defect_amendment` | broken-cite | SUPERSEDED-BY U017 | #131 | 3.1 PLAN_DEFECT is a closed class with cited evidence |
| U017 | §15.26 | 3.1 | Rolling review S1–S3, N1–N3, N11 | `tests/v31/test_rolling_review.py` | passes | KEEP | — | Whole-file cite; consider naming tests per scenario |
| U018 | issue-35#plan-amendment-exclusion | 3.0 | No live plan amendments once execution begins | `gh issue view 35` assertion | weak-proof | RETIRE | #119 | Already superseded by decision-5 |
| **C. State authority, recovery, families** | | | | | | | | |
| U019 | issue-35#child-49 | 3.0 | Run recorder persists dispatch events and findings to SQLite WAL | `-k test_review_finding_persist` | weak-proof | SUPERSEDED-BY U022 | #122 | 3.1 has one state authority in runs.db (v31:46 §2) |
| U020 | plan-t0#finding-F6-findings-fk | 3.0 | `findings.dispatch_id` references `dispatches(id)` | `-k test_findings_reference_existing_dispatch` | none | KEEP | #131 | FK not implemented; confirm 3.1 `db.py` schema |
| U021 | §15.1 | 3.1 | Duplicate submit reuses the operation | `test_gates.py::test_duplicate_submit_reuses_the_operation` | passes | KEEP | — | |
| U022 | §15.5 | 3.1 | Concurrent event/cursor writers neither lose nor duplicate | `test_state_core.py::test_concurrent_event_writers_neither_lose_nor_duplicate` | passes | KEEP | — | |
| U023 | issue-35#child-85 | 3.0 | Run takeover does a durable-state handshake and revokes the stale lease | `-k test_stale_lease_takeover` | weak-proof | MERGE-WITH U024 | #122 | Both claim crash/lease recovery |
| U024 | §15.2 | 3.1 | Crash between transition and outside work recovers once | `test_state_core.py::test_crash_after_commit_before_external_work_recovers_once` | passes | KEEP | — | |
| U025 | issue-35#decision-6 | 3.0 | One orchestrator supervises concurrent families with one focus family | re-point `test_families.py::StickyFocusMatrixTests` | broken-cite | MERGE-WITH U026 | #121 | Duplicate of sticky-focus |
| U026 | issue-77#sticky-focus | 3.0 | Family focus is sticky and inferred | `tests/v31/test_discovery_pinning.py::test_ambiguous_runs_never_select_latest_mtime` + `::test_unbound_session_never_starts_or_binds` | passes | SUPERSEDED-BY src/office/discovery.py:88,120 | #121 | Session binding is the sticky focus; inference is limited to the sole active run and ambiguity is refused (discovery.py:1-5) |
| U027 | issue-77#concurrent-families | 3.0 | Multiple families run concurrently with durable isolated state | `tests/v31/test_discovery_pinning.py::test_ambiguous_runs_never_select_latest_mtime` | passes | SUPERSEDED-BY src/office/db.py:25, src/office/discovery.py:111 | #121 | Each run is its own family (`family_id = run_id`, lifecycle.py:67); state is per run in runs.db and is re-read on resume |
| U028 | issue-77#soft-scheduling | 3.0 | Cross-family quota collisions produce advisory warnings | re-point `test_families.py::test_projected_collision_warns_for_one_family_while_the_other_stays_runnable` (3.0 only) | broken-cite | KEEP | #121 | 3.1 gap: no cross-run quota projection or collision warning in src/office |
| **D. Version identity, pinning, discovery** | | | | | | | | |
| U029 | issue-35#child-53 | 3.0 | Spokes collapse into single auto-office, keeping primitive CLI skills | `python3 scripts/check_ecosystem.py` | weak-proof | SUPERSEDED-BY v31:234 (§14) | #122 | `office-skills` → `auto-office` done in 3.1 |
| U030 | §15.14 | 3.1 | Unbound session never starts or binds | `test_discovery_pinning.py::test_unbound_session_never_starts_or_binds` | passes | KEEP | — | |
| U031 | §15.15 | 3.1 | No latest-mtime run selection | `test_discovery_pinning.py::test_ambiguous_runs_never_select_latest_mtime` | passes | KEEP | — | |
| U032 | §15.16 | 3.1 | Old-version run stays usable after new default | `test_discovery_pinning.py::test_old_version_run_stays_usable_after_new_default` | passes | KEEP | — | |
| U033 | §15.17 | 3.1 | Missing pinned runtime is a blocker | `…missing_pinned_legacy_runtime…` | passes | KEEP | — | Ellipsis cite; spell out full name |
| U034 | §15.18 | 3.1 | Packet/run version mismatch rejected | `test_state_core.py::test_packet_version_mismatch_is_rejected`, `…foreign_office_version…` | passes | KEEP | — | |
| U035 | §15.23 | 3.1 | Rollback changes future runs only | `test_discovery_pinning.py::test_rollback_changes_future_runs_only` | passes | KEEP | — | |
| U036 | §15.27 | 3.1 | Parallel tasks, shared scope, stacking, integration conflict | `test_gates.py::test_parallel_…`, `…shared_scope…`, `…stacked…`, `…integration_conflict…` | passes | KEEP | — | Ellipsis cites; spell out names |
| U037 | §15.24 | 3.1 | Legacy/raw is never a second authority | `test_compat_hooks_install.py::test_raw_…`, `…legacy_helper_cannot_write_a_31_run` | passes | KEEP | — | Removal due in 3.2.0 (v31:208 §11) |
| **E. Review, gates, verification depth** | | | | | | | | |
| U038 | plan-v2#finding-F3 | 3.0 | Independent PASS requires validated reviewer dispatch and readback | re-point `test_review.py::test_review_loop_valid_reviewer_dispatch_passes_and_labels_dispatch` | broken-cite | MERGE-WITH U040 | #121 | Same independence claim as 3.1 reviewer gate |
| U039 | issue-77#executor-local-review | 3.0 | Executor owns local implementation and adversarial review loop | re-point `test_review.py` review-loop suite | broken-cite | MERGE-WITH U040 | #121 | |
| U040 | §15.11 | 3.1 | Reviewer schema/quota failure falls back, then blocks | `test_gates.py::test_invalid_reviewer_reply_falls_back_then_blocks`, `…quota_failure…` | passes | KEEP | — | |
| U041 | §15.12 | 3.1 | Repeated finding stops after one escalation | `test_gates.py::test_repeated_finding_stops_after_one_escalation` | passes | KEEP | — | |
| U042 | issue-77#structural-finding-routing | 3.0 | Findings route to executor / planner / user by class | `-k test_structural_finding_routing` | broken-cite | KEEP | #124 | Destination not asserted anywhere |
| U043 | issue-35#decision-4 | 3.0 | Universal self-review; final integration adversary | `-k test_integration_adversary_trigger` | broken-cite | MERGE-WITH U044 | #131 | Same adversary trigger |
| U044 | issue-77#integration-adversary-trigger | 3.0 | Integration adversary spawns only for dependent/merging landings | `-k test_integration_trigger` | none | KEEP | #131 | Not implemented; check 3.1 `integration.py` |
| U045 | issue-35#child-57 | 3.0 | Universal self-verification floor; independent verifiers funded dynamically | `pytest tests/test_landings.py` | weak-proof | KEEP | #131 | Funding not asserted |
| U046 | issue-35#child-84 | 3.0 | Blast radius must be evidence-backed by traces or probes | `-k test_blast_radius` | none | KEEP | #131 | Not implemented |
| U047 | §15.6 | 3.1 | Dirty edit with unchanged HEAD invalidates a prior PASS | `test_gates.py::test_dirty_edit_with_unchanged_head_invalidates_prior_pass` | passes | KEEP | — | |
| U048 | §15.7 | 3.1 | Stale result is audit-only | `test_gates.py::test_stale_pass_is_audit_only`, `test_visual.py::test_unrelated_edit_reuses_…` | passes | KEEP | — | |
| U049 | §15.25 | 3.1 | No gate is green from skipped, unavailable, or stale | `test_gates.py::test_missing_check_command_is_never_pass`, `test_rolling_review.py::test_unavailable_plan_reviewer_blocks_dispatch` | passes | KEEP | — | |
| **F. Visual evidence** | | | | | | | | |
| U050 | §15.8 | 3.1 | Wrong visual state is an invalid comparison, blocks after one recapture | `test_visual.py::test_wrong_state_is_invalid_comparison_then_blocks_after_one_recapture` | passes | KEEP | — | |
| U051 | §15.9 | 3.1 | Broken interaction is a product failure | `test_visual.py::test_broken_interaction_is_a_product_failure_not_invalid` | passes | KEEP | — | |
| U052 | §15.10 | 3.1 | No image capability never yields visual PASS | `test_visual.py::test_route_without_image_capability_never_passes` | passes | KEEP | — | |
| U053 | §16.1–§16.5 | 3.1 | Vision probes qualify/disqualify each harness/model for visual judgment | live run 2026-09-27 (isolated homes) | weak-proof | KEEP | #126 | Live observation, not repeatable command |
| **G. Routing, trust, capability floors** | | | | | | | | |
| U054 | issue-35#child-44 | 3.0 | Preferences resolve across three YAML tiers with CLI highest | `-k test_cli_set_outranks_every_file_tier` | weak-proof | KEEP | #122 | Tier order assertion incomplete |
| U055 | issue-35#child-45 | 3.0 | Fit test selects one of six mode presets from blast radius, reversibility, size | `-k test_fit_test` | weak-proof | KEEP | #128 | Decided: fit_gear must select all six modes |
| U056 | issue-35#child-46 | 3.0 | Adapters define invocation, prompt transport, capabilities; unverified restricted | `python3 tests/test_schemas.py` | weak-proof | MERGE-WITH U060 | #122 | Whole-file cite |
| U057 | issue-35#child-56 | 3.0 | Handoffs use a traceable base envelope | `python3 tests/test_schemas.py` | weak-proof | SUPERSEDED-BY U034 | #122 | 3.1 packets are version-checked by `state.check_packet` |
| U058 | issue-35#child-72 | 3.0 | Role routing defaults (planner/reviewer seeds) | `-k test_preferred_seed_picks_first_choice_even_if_pricier` | weak-proof | KEEP | #122 | Seeds live in config; model names drift |
| U059 | issue-35#cli-seed-model-axis-separation | 3.0 | preferred_seed is a model/effort identity separate from harness slug | `-k test_disclosure_flags_unverified_invocation_slug or …unproven…` | weak-proof | KEEP | #122 | |
| U060 | issue-35#child-83 | 3.0 | Conformance workflow scaffolds adapters, runs deterministic and live checks | `-k test_conformance_workflow` | broken-cite | KEEP | #126 | 3.1 `conformance.py` covers image capability only |
| U061 | issue-77#routing-precedence | 3.0 | Six-tier routing precedence chain | `-k test_routing_precedence` | broken-cite | KEEP | #123 | |
| U062 | issue-41#cheapest-floor-quota-objective | 3.0 | Router order: exclusions, capabilities, floor, cheapest, quota | `gh issue view 41` assertion | weak-proof | KEEP | #119, #123 | Decision-record command |
| U063 | issue-41#hard-vs-advisory-floor-authority | 3.0 | Floor is a compound contract; advisory anchor cannot lower it | `gh issue view 41` assertion | weak-proof | MERGE-WITH U066 | #119, #123 | |
| U064 | issue-39#finding-hard-exclusion-and-prior-pinning | 3.0 | Hard exclusions and prior pinning in route() | `-k test_routing_candidate_triple_pinning_hard_exclusion_and_expiry` | passes | KEEP | #123 | superseded_by in route() still open |
| U065 | t2b#per-role-capability-floor | 3.0 | Role floor from config against pinned catalog, fail closed | re-point `test_scoring.py::CapabilityFloorTests` | broken-cite | KEEP | #121 | Ported to 3.1 `routing.py`/`scoring.py` |
| U066 | t2b#derived-advisory-floor | 3.0 | Advisory anchor derived from role floor, not caller | re-point `test_derived_routing.py::test_self_asserted_proven_floor_and_reward_are_ignored_for_mutable_role` | broken-cite | KEEP | #121 | |
| U067 | t2b#derived-adapter-trust-quarantine | 3.0 | Trust drops to quarantined on unresolved critical finding | re-point `tests/test_trust_conformance.py` | broken-cite | KEEP | #121 | |
| U068 | t2b#explicit-adapter-trust-act | 3.0 | Trust rises only by explicit act | re-point `test_scoring.py::TrustActWritePathTests` + `tests/test_trust_conformance.py` | broken-cite | KEEP | #121 | |
| U069 | t2b#recorded-gate-override | 3.0 | Gate override needs recorded, scoped, expiring authorization | re-point `test_derived_routing.py::test_override_without_recorded_authorization_is_a_hard_stop` | broken-cite | MERGE-WITH U071 | #121 | 3.1 `authority.py` binds approvals |
| U070 | issue-40#no-network-at-route-time | 3.0 | route() never opens a network socket | `test_schemas.py::TestRouteNoNetworkAccess` | passes | KEEP | — | |
| U071 | §16.6 | 3.1 | agy unknown `--model` slug exits 1 (drift detected) | live observation | weak-proof | KEEP | #126 | Add a fake-agent regression test |
| U072 | issue-40#dual-detection-triggers | 3.0 | Capability rediscovery and catalog refresh trigger independently | `gh issue view 40` assertion | weak-proof | KEEP | #119, #130 | |
| U073 | issue-40#refresh-outside-critical-path | 3.0 | Stale catalog refresh runs outside the critical path | `-k test_stale_catalog_refresh_is_non_blocking` | none | KEEP | #130 | Not implemented |
| U074 | issue-40#compact-detection-output | 3.0 | Raw probe output discarded; agent gets compact status | `gh issue view 40` assertion | weak-proof | KEEP | #119, #130 | |
| U075 | issue-39#catalog-expiry-and-numeric-confidence | 3.0 | Catalog triples never deleted; carry last_seen/superseded_by | `gh issue view 39` assertion | weak-proof | KEEP | #119, #123 | |
| **H. Reward, calibration, maturity, readiness (#127, #129: in scope)** | | | | | | | | |
| U076 | issue-35#child-48 | 3.0 | Per-role scoring: 2x gate credit, round-5 penalty, normalization | `pytest tests/test_scoring.py` | weak-proof | KEEP | #127 | Implementation missing |
| U077 | t2b#derived-local-reward | 3.0 | Local reward from labeled rows; unknown is None, not zero | re-point `test_scoring.py::LocalRewardTests` | broken-cite | KEEP | #121, #127 | |
| U078 | t2b#outcome-label-pipeline | 3.0 | Closeout/maintenance record and revise outcome labels with evidence | re-point `test_schemas.py::test_outcome_label_evidence_gate + test_pending_label_never_carries_evidence` | broken-cite | KEEP | #121, #127 | |
| U079 | amendment-v3#label-to-route-round-trip | 3.0 | Closeout labels change a later run's route | `test_integration.py::DerivedRoutingRoundTripTest` | passes | KEEP | #127 | |
| U080 | amendment-v3#unmeasured-is-not-measured-zero | 3.0 | Unmeasured triple ranks above measured zero | `-k test_an_unmeasured_triple_does_not_rank_as_a_measured_zero` | passes | MERGE-WITH U077 | #127 | Same None-vs-zero claim |
| U081 | issue-35#child-51 | 3.0 | Calibration/replay protocol: 20 labeled rows, 80/20 check | `pytest tests/test_calibration.py` | none | KEEP | #127 | File absent; implementation missing |
| U082 | issue-48#recurrence-window | 3.0 | Dispatch labels stay pending until scheduled recurrence check | `-k test_recurrence_window_auto_labels_at_close` | none | KEEP | #127 | Implementation missing |
| U083 | issue-48#orchestrator-scoring | 3.0 | Orchestrator PR-success reward uses recurrence-gated ground truth | `gh issue view 48` assertion | weak-proof | KEEP | #119, #127 | Implementation missing |
| U084 | issue-48#task-credit-mapping | 3.0 | Credit maps to task, falls back to reduced run-level charge | `gh issue view 48` assertion | weak-proof | KEEP | #119, #127 | Implementation missing |
| U085 | issue-42#size-class-accuracy-bar | 3.0 | Size-class estimator calibrated toward an accuracy bar | `gh issue view 42` assertion | weak-proof | KEEP | #119, #127 | Implementation missing |
| U086 | issue-55#evidence-bar-and-decay | 3.0 | Maturity needs 30 labeled runs across 3+ repos; decays | config-literal assertion | weak-proof | KEEP | #119, #127 | Config literal is not behavior |
| U087 | issue-35#child-55 | 3.0 | Continuous maturity age (0–100) gates automated evolution | `test -f references/graduation-bar.yaml` | none | KEEP | #127 | Implementation missing |
| U088 | config#exploration-limits | 3.0 | Exploration dispatches bounded by configured limits | `-k test_exploration_limits` | none | KEEP | #127 | Implementation missing |
| U089 | issue-35#child-82 | 3.0 | Repository Agent Readiness scoring feeds planning, gear, verifier funding | `-k test_readiness_scoring` | none | KEEP | #129 | Decided in scope; implementation missing |
| U090 | issue-35#child-58 | 3.0 | Advisory run budgets track tokens, cost, time, dispatches, quota | `pytest tests/test_budget.py` | weak-proof | KEEP | #122 | Whole-file cite |
| **I. Dispatch, monitoring, hooks, compaction** | | | | | | | | |
| U091 | issue-35#decision-7 | 3.0 | Packets survive handoff; compaction is conditional at phase boundaries | `-k test_pre_compact` | weak-proof | MERGE-WITH U092 | #124 | Same test as U092 |
| U092 | issue-77#compaction-boundary | 3.0 | Roles checkpoint before review and compact conditionally | `-k test_pre_compact` | weak-proof | KEEP | #124 | Conditionality not asserted |
| U093 | issue-48#role-portability | 3.0 | Every role is compactable and transferable mid-run | `gh issue view 48` assertion | weak-proof | MERGE-WITH U092 | #119, #124 | |
| U094 | issue-93#non-blocking-monitoring | 3.0 | Monitor delivers completion events without blocking | `pytest tests/test_monitor.py` | weak-proof | SUPERSEDED-BY U096 | #125 | 3.1 supervisor + durable jobs replace the monitor (v31:92 §5) |
| U095 | issue-93#agy-false-done | 3.0 | Pane reclamation requires a durable terminal event | `-k test_agy_false_done` | passes | MERGE-WITH U096 | — | |
| U096 | §15.13 | 3.1 | Every process ending is classified | `test_gates.py::test_process_endings_always_classified` | passes | KEEP | — | |
| U097 | §16.7 | 3.1 | Herdr launcher confirms start within 45s, with process fallback | live observation; `tests/v31/test_herdr_panes.py` | weak-proof | KEEP | — | Link to a named test |
| U098 | issue-93#approval-hook-withdrawn | 3.0 | PreToolUse approval hook is withdrawn | `-k test_pre_tool_use_does_not_inspect_bash` | passes | RETIRE | — | Already superseded in 3.0 |
| U099 | §16.8 | 3.1 | `office doctor` finds legacy and malformed hook configs | live observation | weak-proof | KEEP | — | Add fixture test |
| **J. Prune** | | | | | | | | |
| U100 | §15.19 | 3.1 | Prune dry run mutates nothing | `test_prune.py::test_dry_run_mutates_nothing` | passes | KEEP | — | |
| U101 | §15.20 | 3.1 | Force prune never removes resumable runs | `test_prune.py::test_force_prunes_terminal_only_and_keeps_tombstone` | passes | MERGE-WITH U103 | — | Same test as U103 |
| U102 | §15.21 | 3.1 | Force prune re-checks eligibility at deletion | `test_prune.py::test_force_rechecks_eligibility_at_deletion` | passes | KEEP | — | |
| U103 | §15.22 | 3.1 | Tombstone kept, artifacts removed | `test_prune.py::test_force_prunes_terminal_only_and_keeps_tombstone` | passes | KEEP | — | |
| **K. Self-improvement and reporting** | | | | | | | | |
| U104 | issue-35#child-50 | 3.0 | Dream compiler extracts patterns from runs.db into reference docs | `pytest tests/test_propose.py` | weak-proof | KEEP | #127 | Whole-file cite |
| U105 | issue-35#child-52 | 3.0 | Self-heal PR appends dreamt lines citing run_id | `-k AppendTest` | passes | KEEP | — | |
| U106 | plan-v2#finding-F1 | 3.0 | End-to-end self-improvement cycle | evidence doc + privacy-lint + `test_propose.py` | passes | KEEP | — | |
| U107 | t5s#graduation-bar | 3.0 | Committed graduation bar for standing proposals | `test -f` + `-k 'privacy or replaying or lineage'` | passes | KEEP | — | |
| U108 | issue-35#child-54 | 3.0 | Context load size constrained by line budgets | `python3 scripts/check_ecosystem.py` | passes | KEEP | — | |
| U109 | issue-35#child-54-pr-report | 3.0 | Per-PR before/after size and token report | `pytest tests/test_pr_report.py` | passes | KEEP | — | |
| **L. Portability, process, CI** | | | | | | | | |
| U110 | issue-35#decision-8 | 3.0 | Claude, Codex, agy each demonstrate the orchestrator lifecycle | evidence doc + privacy-lint | passes | MERGE-WITH U111 | — | Duplicate of U111 |
| U111 | issue-93#portability-demonstration | 3.0 | Portability demonstration in a scratch repo | evidence doc + privacy-lint | passes | KEEP | — | Re-run for 3.1 is not recorded |
| U112 | §16.9 | 3.1 | Live run through codex orchestrator lands with hidden tests passing | live observation (eval pilot F01) | weak-proof | MERGE-WITH U111 | — | Only codex exercised for 3.1 |
| U113 | §17 | 3.1 | Prospective matched-fixture evaluation (spec §24, ≥60 jobs) | `eval/v31/` | none | KEEP | #171 | Not completed; quota-bound |
| U114 | issue-35#decision-2 | 3.0 | Implementation resolves conflicts, reuses components, lands | `git merge-base --is-ancestor 5d7a450 origin/main` (rule a; exits 0 on 2026-09-27) | passes | KEEP | #118 | Not squashed; ancestor of main. Landing via PR #99 also confirmed by rule b |
| U115 | plan-v2#finding-F10 | 3.0 | Final merge asserts reviewed head SHA, tree hash, diff equality | `git merge-base --is-ancestor 854b04a origin/main` (rule a) + `gh pr view 99 --json state,mergeCommit,headRefOid` (MERGED, merge ff303e7, head 854b04a = `ff303e7^2`) | attested | KEEP | #118 | Reviewed head SHA is proven; integration tree hash and diff equality attested at ff303e7 |
| U116 | plan-v2#ci-billing-lock | 3.0 | Remote CI `validate.yml` runs on GitHub | `gh run list --workflow=validate.yml` | none | SUPERSEDED-BY #120 decision | #120 | CI stays empty; validate.yml to be removed |
| U117 | issue-40#validate-yml-runs-clean-locally | 3.0 | Every `validate.yml` command passes locally | check_ecosystem + full pytest | passes | SUPERSEDED-BY #120 decision | #120 | Workflow file will be removed; keep an equivalent local gate if wanted |

## Families in 3.1

- **Grouping (covered).** Every 3.1 run is its own family: `family_id = run_id`
  (`src/office/lifecycle.py:67`, `src/office/db.py:25`). Multiple runs coexist in `runs.db` and are
  listed by `office list` (`src/office/cli.py:249`).
- **Sticky focus (covered, narrower).** Focus is a session binding set by `office resume <run>`
  (`src/office/discovery.py:88`). Resolution order is flag, env, binding, sole active run
  (`discovery.py:120`). 3.0 inferred focus from context; 3.1 refuses ambiguity instead.
- **Restart reconstruction (covered).** State is re-read from `runs.db` on each call; there is no
  separate family file to rebuild.
- **Legacy code.** 3.1 calls `scripts/office_runtime.py` only to serve pinned 3.0 runs
  (`src/office/compat.py:136`); `family-*` commands map to `office list`/`resume` (`compat.py:35`).
- **Gap.** No cross-run quota collision warning (U028).

## Counts

Rows: 117 (84 from 3.0, 33 from 3.1). §16 rows 1–5 are one row (U053).

| Disposition | Count |
|---|---|
| KEEP | 84 |
| SUPERSEDED-BY | 15 |
| MERGE-WITH | 15 |
| RETIRE | 3 |
| UNDECIDED | 0 |
