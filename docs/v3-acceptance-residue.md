# Auto Office v3 — Acceptance Residue

Companion to `docs/v3-acceptance.md`. That document is the matrix; this one enumerates
every row on it that is not yet green, says why each fails, and sorts the whole set into
the stages that clear it.

Baseline: `main` at `cd72107`. 84 rows.

| Status | Count |
|---|---|
| `verified` | 12 |
| `conflict` | 57 |
| `missing` | 11 |
| `superseded` | 3 |
| `external-blocker` | 1 |

The 68 `conflict` and `missing` rows are enumerated below. Every row appears in exactly
one stage; the assignment is checked mechanically, so this file and the matrix cannot
silently drift apart in membership.

## Why rows fail

Four distinct defects account for all 68. They are not equally expensive, and separating
them is the point of this document.

1. **Stale pointer.** The cited test was renamed; the behavior is guarded elsewhere,
   rejecting cases included. Cheapest class. 17 rows.
2. **Narrow assertion.** The command passes, and passes for a reason other than the
   row's claim. The behavior is mostly real, the assertion is narrower than the
   sentence. 20 rows.
3. **Wrong evidence source.** The command reads the GitHub issue record or a config
   literal rather than the repository. It establishes what was *decided*, not what was
   *built*, and would pass identically against an empty implementation. 10 rows, spread
   across stages.
4. **Absent behavior.** `git grep` finds no implementation. 21 rows, concentrated in the
   reward/calibration subsystem.

A cross-cutting rule follows from defect 3 and should be written into the matrix
preamble before any row is re-pointed: **no row may cite `gh issue view` or a bare
config-literal read as its verification command.** A declaration is not an enforcement.

## Charted winners

Resolved this session. No ticket needed.

- **Renamed tests win over the matrix's pointers.** 17 rows cite a test function that
  does not exist while the behavior sits under a new name in a matrix class. All 16
  distinct replacements were confirmed present in the tree. The matrix is stale, the
  tests are correct. Listed per-row under W5.
- **`issue-35#child-53` is materially satisfied.** Spoke retirement is complete: `skills/`
  holds no `codex-office` or `agy-office`, and the only surviving mention is a comment in
  `scripts/agy-usage.py:28`. The row is `conflict` only because `check_ecosystem.py`
  asserts counts and line budgets, not retirement. The claim is true; the command is wrong.

## Stages

Stage 0 settles rules that decide many rows at once. Nothing downstream should be
re-pointed before it lands. Stage 1 is unblocked now. Stage 2 is assertion work on
behavior that already exists. Stage 3 is where absent features and scope questions live.

| Stage | Ticket | Type | Rows | Blocked by |
|---|---|---|---|---|
| 0 | W1 instruction-document sufficiency | grilling | 7 | — |
| 0 | W2 historical-process rows | grilling | 2 | — |
| 0 | W3 ban decision-record commands | task | rule | — |
| 0 | W4 CI billing lock vs "keep CI empty" | grilling | 1 | — |
| 1 | W5 re-point stale test pointers | task | 17 | — |
| 2 | W6 T0 runtime and config | task | 10 | W1, W3 |
| 2 | W7 T2 routing and precedence | task | 4 | W3 |
| 2 | W8 T4 review, landing, compaction | task | 4 | W3 |
| 2 | W9 non-blocking monitoring | task | 1 | — |
| 2 | W10 adapter conformance live checks | task | 1 | — |
| 3 | W11 reward/calibration/maturity scope | grilling | 11 | — |
| 3 | W12 six mode presets or three | grilling | 1 | — |
| 3 | W13 repository agent readiness | grilling | 1 | — |
| 3 | W14 catalog freshness triggers | task | 3 | W11 |
| 3 | W15 verification depth | task | 6 | W1 |

W3 covers the ten defect-3 rows in place rather than owning them; each is listed under
the stage that owns its substance. W4 governs `plan-v2#ci-billing-lock`, which is
`external-blocker` rather than `conflict` and so is not among the 68.

---

## Stage 0 · W1 · grilling — Does an instruction-document assertion verify an instruction-layer row?

**Rows: 7**

Every row here is verified by `tests/test_v3_instruction_contract.py`, which regex-matches prose in `protocol/` and `skills/**/SKILL.md`. The behavior is real in the instruction layer, but the test proves the document *says* it, not that a run *does* it. Either that is acceptable evidence for a row whose subject is an instruction, or it is not, and the answer decides seven rows at once. Nothing downstream should be re-pointed until it is settled.

### `issue-35#decision-1` — T1 — `conflict`

**Claim.** The orchestrator captures provisional intent while the dedicated planner investigates, interacts directly with the user, challenges assumptions, and freezes authoritative requirements only upon user confirmation.

**Cited command.** `python3 -m pytest tests/test_v3_instruction_contract.py -q`

**Why it does not hold.** Passes for a reason other than the claim. The 17 tests in `tests/test_v3_instruction_contract.py` assert regex properties of instruction DOCUMENTS: that no live planner-user prohibition survives, that the Planner section says it talks directly to the user, and similar. None asserts that the orchestrator captures provisional intent, that assumptions are challenged, or that requirements freeze only on user confirmation. The command would still exit 0 with all three absent.

### `issue-35#decision-3` — T1 — `conflict`

**Claim.** The executor owns code-review dispositions and evidence-backed finding refutations, with exceptional upline consultation to the orchestrator or user available only when disagreement cannot be resolved locally.

**Cited command.** `python3 -m pytest tests/test_v3_instruction_contract.py -q`

**Why it does not hold.** The only relevant test, `test_executor_owns_disposition_with_exceptional_consultation`, does `assertRegex(text, "exceptional")` and `assertRegex(text, "not a mandatory approval chain\

### `issue-77#provisional-kickoff` — T1 — `conflict`

**Claim.** The orchestrator kickoff gathers only initial intent and launches the planner with a serialized provisional intent packet without attempting full requirements freezing.

**Cited command.** `python3 -m pytest tests/test_v3_instruction_contract.py -k test_provisional_kickoff -q`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. The behaviour is specified in the instruction layer (`protocol/lifecycle.md:5` states intent capture is two-phase and the orchestrator captures only provisional intent at kickoff; `skills/auto-planning/SKILL.md:8` receives it). No test pins that text, so it can be edited away silently.

### `issue-93#structured-intake-fallback` — T1 — `conflict`

**Claim.** Intake supports batched plain-text questioning when interactive structured questions (ask_question) are unavailable, preserving the twelve-item intake floor.

**Cited command.** `python3 -m pytest tests/test_v3_instruction_contract.py -k test_intake_fallback -q`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. The behaviour is specified in the instruction layer: `skills/auto-intake/SKILL.md:8` states "Use a structured question tool when the harness has one; batched plain text otherwise", and line 12 pins the twelve-item floor in one or two batched rounds. No test pins either sentence.

### `issue-77#orchestrator-distribution` — T1 — `conflict`

**Claim.** The orchestrator distributes structured executor packets according to the planner's dependency graph without executing a second technical plan verification gate.

**Cited command.** `python3 -m pytest tests/test_v3_instruction_contract.py -k test_orchestrator_distribution -q`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. No test covers this claim under any name. In particular nothing asserts the ABSENCE of a second technical plan-verification gate, which is the falsifiable half of the row.

### `issue-77#interactive-planner` — T1 — `conflict`

**Claim.** The dedicated planner conducts repo reconnaissance, engages in direct user interview, reshapes the problem statement if needed, self-reviews, and runs a plan adversary before freezing requirements.

**Cited command.** `python3 -m pytest tests/test_v3_instruction_contract.py -k test_interactive_planner -q`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Partially covered elsewhere: `tests/test_v3_instruction_contract.py::test_planner_role_affirmatively_talks_to_the_user` pins the direct-interview clause. Repo reconnaissance, problem reshaping, planner self-review and the plan adversary before freeze have no assertion.

### `issue-93#spoke-loading` — T1 — `conflict`

**Claim.** Spoke loading operates portably across Claude Code, Codex, and agy with verified mark-spoke receipts and direct file reading fallback.

**Cited command.** `python3 -m pytest tests/test_v3_instruction_contract.py -k test_spoke_loading -q`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Partially covered elsewhere: `scripts/office_runtime.py:914 cmd_mark_spoke` implements receipts and `tests/test_integration.py:478` exercises `mark-spoke` for one spoke. Nothing covers portability across Claude Code, Codex and agy, and nothing covers the direct-file-reading fallback.

---

## Stage 0 · W2 · grilling — Can a historical-process row be machine-checked at all?

**Rows: 2**

Both rows assert something about a merge that already happened, and both cite a display command with no exit condition. `git diff --stat <sha>` and `git log -1 --stat` cannot fail while the commit exists. Three exits: write a real assertion over git history, convert the row to an attested (non-machine) record, or rule it past the destination.

### `issue-35#decision-2` — T0–T8 — `conflict`

**Claim.** Implementation resolves specification and instruction conflicts, reuses working components, and lands auto-office-v3 into main via PR #99 rather than terminating as a spec-only finish.

**Cited command.** `git diff --stat 5d7a450`

**Why it does not hold.** `git diff --stat <sha>` has no assertion and cannot fail while the commit exists. It substantiates only that the diff is large and touches `scripts/` and `tests/` (so not spec-only). It cannot show that specification/instruction conflicts were resolved, that working components were reused, or that the branch landed via PR #99. The PR-99 landing is separately true (`git log` head is `ff303e7 Merge pull request #99 from Hikari9/auto-office-v3`) but that is not what this command checked.

### `plan-v2#finding-F10` — T8 — `conflict`

**Claim.** Final merge verification asserts reviewed PR head SHA, integration tree hash, and exact diff equality between pre-merge main and post-merge main rather than mere containment.

**Cited command.** `git log -1 --stat`

**Why it does not hold.** `git log -1 --stat` is a display command with no exit condition tied to the claim; it cannot fail. It asserts nothing about the reviewed PR head SHA, nothing about an integration tree hash, and nothing about exact diff equality between pre-merge and post-merge main as opposed to mere containment. This is the clearest instance in the matrix of a gate satisfiable by any evidence at all.

---

## Stage 1 · W5 · task — Re-point 17 rows whose cited test was renamed (winner already charted)

**Rows: 17**

No decision outstanding. Each row cites a test function that does not exist, while the behavior it claims is guarded elsewhere under a different name, rejecting cases included. The tests were reorganised into matrix classes and the matrix's pointers went stale. **Winner: the renamed test.** Every replacement named below was confirmed present in the tree on `cd72107`. Work is: correct the Verification Command, re-run, record evidence, flip to `verified`.

### `issue-35#decision-5` — T2 — `conflict`

**Claim.** The runtime independently versions requirements_version, plan_version, and routing_version; routing-only changes do not wake the planner; running workers finish atomic units unless replaced.

**Cited command.** `python3 -m pytest tests/test_amendments.py -k test_amendment_matrix -q`

**Re-point to.** `tests/test_amendments.py::AmendmentTransitionMatrixTests`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. The claim is nonetheless covered under different names: `tests/test_amendments.py::AmendmentTransitionMatrixTests` holds five matrix tests including `test_matrix_routing_only_delta_does_not_wake_planner_and_touches_only_routing_version`, `test_matrix_running_dispatch_completes_its_atomic_unit_unless_explicitly_replaced` and the rejecting case `test_matrix_bumping_every_version_on_every_delta_is_rejected`. Correcting the command to `-k AmendmentTransitionMatrixTests` would likely make this row verifiable.

### `issue-77#routing-amendment` — T2 — `conflict`

**Claim.** Routing-only amendments increment routing_version, update dispatch targets immediately for pending work and at atomic boundaries for running workers, without waking the planner.

**Cited command.** `python3 -m pytest tests/test_amendments.py -k test_routing_delta -q`

**Re-point to.** `tests/test_amendments.py::RoutingOnlyPreservesApprovalTests (+ test_matrix_running_dispatch_completes_its_atomic_unit_unless_explicitly_replaced)`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_amendments.py::RoutingOnlyPreservesApprovalTests` (four tests: versions untouched, approval intact, event recorded, planner not woken). The pending-versus-running dispatch-target update timing is covered by `test_matrix_running_dispatch_completes_its_atomic_unit_unless_explicitly_replaced`.

### `issue-77#requirements-amendment-fits-plan` — T2 — `conflict`

**Claim.** Requirements amendments fitting existing architecture and milestones increment requirements_version and notify affected executors without waking the planner.

**Cited command.** `python3 -m pytest tests/test_amendments.py -k test_requirements_delta -q`

**Re-point to.** `tests/test_amendments.py::test_matrix_requirements_delta_still_fitting_plan_does_not_wake_planner — notify-affected-executors clause still unasserted`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under a different name by `tests/test_amendments.py::test_matrix_requirements_delta_still_fitting_plan_does_not_wake_planner`. The notify-affected-executors clause has no assertion.

### `issue-77#plan-contract-amendment` — T2 — `conflict`

**Claim.** Plan-contract amendments altering architecture, interfaces, or milestones pause affected scopes, wake planner for interactive delta-planning, and increment plan_version.

**Cited command.** `python3 -m pytest tests/test_amendments.py -k test_plan_contract_delta -q`

**Re-point to.** `tests/test_amendments.py::test_matrix_plan_contract_delta_wakes_planner_and_pauses_only_affected_scopes (+ test_plan_contract_amendment_pauses_only_its_affected_scopes, test_invalidate_packets_only_touches_packets_in_affected_scope)`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_amendments.py::test_matrix_plan_contract_delta_wakes_planner_and_pauses_only_affected_scopes`, `test_plan_contract_amendment_pauses_only_its_affected_scopes` and `test_invalidate_packets_only_touches_packets_in_affected_scope`.

### `issue-35#decision-6` — T2 — `conflict`

**Claim.** One orchestrator supervises concurrent independent families using durable family state with exactly one family holding conversational focus, soft collision warnings, and defined routing precedence.

**Cited command.** `python3 -m pytest tests/test_families.py -k test_sticky_focus -q`

**Re-point to.** `tests/test_families.py::StickyFocusMatrixTests`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_families.py::StickyFocusMatrixTests` (six tests, including the rejecting cases `test_ambiguous_named_command_mutates_neither_family_nor_focus` and `test_projected_collision_warns_for_one_family_while_the_other_stays_runnable`). The durable-state and routing-precedence clauses are covered by `RestartReconstructionTests` and `FamilyConfigTierPrecedenceTests` in the same file.

### `issue-77#sticky-focus` — T2 — `conflict`

**Claim.** Conversational family focus is sticky and inferred; unqualified commands apply to the focus family, naming another switches focus, and ambiguous inference mutates nothing.

**Cited command.** `python3 -m pytest tests/test_families.py -k test_sticky_focus -q`

**Re-point to.** `tests/test_families.py::StickyFocusMatrixTests + FocusAmbiguityTests`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Identical situation to `issue-35#decision-6`: covered by `tests/test_families.py::StickyFocusMatrixTests` and `FocusAmbiguityTests`, including the mutates-nothing rejecting cases.

### `issue-77#concurrent-families` — T2 — `conflict`

**Claim.** A single orchestrator instance supervises multiple independent planner/executor families concurrently using durable family registry state without retaining child transcripts.

**Cited command.** `python3 -m pytest tests/test_families.py -k test_concurrent_isolation -q`

**Re-point to.** `tests/test_families.py::RestartReconstructionTests`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_families.py::RestartReconstructionTests` (registry reconstruction from durable records, and from a corrupted registry file) and `tests/test_monitor.py::test_concurrent_family_isolation`. The no-retained-child-transcripts clause has no assertion.

### `issue-77#soft-scheduling` — T2 — `conflict`

**Claim.** The orchestrator projects global quota and resource collisions across active families and emits advisory warnings without blocking independent ready dispatches.

**Cited command.** `python3 -m pytest tests/test_families.py -k test_soft_collision_warning -q`

**Re-point to.** `tests/test_families.py::test_projected_collision_warns_for_one_family_while_the_other_stays_runnable (+ test_unknown_quota_is_... rejecting case)`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_families.py::test_projected_collision_warns_for_one_family_while_the_other_stays_runnable` and the rejecting case `test_unknown_quota_is_neither_a_collision_nor_silently_safe`.

### `t2b#derived-local-reward` — T2B — `conflict`

**Claim.** Local reward is computed from labeled outcome rows in runs.db for the exact triple, representing unknown/unlabeled candidates as None rather than neutral zero.

**Cited command.** `python3 -m pytest tests/test_scoring.py -k test_local_reward -q`

**Re-point to.** `tests/test_scoring.py::LocalRewardTests`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_scoring.py::LocalRewardTests`, including `test_no_dispatches_is_unknown_not_zero`, `test_only_pending_label_is_unknown` and `test_unknown_reward_does_not_rank_as_measured_zero`.

### `t2b#per-role-capability-floor` — T2B — `conflict`

**Claim.** Per-role capability floor is derived from config roles.<role>.floor against pinned catalog row, failing closed with named missing field if required attributes are absent.

**Cited command.** `python3 -m pytest tests/test_scoring.py -k test_capability_floor -q`

**Re-point to.** `tests/test_scoring.py::CapabilityFloorTests (nine tests)`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_scoring.py::CapabilityFloorTests` (nine tests), including the two fail-closed-naming-the-field rejecting cases `test_missing_effort_field_fails_closed_and_names_field` and `test_missing_benchmark_index_fails_closed_and_names_field`.

### `t2b#explicit-adapter-trust-act` — T2B — `conflict`

**Claim.** Adapter trust rises to `valid-unverified` from `quarantined`, or to `proven` from any state, only via an explicit, attributed `record_trust_act` write (amendment v5); it is never inferred or backfilled from dispatch counts, labels, or validation rows.

**Cited command.** `python3 -m pytest tests/test_schemas.py -k test_explicit_trust_act -q`

**Re-point to.** `tests/test_scoring.py::TrustActWritePathTests (eleven tests) + tests/test_trust_conformance.py`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_scoring.py::TrustActWritePathTests` (eleven tests, including reserved-actor and empty-triple rejecting cases) and `tests/test_trust_conformance.py::test_explicit_trust_act_raises_a_clean_triple_to_proven` plus `test_explicit_trust_act_cannot_override_a_standing_quarantine`. The row Evidence also claimed the implementation was pending `scripts/office_trust.py`; that file does not exist and the behaviour lives in `scripts/office_scoring.py` instead.

### `t2b#derived-adapter-trust-quarantine` — T2B — `conflict`

**Claim.** Adapter trust state moves to `quarantined` automatically, derived from an unresolved adapter-attributed critical failure in runs.db; no query may derive `proven` or clear a standing `quarantined` result (amendment v5).

**Cited command.** `python3 -m pytest tests/test_schemas.py -k TestTrustQuery -q`

**Re-point to.** `tests/test_trust_conformance.py`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. No class of that name exists anywhere in the repository. The claim is nonetheless strongly covered in a file the row never cites: `tests/test_trust_conformance.py` holds `test_qualifying_failure_quarantines_with_no_human_action`, `test_five_self_reported_labels_two_shapes_never_produce_proven`, `test_appended_benign_label_cannot_retract_a_failure` and `test_material_post_merge_defect_also_latches`. Correcting the command to `python3 -m pytest tests/test_trust_conformance.py -q` would make this row verifiable.

### `t2b#derived-advisory-floor` — T2B — `conflict`

**Claim.** Advisory quality anchor evaluates candidate against role floor and preferred_seed criteria rather than caller-supplied advisory_pass boolean.

**Cited command.** `python3 -m pytest tests/test_scoring.py -k test_advisory_floor -q`

**Re-point to.** `tests/test_derived_routing.py::test_self_asserted_proven_floor_and_reward_are_ignored_for_mutable_role`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_derived_routing.py::test_self_asserted_proven_floor_and_reward_are_ignored_for_mutable_role` and `test_asserting_vs_omitting_the_four_values_routes_identically`, which are exactly the caller-supplied-boolean-is-ignored assertion the row wants.

### `t2b#recorded-gate-override` — T2B — `conflict`

**Claim.** An explicit gate override requires a recorded user authorization record with expiry and scope, failing closed if authorization is absent or unverified.

**Cited command.** `python3 -m pytest tests/test_scoring.py -k test_recorded_override -q`

**Re-point to.** `tests/test_derived_routing.py::test_override_without_recorded_authorization_is_a_hard_stop`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names in a file the row never cites: `tests/test_derived_routing.py` holds `test_override_without_recorded_authorization_is_a_hard_stop`, `test_override_not_authorized_by_the_user_is_rejected`, `test_expired_override_is_rejected` and `test_recorded_override_bypasses_trust_gate_and_is_disclosed`. Correcting the command to `python3 -m pytest tests/test_derived_routing.py -k override -q` would make this row verifiable.

### `t2b#outcome-label-pipeline` — T2B — `conflict`

**Claim.** Closeout and maintenance record and revise outcome labels with verified evidence in runs.db, leaving dispatches lacking evidence unlabeled.

**Cited command.** `python3 -m pytest tests/test_scoring.py -k test_outcome_labels -q`

**Re-point to.** `tests/test_schemas.py::test_outcome_label_evidence_gate + test_pending_label_never_carries_evidence`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Covered under different names by `tests/test_schemas.py::test_outcome_label_evidence_gate` and `test_pending_label_never_carries_evidence`, and by `tests/test_review.py::test_review_loop_valid_reviewer_dispatch_passes_and_labels_dispatch` plus `test_a_skipped_gate_records_no_validation_row_and_no_evidence` for the leave-unlabeled clause. The revise-a-label-later clause has no assertion.

### `plan-v2#finding-F3` — T4 — `conflict`

**Claim.** Recorded independent-review PASS requires validated reviewer dispatch and readback bound to distinct producer/reviewer identity, tree SHA, all 3 versions, review scope, and non-empty evidence.

**Cited command.** `python3 -m pytest tests/test_review.py -k test_positive_provenance -q`

**Re-point to.** `tests/test_review.py::test_review_loop_valid_reviewer_dispatch_passes_and_labels_dispatch (+5 rejecting cases)`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Strongly covered under a different name: `tests/test_review.py::test_review_loop_valid_reviewer_dispatch_passes_and_labels_dispatch` is the positive path, guarded by five rejecting cases (unset source, env-var ignored, reviewer-identity mismatch, stale tree SHA, version mismatch, scope mismatch). This row is the best candidate in the matrix for becoming `verified` by correcting the command alone.

### `issue-77#executor-local-review` — T4 — `conflict`

**Claim.** Each executor owns its local implementation and adversarial code-review loop, deciding dispositions and refuting findings with evidence.

**Cited command.** `python3 -m pytest tests/test_review.py -k test_executor_local_review -q`

**Re-point to.** `tests/test_review.py review-loop suite (incl. test_review_loop_unset_review_source_is_unavailable)`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Substantially covered under different names: `tests/test_review.py` holds the full review-loop suite, including the rejecting cases `test_review_loop_unset_review_source_is_unavailable`, `test_review_loop_review_status_env_var_is_ignored` and `test_review_loop_self_approval_rejected`. Disposition ownership and evidence-backed refutation are asserted only as document text in `tests/test_v3_instruction_contract.py`.

---

## Stage 2 · W6 · task — T0 runtime and config rows: write the missing assertion

**Rows: 10**

The cited command passes, and passes for a reason other than the row's claim. The behavior is mostly real; the assertion covering it is narrower than the sentence. Work is writing the assertion, not building the feature. Blocked by W3 where the row cites a decision-record command.

### `issue-35#child-44` — T0 — `conflict`

**Claim.** User preferences resolve across a three-tier YAML hierarchy with precedence auto-office/config.default.yaml -> .auto-office/config.yaml -> ~/.claude/auto-office/config.yaml, and invalid keys fail soft.

**Cited command.** `python3 -m pytest tests/test_runtime.py -k test_cli_set_outranks_every_file_tier -q`

**Why it does not hold.** Confirms the prior F5 note by running it. The selected test covers CLI-versus-file precedence only. It does not exercise the three named YAML tiers in order, and it asserts nothing about invalid keys failing soft. `tests/test_families.py::test_each_tier_outranks_the_one_below_it` covers tiering for families but is not the cited command and is not the same hierarchy.

### `issue-35#child-46` — T0 — `conflict`

**Claim.** Harness adapters define invocation parameters, prompt transport, and capabilities, with unverified adapters remaining valid-unverified without mutable authority.

**Cited command.** `python3 tests/test_schemas.py`

**Why it does not hold.** Confirms the prior F5 note. `python3 tests/test_schemas.py` validates JSON documents against JSON Schemas. Nothing in it exercises adapter authority: no test constructs an unverified adapter and asserts it stays valid-unverified, and no test asserts an unverified adapter cannot mutate authority. A schema-shape pass is satisfiable by a correctly shaped adapter with entirely wrong authority behaviour.

### `issue-35#child-49` — T0 — `conflict`

**Claim.** Run recorder persists dispatch start/end events and findings into a SQLite WAL database via a fire-and-forget stdlib script without reading row content back.

**Cited command.** `python3 -m pytest tests/test_review.py -k test_review_finding_persist -q`

**Why it does not hold.** Confirms the prior F6 note by running it. The test asserts exit 0, a non-empty finding id on stdout, and that `.office/findings/<id>.json` exists. It never opens `runs.db`, so it cannot show a dispatch start/end event or a findings row was persisted, and the claim that the recorder is fire-and-forget without reading rows back is untested in either direction.

### `issue-35#child-53` — T0 — `conflict`

**Claim.** Spoke retirement collapses codex-office and agy-office into single auto-office, preserving primitive CLI skills and migrating or retiring all protected evals.

**Cited command.** `python3 scripts/check_ecosystem.py`

**Why it does not hold.** Confirms the prior F16 note. The retirement is in fact complete (`ls skills/` shows no `codex-office` or `agy-office`; the only remaining mention is a comment in `scripts/agy-usage.py:28`), but that was established by my inspection, not by the row command. `check_ecosystem.py` asserts counts and line budgets; it would print the same PASS with both retired spokes still present and with a migrated eval that tests nothing.

### `issue-35#child-72` — T0 — `conflict`

**Claim.** Role routing defaults select Opus Medium with Astra Low fallback for planner, Luna XHigh for reviewer, task-shape/evidence for executor, and user-selected for orchestrator.

**Cited command.** `python3 -m pytest tests/test_runtime.py -k test_preferred_seed_picks_first_choice_even_if_pricier -q`

**Why it does not hold.** Confirms the prior F16 note. One scenario (first choice wins despite price) cannot establish the four named per-role defaults. The command passes with planner, reviewer, executor and orchestrator defaults all set to something else.

### `issue-35#child-81` — T0 — `conflict`

**Claim.** Five task-shape playbooks (Change, Restructure, Investigate, Prototype, Visual) specialize procedure and evidence requirements without weakening global verification gates.

**Cited command.** `python3 -m pytest tests/test_integration.py -k test_start_cli_can_select_full_fit_path -q`

**Why it does not hold.** Confirms the prior F5 note. One Change/full-fit path is exercised; Restructure, Investigate, Prototype and Visual are not. The row also claims the playbooks specialise evidence requirements without weakening global gates, and no test compares a playbook gate set against the global floor at all.

### `issue-35#child-85` — T0 — `conflict`

**Claim.** Run takeover performs a durable-state handshake across registry, packets, git, and validation state, revoking previous holder authority upon transfer.

**Cited command.** `python3 -m pytest tests/test_integration.py -k test_stale_lease_takeover -q`

**Why it does not hold.** Confirms the prior F5 note. Lease expiry and acquisition are covered. The claim is a four-part handshake across registry, packets, git and validation state plus revocation of the previous holder authority; none of those four is asserted, and no test constructs a takeover that must be refused because one part is inconsistent.

### `issue-35#cli-seed-model-axis-separation` — T0 — `conflict`

**Claim.** A role's `preferred_seed` entry is a spec-level model/effort (optionally harness) identity distinct from the harness-specific `invocation_model_id` slug actually dispatched, so a config seed without a harness field matches that model/effort on any harness, and an unverified/undocumented invocation slug is disclosed rather than silently treated as resolved.

**Cited command.** `python3 -m pytest tests/test_runtime.py -k "test_disclosure_flags_unverified_invocation_slug or test_disclosure_flags_unproven_invocation_slug" -q`

**Why it does not hold.** Confirms the prior F16 note. Both selected tests are about disclosing an unverified/unproven invocation slug. The other half of the claim, that a `preferred_seed` entry without a `harness` field matches that model/effort on ANY harness, has no test: nothing routes the same seed across two harnesses and asserts both match.

### `issue-35#child-56` — T0 — `conflict`

**Claim.** Role handoffs use a machine-generated traceable base envelope wrapping role-specific payload with mandatory task scope, blast radius, mutations, and validation commands.

**Cited command.** `python3 tests/test_schemas.py`

**Why it does not hold.** Confirms the prior F16 note with a sharper reason. `tests/test_schemas.py` never calls `create_execution_packet`; the only test that mentions it, `test_create_execution_packet_names_session_id_and_packet_version`, regex-matches the function SIGNATURE out of `docs/v3-runtime-contracts.md` prose. Generation is exercised only by `tests/test_amendments.py:160`, which is not the cited command. Everything else is static-fixture schema validation.

### `issue-35#child-58` — T0 — `conflict`

**Claim.** Advisory run budgets dynamically track tokens, cost, wall-clock time, dispatches, and quota refresh hours without lowering capability floors.

**Cited command.** `python3 -m pytest tests/test_budget.py -q`

**Why it does not hold.** Confirms the prior F5 note. All three tests in `tests/test_budget.py` call `check_skill_budgets` on temporary `SKILL.md` files, which is child-54 line-budget behaviour, not run budgets. No token, cost, wall-clock, dispatch-count or quota-refresh tracking is exercised, and the file imports nothing that would.

---

## Stage 2 · W7 · task — T2 routing and precedence rows

**Rows: 4**

Two rows are substantially implemented but verified by a decision-record command (W3). One asserts a six-tier precedence chain where only the family tier is walked. One has two of three clauses genuinely proven and a third contradicted: `route()` never reads `superseded_by`, so the newer triple does not demonstrably govern.

### `issue-77#routing-precedence` — T2 — `conflict`

**Claim.** Live routing resolves through six tiers: explicit dispatch > family/project > repo config > session policy > user-global > plugin default.

**Cited command.** `python3 -m pytest tests/test_families.py -k test_routing_precedence -q`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Partially covered under a different name by `tests/test_families.py::FamilyConfigTierPrecedenceTests::test_each_tier_outranks_the_one_below_it`. That test walks the family config tiers; no test enumerates all six named tiers in the row, so the explicit-dispatch and plugin-default ends are unproven.

### `issue-41#cheapest-floor-quota-objective` — T2B — `conflict`

**Claim.** The router evaluates candidates in strict order — (1) hard exclusions, (2) required role capabilities, (3) role benchmark anchor(s), (4) quota/availability, (5) cheapest qualifying candidate — and cost never lowers the floor; an advisory-role override is recorded for later calibration.

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','41','--json','comments','--jq','.comments[].body']); assert b'then (5) cheapest qualifying candidate' in out"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. The behaviour is in fact substantially implemented and tested elsewhere: `scripts/office_routing.py:168 route()` applies numbered stages 1-5 and `tests/test_derived_routing.py` constructs rejecting cases for stages 3 and 4. The row cites none of that. Replacing the command with `python3 -m pytest tests/test_derived_routing.py -q` would make this row checkable.

### `issue-41#hard-vs-advisory-floor-authority` — T2B — `conflict`

**Claim.** The role capability floor is a compound contract (benchmark anchor, required capabilities, hard exclusions) that is hard for executor/worker selection and only advisory (cost/mode may override, with the override recorded) for orchestrator/planner/reviewer selection; a high score never overrides a known-unsafe harness/model behavior.

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','41','--json','comments','--jq','.comments[].body']); assert b'Numeric routing floors remain hard for executor/worker selection and advisory for orchestrator/planner/reviewer selection' in out"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. The hard/advisory split is implemented (`scripts/office_routing.py`, `compute_capability_floor` in `scripts/office_scoring.py`) and covered by `tests/test_scoring.py::CapabilityFloorTests` and `tests/test_derived_routing.py`, none of which this command runs. The clause that a high score never overrides a known-unsafe behaviour is covered by `tests/test_trust_conformance.py::test_explicit_trust_act_cannot_override_a_standing_quarantine`, also not run here.

### `issue-39#finding-hard-exclusion-and-prior-pinning` — T2B — `conflict`

**Claim.** Hard exclusions (`effort` unparseable, `intelligence_index` null, absent from AA without opt-in) and numeric priors (`intelligence_index`/price/speed) are pinned to one exact routable triple — "a mis-keyed triple corrupts every prior attached to it" — so a candidate with an unparseable effort is hard-excluded by the schema itself, and a superseded triple's pinned data is retained (schema-valid) rather than deleted, with the newer, non-superseded triple governing new routing decisions.

**Cited command.** `python3 -m pytest tests/test_schemas.py -k test_routing_candidate_triple_pinning_hard_exclusion_and_expiry -q`

**Why it does not hold.** Two clauses are genuinely proven with a rejecting case: an unparseable effort is schema-rejected, and a superseded triple stays schema-valid rather than being deleted. The third clause is contradicted: `git grep superseded` over `scripts/office_routing.py` and `tests/test_derived_routing.py` returns nothing, so `route()` never reads `superseded_by` and the newer triple does NOT demonstrably govern new routing decisions. A schema-only test cannot fail for that. The other named hard exclusions (`intelligence_index` null, absent from AA without opt-in) are also unexercised.

---

## Stage 2 · W8 · task — T4 review, landing and compaction rows

**Rows: 4**

Checkpoints, review loops and finding exit codes are real. What is unasserted is the *conditionality* (a file-existence check after an unconditional snapshot cannot distinguish conditional from unconditional compaction) and the *destinations* of structural finding routing.

### `issue-77#structural-finding-routing` — T4 — `conflict`

**Claim.** Review findings route structurally: local code defects to executor, plan/spec defects to planner, user requirements to orchestrator/user, and integration conflicts to family coordination.

**Cited command.** `python3 -m pytest tests/test_landings.py -k test_structural_finding_routing -q`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Partially covered elsewhere: `scripts/review_loop.sh` routes by defect kind through distinct exit codes (1 implementation, 2 plan, 3 brief, 4 unavailable) and `tests/test_review.py` asserts several of them. Exit-code-by-kind is not the same as routing to executor/planner/orchestrator/family coordination, and the integration-conflict leg has no destination because no family-coordination route exists.

### `issue-48#role-portability` — T4 — `conflict`

**Claim.** Every role, including the orchestrator, is compactable and transferable mid-run; its resumable state is a serialized checkpoint artifact, never host-specific agent session state, so wall-clock/token attribution is measured at the run/role level across compaction boundaries.

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','48','--json','comments','--jq','.comments[].body']); assert b'every role (orchestrator included) is compactable and transferable mid-run' in out"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. Checkpoint save/load/validate is real and tested in `tests/test_landings.py`, but nothing asserts the ORCHESTRATOR is among the compactable roles, and `scripts/hooks/pre_compact.sh` writes a raw `state.json` snapshot rather than a schema-conformant resumable checkpoint (T4 report).

### `issue-35#decision-7` — T4 — `conflict`

**Claim.** Structured packets survive role handoff and compaction occurs conditionally at semantic phase boundaries before adversarial review when implementation history is substantial.

**Cited command.** `python3 -m pytest tests/test_hooks.py -k test_pre_compact -q`

**Why it does not hold.** The selected test writes a `state.json` fixture, runs `pre_compact.sh`, and asserts a `snapshot-*.json` file exists and `catch-up.md` contains the run id. It asserts nothing about structured packets surviving handoff and nothing about compaction being CONDITIONAL on a semantic phase boundary or substantial history: the hook snapshots unconditionally, so the test passes identically whether the conditionality exists or not. T4 independently reported the hook serialises a raw `state.json` copy rather than a schema-conformant checkpoint.

### `issue-77#compaction-boundary` — T4 — `conflict`

**Claim.** Long-lived roles serialize structured checkpoint state before adversarial review and conditionally compact transcript history when implementation history is substantial.

**Cited command.** `python3 -m pytest tests/test_hooks.py -k test_pre_compact -q`

**Why it does not hold.** Same command and same defect as `issue-35#decision-7`. A file-existence assertion after an unconditional snapshot cannot distinguish conditional compaction from unconditional compaction, and no structured checkpoint is validated against `schemas/checkpoint.schema.json`.

---

## Stage 2 · W9 · task — Prove monitor delivery is non-blocking

**Rows: 1**

28 tests pin event identity, sequence, idempotence, corroboration and pane-closure safety. None holds an orchestrator-side call and proves it returns while a worker is still running, which is the row's whole claim.

### `issue-93#non-blocking-monitoring` — T3 — `conflict`

**Claim.** Monitor abstraction provides non-blocking completion event delivery across Herdr panes for Claude, Codex, and agy without freezing orchestrator execution.

**Cited command.** `python3 -m pytest tests/test_monitor.py -q`

**Why it does not hold.** Passes for a reason other than the claim. The 28 tests pin event identity, sequence/idempotence, corroboration and pane-closure safety. Nothing asserts DELIVERY IS NON-BLOCKING, which is the row behaviour: no test holds an orchestrator-side call and proves it returns while a worker is still running. Per-harness coverage is also uneven, with an agy fixture and generic panes but no Claude or Codex leg. `scripts/office_monitor.py:20` itself documents agy exposing a BLOCKING wait.

---

## Stage 2 · W10 · task — Adapter conformance: live checks and the valid-unverified/invalid split

**Rows: 1**

Scaffolding and nine deterministic checks exist. The live check and the clean distinction between `valid-unverified` and `invalid` are not exercised.

### `issue-35#child-83` — T2B — `conflict`

**Claim.** Conformance workflow scaffolds adapter definitions and executes deterministic and live checks, cleanly distinguishing valid-unverified from invalid adapters.

**Cited command.** `python3 -m pytest tests/test_adapters.py -k test_conformance_workflow -q`

**Why it does not hold.** The row cites a test function name that does not exist, so the command tests nothing and cannot substantiate anything. Partially covered elsewhere: `scripts/office_runtime.py:330 cmd_scaffold_adapter` implements scaffolding and `tests/test_adapters.py::TestAdapterConformance` holds nine deterministic checks. The live-check half and the explicit valid-unverified-versus-invalid distinction have no test in that class; trust states are exercised separately in `tests/test_trust_conformance.py`.

---

## Stage 3 · W11 · grilling — Build the reward/calibration/maturity subsystem in v3, or open a v4 map?

**Rows: 11**

The largest single chunk: 11 rows, all genuinely absent, all one subsystem. `git grep` finds no reader for `exploration.*` config keys, no `recurrence_window`, no `calibration`, no `task_credit`, no size-class normalisation, no `scrutiny` reader, no `interpolated` flag, and no caller comparing `maturity_age` against a gate. Config declares several of these; declaration is not enforcement. This is a build, not a fix, and it is plausibly its own map. Deciding scope here unblocks or removes a sixth of the matrix.

### `issue-35#child-48` — T2B — `conflict`

**Claim.** Per-role scoring awards 2x gate credit for accepted-material findings, penalizes producer at round 5, normalizes against size class, and auto-labels at window close.

**Cited command.** `python3 -m pytest tests/test_scoring.py -q`

**Why it does not hold.** Passes for a reason other than the claim, on all four clauses. The 31 tests are `CapabilityFloorTests`, `LocalRewardTests` and `TrustActWritePathTests`. `git grep` finds no 2x gate credit for accepted-material findings, no round-5 producer penalty, no size-class normalisation of scores (`size_class` exists only as a `dispatches` DB column at `scripts/office_runtime.py:295`), and no auto-labelling at window close. The suite would pass unchanged with all four absent, which it currently is.

### `issue-35#child-51` — T2B — `missing`

**Claim.** Calibration and replay protocol requires 20 labeled rows, highlights decision-boundary flips, uses an 80/20 chronological holdout, and warns if zero flips occur.

**Cited command.** `python3 -m pytest tests/test_calibration.py -q`

**Why it does not hold.** Behaviour genuinely absent. `git grep -i calibration` over `scripts/ tests/ config/ schemas/` returns nothing outside `references/OFFICE-SKILLS-V3-SPEC.md` prose. No 20-row bar, no decision-boundary flip detection, no 80/20 chronological holdout, no zero-flip warning exists. Required change (NOT made): implement the calibration/replay protocol and its test file.

### `issue-48#recurrence-window` — T2B — `missing`

**Claim.** Every dispatch is labeled `pending` (excluded from scoring) until an automated, scheduled recurrence check closes its window at 14 days or 3 subsequent runs, whichever comes first, rather than a human noticing or self-report deciding the label.

**Cited command.** `python3 -m pytest tests/test_scoring.py -k test_recurrence_window_auto_labels_at_close -q`

**Why it does not hold.** Behaviour genuinely absent. `git grep -i recurrence_window` over `scripts/ tests/ schemas/ config/` returns nothing. `schemas/outcome-label.schema.json` admits a `pending` label and `tests/test_schemas.py::test_pending_label_never_carries_evidence` pins that a pending row carries no evidence, but no scheduled check closes a window at 14 days or 3 subsequent runs and nothing transitions a label automatically. Required change (NOT made): the scheduled window-closing job plus its test.

### `issue-48#orchestrator-scoring` — T2B — `conflict`

**Claim.** The orchestrator's "PR success" reward term uses the same recurrence-gated terminal ground truth as every other role (merged AND no recurrence within the window), never a weaker merge-only bar.

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','48','--json','comments','--jq','.comments[].body']); assert b'Same recurrence-based terminal ground truth' in out"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. Independently: no recurrence-gated terminal ground truth exists at all (see `issue-48#recurrence-window`, marked missing), so the orchestrator cannot be shown to share it.

### `issue-48#task-credit-mapping` — T2B — `conflict`

**Claim.** Reward/defect credit maps to the originating task, falling back to a reduced-weight run-level charge when the originating task cannot be identified — this is the settled input (constraints 13-15) this task interprets as the "changed-line credit assignment" requirement; flag for confirmation if a different, literal per-line attribution mechanism was intended.

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','48','--json','body']); import json; assert 'credit maps to the originating task' in json.loads(out)['body']"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. Independently: `git grep task_credit` and `git grep originating_task` over `scripts/` both return nothing. No credit mapping and no reduced-weight run-level fallback exist.

### `issue-42#size-class-accuracy-bar` — T2B — `conflict`

**Claim.** The pre-dispatch size-class estimator (S/M/L/XL, from plan task count and file/diff scope) is calibrated toward landing the correct class at least 70% of the time and the adjacent class the rest, is not a dispatch-blocking gate on a miss, uses a fixed per-role cold-start default absent history, and self-corrects from an estimated-vs-actual outcome row per dispatch.

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','42','--json','comments','--jq','.comments[].body']); assert b'correct class' in out and b'70%' in out"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. Independently: `size_class` exists only as a `dispatches` column (`scripts/office_runtime.py:295`). There is no estimator, no 70%-accuracy calibration, no cold-start default and no estimated-versus-actual outcome row, so no accuracy bar can be measured.

### `issue-55#evidence-bar-and-decay` — T2B — `conflict`

**Claim.** An area is not treated as meaningfully mature until at least 30 labeled runs across 3+ repositories/task shapes exist (hard exclusion below that bar), and a major model/harness release decays only the evidence whose assumptions it invalidates, proportional to how much of the underlying stack changed, rather than erasing unrelated history.

**Cited command.** `python3 -c "import yaml; c = yaml.safe_load(open('config/config.default.yaml')); m = c['maturity']; assert m['scrutiny_bar']['min_labeled_runs'] == 30 and m['scrutiny_bar']['min_repositories_or_task_shapes'] == 3 and set(m['decay']) == {'minor_harness_release_no_contract_change','major_harness_or_invocation_change','new_model_generation','unrelated_change'}"`

**Why it does not hold.** The command loads a YAML file and asserts literal values in it. A config declaration is not an enforcement: `git grep -i scrutiny` over `scripts/` returns nothing, so no code reads `maturity.scrutiny_bar`, no area is hard-excluded below 30 labeled runs or 3 repositories/task shapes, and no decay is applied proportionally to any release. The command would pass identically with zero consumers, which is the current state.

### `config#exploration-limits` — T2B — `missing`

**Claim.** Exploration dispatches are bounded by `exploration.max_per_run`, `max_percent_rolling_20`, `max_cost_vs_cheapest_percent`, and restricted to `allowed_roles`/`allowed_playbooks`.

**Cited command.** `python3 -m pytest tests/test_scoring.py -k test_exploration_limits -q`

**Why it does not hold.** Behaviour genuinely absent and unchanged from the prior evidence note. `config/config.default.yaml:19` declares `exploration.max_per_run`, `max_percent_rolling_20`, `max_cost_vs_cheapest_percent`, `allowed_roles` and `allowed_playbooks`; `git grep` shows no reader for any of them in `scripts/`. A declared-but-unread config key is a gate that cannot reject anything. Required change (NOT made): read and enforce the keys in the selection stage, with a test constructing an over-limit dispatch.

### `issue-39#catalog-expiry-and-numeric-confidence` — T2B — `conflict`

**Claim.** A catalog triple is never deleted (carries `last_seen`/`superseded_by` so hot/warm/dreamt tiers can expire pinned findings without losing them), every routing decision records the snapshot's `content_hash` for reproducibility, and an interpolated numeric value is explicitly flagged `interpolated: true` rather than presented as a measured score.

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','39','--json','comments','--jq','.comments[].body']); assert b'last_seen' in out and b'superseded_by' in out"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. Independently: `last_seen`/`superseded_by` exist in `schemas/routing-candidate.schema.json`, but `git grep interpolated` over `scripts/` returns nothing, so no value is ever flagged `interpolated: true`, and no routing decision is shown recording the snapshot `content_hash`.

### `issue-35#child-55` — T5S — `missing`

**Claim.** Continuous maturity age (0–100 asymptotic scale) gates automated evolution before the permanent human merge-to-main boundary.

**Cited command.** `test -f references/graduation-bar.yaml`

**Why it does not hold.** The cited artifact is absent. A maturity curve does exist and is tested (`scripts/office_runtime.py:253 maturity_age`, `tests/test_runtime.py:135 test_maturity_curve` asserting 0 -> 0 and asymptotic approach below 100), but nothing gates automated evolution on it: no caller compares `maturity_age` against a bar. Required change (NOT made): commit the graduation bar artifact and wire it as a gate ahead of the merge-to-main boundary.

### `issue-35#child-50` — T5S — `conflict`

**Claim.** Dream compiler background subagent lazily extracts pattern lines from runs.db, compiles them into reference docs with self-certification, and maintains hot/warm/dreamt tiers.

**Cited command.** `python3 -m pytest tests/test_propose.py -q`

**Why it does not hold.** The command passes and one named clause is absent. Dream compilation from `runs.db` and sanitisation are implemented and covered by the 14 tests. The hot/warm/dreamt tier lifecycle is not implemented and no test asserts it, so the passing suite cannot fail for its absence. This is the row previously carrying that disposition; running the command confirms it rather than changes it.

---

## Stage 3 · W12 · grilling — Six mode presets, or three?

**Rows: 1**

`scripts/office_runtime.py:572` auto-selects only `express`, `direct`, and `full` on irreversible. `direct+review`, `light` and `quick` exist solely as `--gear` CLI choices and can never be auto-selected. Either the fit test is missing three branches or the row's preset list is wrong. That is a spec question, not a bug.

### `issue-35#child-45` — T0 — `conflict`

**Claim.** The fit test automatically evaluates blast radius, reversibility, and size class to select one of six mode presets (direct, direct+review, light, quick, express, full).

**Cited command.** `python3 -m pytest tests/test_runtime.py -k test_fit_test -q`

**Why it does not hold.** The command passes and the behaviour it names is absent. `scripts/office_runtime.py:572` returns only `"express"` or `"direct"`, plus `full` on irreversible. Three of the six presets (`direct+review`, `light`, `quick`) exist solely as `--gear` CLI choices at line 1437 and can never be auto-selected by the fit test. A four-test suite over a three-outcome function cannot fail for the missing three.

---

## Stage 3 · W13 · grilling — Is Repository Agent Readiness a real feature?

**Rows: 1**

`git grep -i readiness` over `scripts/ tests/ schemas/ config/` returns nothing. The term lives only in plan and spec prose. Nothing computes a score and nothing consumes one. Build it, or rule it out of scope.

### `issue-35#child-82` — T2 — `missing`

**Claim.** Repository Agent Readiness scoring influences planning, gear selection, and verifier funding without lowering capability or safety floors.

**Cited command.** `python3 -m pytest tests/test_families.py -k test_readiness_scoring -q`

**Why it does not hold.** Behaviour genuinely absent. `git grep -i readiness` over `scripts/ tests/ schemas/ config/` returns nothing; the term appears only in `docs/plans/v3-final-merge.md`, `docs/v3-acceptance.md` and `references/OFFICE-SKILLS-V3-LIFECYCLE-SPEC.md` prose. No score is computed and nothing consumes one. Matches the T7 round-1 enumeration of gates with no test constructing their rejecting case.

---

## Stage 3 · W14 · task — Catalog freshness: independent triggers and out-of-band refresh

**Rows: 3**

`git grep -i stale_catalog` over `scripts/` returns nothing. No staleness check, no out-of-band refresh, no next-decision eligibility rule, and no compact rendered status line. The row pointer for the refresh rule is also stale: `office_runtime.py:582` is gitignore-append code.

### `issue-40#dual-detection-triggers` — T2 — `conflict`

**Claim.** Harness-capability rediscovery and catalog-freshness refresh are independent triggers: an unchanged CLI fingerprint never causes harness rediscovery, and catalog staleness (>3 days) is evaluated independently of the CLI fingerprint.

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','40','--json','comments','--jq','.comments[].body']); assert b'Harness-change trigger' in out"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. Independently: `git grep -i stale_catalog` over `scripts/` returns nothing, so neither trigger is implemented and the independence claim has nothing to be independent of.

### `issue-40#refresh-outside-critical-path` — T2 — `missing`

**Claim.** A stale catalog (>3 days old) starts its refresh outside the current run's critical path; the current routing decision continues on the last reproducible snapshot rather than blocking on the refresh, and refreshed models become eligible only on a subsequent routing decision.

**Cited command.** `python3 -m pytest tests/test_scoring.py -k test_stale_catalog_refresh_is_non_blocking -q`

**Why it does not hold.** Behaviour genuinely absent and the row pointer is stale. `scripts/office_runtime.py:582` is `.office/` gitignore-append code, not catalog refresh. `git grep -i stale_catalog` over `scripts/` returns nothing: no staleness check, no out-of-band refresh, no next-decision eligibility rule. Required change (NOT made): implement the non-blocking refresh and a test proving the current decision uses the old snapshot.

### `issue-40#compact-detection-output` — T2 — `conflict`

**Claim.** Raw probe/discovery output is parsed and discarded; the agent context receives only a compact rendered status line (e.g. `harnesses: 3 unchanged`, `catalog: 2d old, fresh`, `refresh: skipped`, joined into one line).

**Cited command.** `python3 -c "import subprocess; out = subprocess.check_output(['gh','issue','view','40','--json','comments','--jq','.comments[].body']); assert b'compact rendered status line' in out"`

**Why it does not hold.** The command asserts a phrase is present in the GitHub issue record. That establishes what was DECIDED, not what was BUILT: it reads no file in this repository and would pass identically against an empty implementation. No code path renders a compact `harnesses: N unchanged / catalog: Nd old / refresh: skipped` status line, so nothing discards raw probe output either.

---

## Stage 3 · W15 · task — Verification depth: integration adversary, verifier funding, evidence backing

**Rows: 6**

Six rows where the vocabulary exists and the rule does not. `integration_adversary` is a schema enum value in three schemas with no triggering rule. `blast_radius` is a required packet field with no evidence-backing check. The `findings` table declares `dispatch_id TEXT` with no foreign key and `review_finding.sh` inserts without checking the dispatch exists. Nothing sizes a verifier budget from risk, gear or playbook.

### `issue-35#decision-4` — T4 — `missing`

**Claim.** Self-review is universal, planner owns plan-review, executor owns code-review, and a final integration adversary is triggered only by genuinely dependent or merging executor landings.

**Cited command.** `python3 -m pytest tests/test_landings.py -k test_integration_adversary_trigger -q`

**Why it does not hold.** Trigger behaviour genuinely absent, confirming the T4 report. `integration_adversary` appears as a schema enum value in `schemas/landing.schema.json`, `review-result.schema.json` and `checkpoint.schema.json`, and as a documentation-text assertion in `tests/test_v3_instruction_contract.py::test_integration_review_trigger_is_explicitly_conditional`. No code counts dependent or merging landings, so nothing can trigger. Required change (NOT made): a dependent-landing detector and a test constructing both the triggering and the non-triggering case.

### `issue-77#integration-adversary-trigger` — T4 — `missing`

**Claim.** A dedicated integration adversary is spawned only when two or more executors produce dependent or merging landings requiring cross-scope validation.

**Cited command.** `python3 -m pytest tests/test_landings.py -k test_integration_trigger -q`

**Why it does not hold.** Same absent behaviour as `issue-35#decision-4`; see that row. The schema vocabulary for an integration adversary exists, the triggering rule does not.

### `issue-35#child-57` — T4 — `conflict`

**Claim.** Universal verification floor requires self-verification for every task and funds independent verifiers dynamically based on risk, gear, and playbook.

**Cited command.** `python3 -m pytest tests/test_landings.py -q`

**Why it does not hold.** Passes for a reason other than the claim. `tests/test_landings.py` covers family CLI, checkpoints, landing recording/validation and review recording. `git grep` for verifier funding across `scripts/` and `tests/` returns nothing: no risk/gear/playbook input sizes a verifier budget anywhere. The dynamic-funding half of the row has no implementation for the suite to fail against.

### `issue-35#child-84` — T1 — `missing`

**Claim.** Blast radius declarations must be evidence-backed through static call/dependency traces or runtime probes before execution begins.

**Cited command.** `python3 -m pytest tests/test_v3_instruction_contract.py -k test_blast_radius -q`

**Why it does not hold.** Partially absent, and the absent half is the claim. `blast_radius` is a required field of `schemas/execution-packet.schema.json` with reject fixtures, so a packet without one fails. Nothing anywhere derives or validates that the declared value is evidence-backed by a static call/dependency trace or a runtime probe; the field accepts any string. Required change (NOT made): a trace/probe producer plus a gate rejecting an unbacked declaration.

### `plan-t0#finding-F6-findings-fk` — T4 — `missing`

**Claim.** The `findings` table declares `dispatch_id TEXT REFERENCES dispatches(id)` and rejects (or the recorder rejects) a finding insert for an unknown dispatch id; a test proves both the dispatch start/end lifecycle rows and the finding-to-dispatch linkage, not file existence alone.

**Cited command.** `python3 -m pytest tests/test_review.py -k test_findings_reference_existing_dispatch -q`

**Why it does not hold.** Behaviour genuinely absent, confirming T4 report finding. The `findings` table declares `dispatch_id TEXT` and nothing more, and `scripts/review_finding.sh` inserts without checking the dispatch exists. Required change (NOT made, out of verification scope): add `REFERENCES dispatches(id)` to the findings DDL plus `PRAGMA foreign_keys=ON`, or a recorder-side existence check, and a test inserting a finding for an unknown dispatch id.

### `issue-35#child-59` — T2 — `missing`

**Claim.** PLAN DEFECT requires proof of plan compliance and contradictory evidence against concrete plan assumptions, scoring as an accepted defect and requiring plan amendment.

**Cited command.** `python3 -m pytest tests/test_amendments.py -k test_plan_defect_amendment -q`

**Why it does not hold.** Behaviour genuinely absent. `scripts/review_loop.sh:316` recognises a `PLAN_DEFECT` status and exits 2, but nothing requires proof of plan compliance, nothing requires contradictory evidence against a concrete plan assumption, and no path scores it as an accepted defect or forces a plan amendment. Required change (NOT made): add the evidence requirement to the PLAN_DEFECT branch and an amendment-triggering test.

---
