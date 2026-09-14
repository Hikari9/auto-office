# Auto Office v3 final implementation and merge plan

## Status and scope

Plan version: 1. Created 2026-09-15. Status: planned, not executed.
This change authorizes only writing and committing this plan. Stop after the plan commit; do not dispatch implementation, push, mark the PR ready, or merge in this session.

Destination: reconcile and complete the existing v3 implementation, demonstrate acceptance, and land `auto-office-v3` into `main` through [Auto Office v3: consolidate lifecycle, routing, and runtime](https://github.com/Hikari9/office-skills/pull/99).

Authoritative requirements:

- [Wayfinder: adaptive harness/model routing](https://github.com/Hikari9/office-skills/issues/35), especially [confirmed decisions 1–3](https://github.com/Hikari9/office-skills/issues/35#issuecomment-5666960972) and [confirmed decisions 4–8](https://github.com/Hikari9/office-skills/issues/35#issuecomment-5667061892).
- [Interactive planner families and multi-project orchestrator control plane](https://github.com/Hikari9/office-skills/issues/77), including its compaction addendum, qualified by the confirmed exceptional-upline decision.
- [Orchestrator portability](https://github.com/Hikari9/office-skills/issues/93), including the amendment withdrawing approval-hook enforcement.

The user has accepted the eight design decisions. This is an implementation plan, not a new interview. Resolve contradictions first; reuse working implementation and add only missing behavior. If evidence reveals a consequential requirement not settled by these sources, pause only that dependent task and surface it.

## Planning envelope

```yaml
plan_version: 1
requirements_version: 1
routing_version: 1
branch: auto-office-v3
target_branch: main
implementation_base_sha: 37aba7c7b6cdf1c2f10bf8b80e747802e89338f8
observed_main_sha: e137cd2ff7f044aeb8984f43f183d6cc1639a954
effective_config_hash: sha256:279b986360e082dcc2136dfc5d29804f6ba1ee3e0ccdf8449612b07d4d7b94a3
playbook: Change
size: XL
execution_status: not_started
model_assignments:
  orchestrator:
    invocation_model_id: not_exposed
    canonical_model_id: GPT-6
    effort: not_exposed
    harness: Codex
    harness_version: not_exposed
    status: inline
    rationale: User-selected current session owns this bounded planning task.
  planner:
    invocation_model_id: not_exposed
    canonical_model_id: GPT-6
    effort: not_exposed
    harness: Codex
    harness_version: not_exposed
    status: inline
    rationale: Same session reconciles inspected code and confirmed requirements; no dedicated planner dispatched.
```

Unknown runtime identities are explicitly unknown, not fabricated routable triples. This envelope records planning provenance, not an execution receipt. Before execution, resolve actual role routes through the router, pin plugin/policy/catalog/adapter/config hashes, and generate accepted execution packets. Recompute config for the execution environment; if it differs, record the new routing version and rationale rather than silently using this planning snapshot.

## Frozen outcome and boundaries

Goal: satisfy the confirmed v3 decisions and prove that the resulting lifecycle works before the final merge.

Done criteria:

1. Every acceptance requirement across the three linked issues has an implementation/spec pointer and verification receipt, or a user-approved supersession. The ten earlier findings are not the entire acceptance inventory.
2. Planner/user interaction and requirements freezing, executor-owned review with exceptional consultation, local review funding, and dependency-triggered integration review agree across active instructions and behavior.
3. Requirements, plan, and routing amend independently; unaffected work continues; durable family state and structured packets support restart without child transcripts.
4. Claude, Codex, and agy each demonstrate the orchestrator lifecycle, including Herdr-path completion events and continued user interaction.
5. Current-head verification and independent review pass; PR is ready and merged under explicit merge authority; resulting main passes the smoke checks below.

Blast radius: root skill and lifecycle specs drive every run; state/version/approval helpers in `scripts/office_runtime.py` feed hooks, dispatches, schema validation and takeover; review scripts can record false success; config resolution affects all role routing; completion/pane hooks affect concurrent live workers. These are cross-cutting runtime and instruction changes, hence XL despite a reuse-first approach. Public artifacts must not contain raw transcripts, credentials, personal configuration or private run data.

Protected paths and non-goals: no changes to user-global skill installations, harness settings, credentials, unrelated repositories, or live user runs. Do not redesign scoring/rewards/capability floors, revive Bash approval enforcement, introduce a heavyweight scheduler, or prove additional harnesses such as Hermes/pi. Do not delete or migrate private telemetry destructively. Do not implement a second independent review merely to satisfy executor count. Any necessary change beyond these boundaries requires a scoped plan amendment.

Named future actions: isolated fixture worktrees and test runs; scoped implementation commits and PR updates after execution is authorized; final merge only under explicit user authority. Billing/payment changes are user-owned and not authorized. This plan commit does not authorize any of these future actions now.

## Architecture and shared contracts

Keep `office_runtime.py` as the existing entry point. Put substantial new family/amendment/packet logic in focused sibling modules rather than growing unrelated conditionals throughout it. Existing CLI commands remain compatible where their semantics are still valid. New subcommands are introduced only for behavior that needs deterministic persistence or validation.

Wave 0 pins schemas and callable boundaries before parallel implementation:

- Version identity: positive `requirements_version`, `plan_version`, `routing_version`; retain `packet_version` as packet revision, not a substitute. Approval references requirement/plan identity; a routing-only change must not invalidate product approval. Record new dispatch routes while preserving historical dispatch identity.
- Family registry: session identity, current focus, and per-family repo/issue, phase, versions, ownership, dependencies, active dispatches, latest landing, pending decisions. Updates are atomic and conflict-aware; unrelated orchestrators cannot overwrite one another's records. Ambiguous focus causes no mutation.
- Amendment operation: typed `routing`, `requirements`, or `plan_contract` delta with affected scopes, expected prior versions, evidence/reason and resulting versions. Requirements within the plan notify affected executors; contract changes pause affected scopes, obtain revised planner output/review, invalidate affected packets and resume. Running routes change at atomic boundaries unless explicitly replaced.
- Landing/checkpoint: family/producer/scope, versions, base/head/diff references, completed tasks, decisions, changes/interfaces, validation evidence, review mode/round/dispositions, deviations, dependencies/artifacts, blockers. Missing evidence is not success. Checkpoint adds unresolved concerns and next phase; public summaries link only sanitized evidence.
- Review result: producer/reviewer identity, reviewed tree/versions, findings, disposition owner and evidence; unresolved consultation is explicit. Inline verification is labeled as such, never independent approval. Keep existing telemetry vocabulary with an explicit mapping for landing disposition names.
- Completion event: session/family/dispatch identity, sequence/event ID, observed status, evidence timestamp, source and terminal classification. Persist events with replay/deduplication so restarts do not lose completion. Status alone cannot justify reclaiming a still-working pane. Polling may occur inside a background bridge; the orchestrator receives events and remains responsive.

Prefer small JSON fixtures demonstrating these contracts. Pin exact module signatures and CLI inputs in Wave 0's contract document; do not let parallel workers invent competing schemas. Native harness completion events can be reused where they cover the actual dispatch; Herdr requires demonstrated delivery through the selected harness capability. Do not claim a durable queue alone wakes an idle orchestrator.

## Ordered work waves

Every task has explicit ownership. Parallelism is optional; tasks sharing files are serial. When later waves legitimately revisit an earlier file, only the later owner may write it after the earlier task lands. Integrate and verify each wave before cutting dependent worktrees. Every dispatch brief includes the accepted contract, versions, `effective_config_hash`, allowed paths and exact validation commands.

### Wave 0 — acceptance and contract baseline

**T0: Reconcile requirements and pin shared contracts.**
Depends on: accepted execution plan.
Touches: `docs/v3-acceptance.md`, `docs/v3-runtime-contracts.md`, `schemas/run-envelope.schema.json`, new family/amendment/landing/checkpoint/completion schemas, `tests/test_schemas.py`.

Read current issue comments, branch/PR state, existing helpers and tests. Build an acceptance matrix covering the original adaptive-routing map as well as the family and portability amendments. Classify each row as existing/verified, instruction conflict, missing behavior, or external blocker. Record superseded requirements explicitly. Inspect review-loop callers before choosing whether to replace its mock callback or retire an unused helper and point callers at the real review path. Pin contract fixtures and legacy-state handling before implementation.

Receipt: schema tests accept complete examples and reject missing identity/version/evidence; acceptance matrix has no unexplained requirement. Any newly found material gap outside this plan becomes a versioned plan amendment, not silent additional scope.

### Wave 1 — parallel, disjoint foundations

**T1: Reconcile active instructions and portable intake.**
Depends on: T0.
Touches: `SKILL.md`, `protocol/*.md`, `skills/*/SKILL.md`, skill-local reference markdown, `references/OFFICE-SKILLS-V3-SPEC.md`, `references/OFFICE-SKILLS-V3-LIFECYCLE-SPEC.md`, `references/IMPLEMENTATION-NOTES.md`, `references/why-*.md`, new `tests/test_v3_instruction_contract.py`.

Replace obsolete authority rules with provisional orchestrator intake and interactive planner freeze; preserve executor disposition ownership and exceptional upline consultation. Define local/inline/integration review, amendment ownership, sticky focus and conditional compaction. Specify native Skill loading or complete direct-file loading with the same receipt; structured questions or batched plain text preserve intent coverage. No approval-hook enforcement requirement is reintroduced. Scope tests to critical contradictory instructions and contract examples, not prose snapshots.

Receipt: `python3 scripts/check_ecosystem.py` and `python3 -m pytest tests/test_v3_instruction_contract.py tests/test_budget.py -q` pass; targeted search finds no active contradictory planner prohibition or unconditional review escalation.

**T2: Implement family state, versions and scoped amendments.**
Depends on: T0.
Touches: `scripts/office_runtime.py`, new `scripts/office_family.py` and `scripts/office_packets.py`, `config/config.default.yaml`, `tests/test_runtime.py`, new `tests/test_families.py`, new `tests/test_amendments.py`.

Reuse atomic writes, leases, hashing, state reconciliation and config layering. Add family/session tiers with precedence: dispatch > family/project > repo > session > user > default. Implement durable focus and scoped delta application. Provide advisory current/projected resource-demand summaries; do not block unrelated families for optimization. Integrate version changes with existing monotonic phase and approval validation instead of bypassing those checks. Legacy state must load read-only or migrate explicitly with a backup and recorded version defaults; never silently certify stale packets or grant ownership.

Receipt: tests prove routing-only deltas leave requirements/plan approval intact, stale concurrent deltas fail without partial writes, contract changes invalidate only affected work, ambiguous focus mutates nothing, two sessions do not collide, and restart reconstructs active families. Legacy state preserves unknown fields and evidence.

**T3: Implement Herdr event delivery and safe reclamation.**
Depends on: T0.
Touches: new `scripts/office_monitor.py`, `scripts/office_liveness.sh`, `scripts/hooks/close_finished_panes.mjs`, new `tests/test_monitor.py`.

Discover the actual available completion-delivery mechanism for each required orchestrator harness before choosing its binding. Reuse available capabilities; add a background bridge only where needed. Support finish, idle, blocked, unknown and disappeared observations, start receipt, durable event replay and monitor health. Distinguish reported done from verified completion; preserve blocked/unknown panes and require independent evidence before declaring death. Explicitly handle the previously observed agy false-done case.

Receipt: deterministic harness fakes demonstrate event delivery, duplicate suppression, reconnect/restart replay, monitor loss visibility, concurrent family isolation and no closure on false done. A background process merely producing a file is not sufficient acceptance for waking the orchestrator; live proof follows in T6.

### Wave 2 — integrate review and packet consumers

**T4: Wire real review, landings, checkpoints and dispatch.**
Depends on: T1, T2, T3.
Touches: `scripts/review_loop.sh`, `scripts/review_finding.sh`, `scripts/office_readback.sh`, `scripts/office_spawn.sh`, `scripts/hooks/pre_compact.sh`, `scripts/hooks/compact_advisor.sh`, `scripts/hooks/session_end.sh`, `scripts/hooks/install_hooks.sh` only if optional lifecycle binding needs it, `tests/test_review.py`, `tests/test_hooks.py`, new `tests/test_landings.py`.

Connect to actual independent review evidence or retire unused mock behavior according to T0. Never synthesize PASS from an unset environment variable. Local producers own findings; consultation routes unresolved disagreement upward without routine orchestrator override. Integration review is triggered by actual dependent/merging landings, not count alone. Readback retains process diagnostics while validating semantic landing evidence. Serialize checkpoints before requested compaction and resume the same role from the packet. Wire monitor lifecycle into dispatch and closeout; keep optional hooks optional.

Receipt: unset review source yields explicit unavailable/required review status, never independent PASS; stale tree/version or producer-as-reviewer evidence is rejected; inline paths remain valid. Single executor and independent parallel changes avoid extra final review; dependent landings receive it. Valid evidence-backed refutation lands; unresolved consultation stays unresolved. Exit 0 alone cannot produce a completed landing. Restart preserves checkpoint fields and review round.

### Wave 3 — full acceptance and real portability

**T5: Integrated regression and coverage verification.**
Depends on: T4.
Touches: `tests/test_integration.py`, `tests/test_dogfood.py`, `docs/v3-acceptance.md`, `.github/workflows/validate.yml` only for justified new checks.

Run the complete workflow on isolated fixtures: interactive requirements freeze, two concurrent families, local review, cross-scope integration review, routing-only delta, plan-contract delta, checkpoint/restart and event-driven completion. Assert reasons and state changes, not just exit codes. Verify existing adaptive routing, quota floors, pinned snapshots, private telemetry, replay gates, proposal isolation and retirement coverage from T0 remain intact.

Receipt: `python3 scripts/check_ecosystem.py`; `python3 -m pytest tests/ -q`; all remaining commands in `.github/workflows/validate.yml`; `git diff --check`. Capture exact tested head and concise results. Do not weaken checks to make the plan fit.

**T6: Demonstrate each required orchestrator harness.**
Depends on: T5.
Touches: `docs/v3-portability-evidence.md`, sanitized evidence links in `docs/v3-acceptance.md`; live raw evidence stays outside the repository.

Run a bounded scratch-repository scenario under Claude, Codex and agy as orchestrators, not just workers. Each must load/mark a spoke, ask/receive user input through its supported path, dispatch via Herdr, remain responsive while work runs, receive terminal events, validate a landing, and restore state after a checkpoint. Exercise an interruption/blocked outcome without losing the pane. Reuse deterministic fakes for destructive/failure cases; never stop unrelated live workers. Record harness/model/version, invocation, commit, event/receipt IDs and outcome without private transcript content.

Receipt: three explicit passing lifecycle records at the candidate head. Missing credentials, missing delivery capability or unavailable user interaction is a reported acceptance blocker, never replaced by an adapter unit test or invented answer. Targeted repairs return to the owning task and rerun affected checks.

### Wave 4 — independent review, PR readiness and final merge

**T7: Review and close the coverage gaps.**
Depends on: T6.
Touches: `docs/v3-acceptance.md`, `docs/v3-portability-evidence.md`, PR/issue metadata; fixes return to the original file owners.

Have an independent reviewer inspect the frozen requirements, final diff, acceptance matrix, negative controls and real-harness receipts. Review shared-state races, version migration, false review success, lost completion events and unaffected-family behavior. Follow executor-owned disposition with exceptional consultation. Update the PR description to describe final implemented behavior and remove obsolete gap claims. Reconcile map/ticket summaries with authoritative comments without declaring success before proof.

External blocker: the observed GitHub Validate job did not start because the account is locked for billing. The account owner must resolve this; do not alter billing or bypass CI. After restoration, run CI at the final head. Local passes do not erase the external blocker.

Receipt: accepted review dispositions, complete acceptance matrix and green current-head checks; PR ready. All material unresolved items remain visible and block a completeness claim.

**T8: Merge and verify main.**
Depends on: T7 and explicit merge authority.
Touches: integration branch/main through PR merge; linked issue/map state; no unrelated code.

Refresh main and PR head. If main advanced, integrate it and repeat affected verification plus required full checks before merge. Use repository merge convention without force/admin bypass. Preserve private run state and avoid broad branch/worktree cleanup. After merge, verify merge SHA contains the reviewed changes, run ecosystem and test smoke checks on a clean main checkout, and record the PR/merge receipt. Close covered issues and map only once their acceptance is demonstrated; retain any explicitly deferred item with approved scope and a durable pointer.

## Rollback and amendments

Keep implementation in reviewable commits grouped by wave. Record pre-migration state backup and schema versions in private run storage; restore only that test/run scope if migration fails. Before merge, revert the specific faulty wave commit and correct its owning task; do not reset shared worktrees. After merge, use a reviewed revert PR for a demonstrated regression, preserving telemetry and checkpoint evidence. Do not automatically delete state or revert unrelated work.

An accepted PLAN DEFECT must identify the contradicted assumption and evidence. Increment this plan version, update only affected contracts/tasks, invalidate their stale execution packets, and resume after the required review. Known external blockers do not justify lowering acceptance or inventing new requirements.

## Planning verification

Baseline inspected: runtime command/phase and config surfaces, current schemas/tests, review helper, authoritative issue amendments and PR status. Historical baseline was 106 tests plus 8 subtests passing; this plan makes no claim that implementation or current-head portability checks have run. No agents or execution packets were dispatched. Validate this plan's formatting and commit only this file, then stop.
