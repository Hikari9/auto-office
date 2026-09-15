# Auto Office v3 final implementation and merge plan

## Status and scope

Plan version: 2. Created 2026-09-15. Amended 2026-09-15 after independent plan review. Status: approved for execution through T7.

Version 1 authorized only the plan commit. The user has since taken over the orchestrator role in a new session and explicitly authorized amending this plan and launching executors. Execution of T0 through T7 is therefore authorized. Merge to `main` (T8) is not: it remains gated on an explicit, current-session user statement, and no agent may lift that gate on its own initiative. Pushing the branch and updating PR #99 are authorized as part of T7; marking the PR ready is authorized only once T7's receipt is satisfied.

Destination: reconcile and complete the existing v3 implementation, demonstrate acceptance, and land `auto-office-v3` into `main` through [Auto Office v3: consolidate lifecycle, routing, and runtime](https://github.com/Hikari9/office-skills/pull/99).

Authoritative requirements:

- [Wayfinder: adaptive harness/model routing](https://github.com/Hikari9/office-skills/issues/35), especially [confirmed decisions 1–3](https://github.com/Hikari9/office-skills/issues/35#issuecomment-5666960972) and [confirmed decisions 4–8](https://github.com/Hikari9/office-skills/issues/35#issuecomment-5667061892).
- [Interactive planner families and multi-project orchestrator control plane](https://github.com/Hikari9/office-skills/issues/77), including its compaction addendum, qualified by the confirmed exceptional-upline decision.
- [Orchestrator portability](https://github.com/Hikari9/office-skills/issues/93), including the amendment withdrawing approval-hook enforcement.

The user has accepted the eight design decisions. This is an implementation plan, not a new interview. Resolve contradictions first; reuse working implementation and add only missing behavior. If evidence reveals a consequential requirement not settled by these sources, pause only that dependent task and surface it.

## Planning envelope

```yaml
plan_version: 4
requirements_version: 3
routing_version: 3
branch: auto-office-v3
target_branch: main
implementation_base_sha: 213ba5628f62a818ff62a61e159b770100d3a9ba
observed_main_sha: e137cd2ff7f044aeb8984f43f183d6cc1639a954
baseline_at_implementation_base_sha:
  check_ecosystem: PASS (16 skills, 8 schemas, 22 evals)
  pytest: 106 passed, 8 subtests passed
base_drift:
  - sha: 487fca85da3fad9dd067e8192587f27746ec360b
    summary: >-
      User committed an agy quota-probe fix directly to auto-office-v3 mid-run: rewrote
      scripts/agy-usage.py onto the stdlib with native desktop OAuth credentials, dropped
      the requests/python-dotenv dependencies, fixed --all tightest-percent accounting for
      non-Gemini models, and added quota-probe tests to tests/test_adapters.py.
    owned_by: user, outside every task's Touches list; no wave contends with these files
    rebaselined_pytest: 109 passed, 8 subtests passed
    integration_expectation: >-
      T0's worktree was cut at 5d7a450 and, after remediating 11 independent-review
      findings, reports 112 passed / 84 subtests, which is the 106/8 baseline plus 6 tests
      and 76 subtests. Merging T0 onto 487fca8 must therefore yield 115 passed / 84
      subtests. (The earlier figure of 111/83 described T0 at commit 688246b, before
      remediation.) Any other number at integration is a regression or a
      collision to explain, not a new baseline. Later dispatches cut from 487fca8 or later
      use the 109/8 baseline instead.
amendment_history:
  - version: 4
    kind: requirements
    reason: >-
      Independent review round 3 of T0 found that the issue-35 PR-report requirement was
      assigned to T5, whose Touches are only tests and documentation, so it could not
      deliver the deterministic script the requirement names. The matrix reported zero
      unassigned owners while the requirement was effectively unowned. This is the same
      class as v2 finding F1: an authoritative requirement with no owning task. Assigned
      to T4, which already owns the readback and reporting script surface.
    affected_scopes: [T4, T0]
    resulting_versions: {plan_version: 4, requirements_version: 3, routing_version: 3}
  - version: 3
    kind: requirements
    reason: >-
      The orchestrator found that route() decides every dispatch from four values the
      caller supplies rather than derives: adapter_state (office_runtime.py:212),
      absolute_floor_pass (:229), advisory_pass (:272) and local_reward (:282). The
      outcome_labels and lineage tables are created at :374-375 and written by no code
      path; the labels table holds zero rows. maturity_age() scores --points handed to it
      on the command line, and nothing converts event_weights into points. v3 shipped the
      policy constants and the consumption sites without the layer that connects run
      history to them, so its learning loop cannot learn and its trust gate is
      honour-system. The user lifted the "do not redesign scoring/rewards/capability
      floors" non-goal to close this, scoped to structural wiring only.
    affected_scopes: [T0, T1, T2, T2B, T5, non_goals, done_criteria]
    resulting_versions: {plan_version: 3, requirements_version: 2, routing_version: 3}
  - version: 2
    kind: plan_contract
    reason: >-
      Independent plan review (codex@local/luna@xhigh) returned CHANGES REQUIRED with
      twelve accepted-material and one accepted-minor findings. All thirteen were
      verified against source by the orchestrator and accepted.
    affected_scopes: [T0, T2, T3, T4, T5, T5S, T6, T8, status_and_scope, routing]
    resulting_versions: {plan_version: 2, requirements_version: 1, routing_version: 2}
effective_config_hash: sha256:279b986360e082dcc2136dfc5d29804f6ba1ee3e0ccdf8449612b07d4d7b94a3
playbook: Change
size: XL
execution_status: not_started
model_assignments:
  orchestrator:
    invocation_model_id: claude-opus-5[1m]
    canonical_model_id: opus
    effort: not_exposed
    harness: claude
    harness_version: local
    status: inline
    rationale: >-
      User-directed takeover. Holds intent, dispatch, integration and the merge gate.
      Produced this amendment, so it may not hold any gate over the work it amends.
  planner:
    invocation_model_id: claude-opus-5[1m]
    canonical_model_id: opus
    effort: not_exposed
    harness: claude
    harness_version: local
    status: inline
    rationale: >-
      Amendment applies accepted findings from an independent reviewer; no new product
      design was authored, so no dedicated planner was dispatched.
  plan_reviewer:
    invocation_model_id: gpt-5.6-luna
    canonical_model_id: luna
    effort: xhigh
    harness: codex
    harness_version: local
    status: routed
    decision_hash: sha256:e1a26b6efe059b8ff0a2ecfc0e7d1883f660e97239ccfae846a0a791b487c266
    rationale: >-
      Cleared trust, capability, role-floor and task-shape gates; fit inside the 20%
      protected quota reserve; matched preferred seed #1, which decided advisory ranking.
      Did not produce the plan under review.
```

Unknown runtime identities are explicitly unknown, not fabricated routable triples. This envelope records planning provenance, not an execution receipt. Before execution, resolve actual role routes through the router, pin plugin/policy/catalog/adapter/config hashes, and generate accepted execution packets. Recompute config for the execution environment; if it differs, record the new routing version and rationale rather than silently using this planning snapshot.

## Frozen outcome and boundaries

Goal: satisfy the confirmed v3 decisions and prove that the resulting lifecycle works before the final merge.

Done criteria:

1. Every acceptance requirement across the three linked issues has an implementation/spec pointer and verification receipt, or a user-approved supersession. The ten earlier findings are not the entire acceptance inventory.
2. Planner/user interaction and requirements freezing, executor-owned review with exceptional consultation, local review funding, and dependency-triggered integration review agree across active instructions and behavior.
3. Requirements, plan, and routing amend independently; unaffected work continues; durable family state and structured packets support restart without child transcripts.
4. Claude, Codex, and agy each demonstrate the orchestrator lifecycle, including Herdr-path completion events and continued user interaction.
4a. `route()` derives adapter trust, capability-floor result and local reward from recorded evidence rather than caller assertion; outcome labels are written; an override of a derived gate requires recorded user attribution.
4b. One complete self-improvement cycle runs end to end: sanitized dream, isolated proposal, standing-PR/branch lineage, idempotent replay, independent review, and an intact maintainer-merge boundary, against a committed graduation bar.
5. Current-head verification and independent review pass; PR is ready and merged under explicit merge authority; resulting main passes the smoke checks below.

Blast radius: root skill and lifecycle specs drive every run; state/version/approval helpers in `scripts/office_runtime.py` feed hooks, dispatches, schema validation and takeover; review scripts can record false success; config resolution affects all role routing; completion/pane hooks affect concurrent live workers. These are cross-cutting runtime and instruction changes, hence XL despite a reuse-first approach. Public artifacts must not contain raw transcripts, credentials, personal configuration or private run data.

Protected paths and non-goals: no changes to user-global skill installations, harness settings, credentials, unrelated repositories, or live user runs. `.github/workflows/validate.yml` is protected for the whole run (amendment v2, finding F11), and `scripts/hooks/install_hooks.sh` may be edited but never executed (amendment v2, finding F12). Do not revive Bash approval enforcement, introduce a heavyweight scheduler, or prove additional harnesses such as Hermes/pi. Do not delete or migrate private telemetry destructively. Do not implement a second independent review merely to satisfy executor count. Any necessary change beyond these boundaries requires a scoped plan amendment.

Scoring, rewards and capability floors (amendment v3). Version 1's non-goal barring this work is withdrawn by explicit user decision, and replaced by a bounded one. **Structural change is in scope**: deriving a value from recorded evidence instead of accepting it as caller input, writing the outcome labels that every scoring path already reads, and expressing the capability floor as a declared per-role minimum evaluated against the pinned catalog snapshot. **Parametric change is out of scope**: no task may alter any numeric value in `config/config.default.yaml` under `maturity`, `replay`, `quota`, `cost_policy` or `exploration`, nor change `maturity_age()`'s curve or the reward model's shape. The reason is evidential, not conservative: `runs.db` currently holds 2 dispatches and 0 outcome labels, so new weights chosen today would carry exactly the same authority as the ones already in the file while consuming a wave, and would additionally destroy the value of `replay.min_labeled_rows_for_refit`, which exists so a policy change is validated against recorded decisions rather than intuition. The first reweighting happens in a later run, through the replay gate, once at least 20 labeled rows exist. A task that finds a weight it believes is wrong records it as a matrix row with evidence and leaves the value untouched.

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

**Acceptance matrix row format (amendment v2, finding F13).** Prose classification is not a receipt. Every row is a record with all of: source and decision ID (for example `issue-35#decision-5`); the behavior in one falsifiable sentence; the owning task ID; an implementation or spec pointer as `path:line`; the exact verification command or the exact live observation that proves it; an evidence path or hash; status from `verified | missing | conflict | external-blocker | superseded`; and, for `superseded` or `external-blocker`, the explicit user approval or blocker disposition. A row missing any field is incomplete, not verified. Enumerate issue 35's settled acceptance constraints individually rather than as a summary paragraph; the ten earlier findings and this review's thirteen findings are inputs to the inventory, not the inventory.

**Interfaces T0 must pin before Wave 1 (amendment v2, finding F4).** The concept list below is not sufficient for parallel workers. T0 must additionally pin exact JSON schemas and exact Python/CLI signatures for: the dispatch packet (superseding `schemas/execution-packet.schema.json`'s ten generic fields and its `additionalProperties: true`) carrying `requirements_version`, `plan_version`, `routing_version`, `effective_config_hash`, `selection_disclosure`, session and family identity, allowed paths, and validation commands; `start_receipt`; `event_id` and monotonic `sequence`; the observed-status enum; terminal classification; monitor health; the replay cursor and acknowledgement; replacement authority; and the linkage from a dispatch to its landing and checkpoint. `scripts/office_spawn.sh` currently emits none of the version, disclosure, session-identity or event-cursor fields, so this is new surface, not documentation of existing surface.

**Scoring, trust and floor contracts T0 must pin (amendment v3).** Pin, in `docs/v3-runtime-contracts.md`: the derived-trust record (which recorded dispatch and outcome rows count toward `adapter_trust`, how a task shape is identified, what disqualifies a row, and the exact query); the per-role capability-floor declaration format and its evaluation semantics against a pinned catalog row, including what happens when a catalog field needed by the floor is unknown; the outcome-label vocabulary, its required evidence, and its relationship to the existing telemetry vocabulary; the derivation of `local_reward` from labeled rows; and the recorded-override record shape, which must carry user attribution, scope and expiry. Pin these as contracts only. T0 implements none of them.

**Two shared-file blocks T0 must pin verbatim (amendment v3).** Because one file may have only one owning task, T0 pins the exact text of two edits that a different task will consume: (a) the `roles.<role>.floor` block to be inserted into `config/config.default.yaml`, which T2 inserts verbatim and T2B consumes; and (b) the delegation shim in `scripts/office_runtime.py` by which `route()` calls the new routing module, which T2 applies verbatim and T2B implements behind. Both must be pinned precisely enough that T2 can apply them without understanding T2B's internals.

**Runtime commands T0 must pin for T4 (amendment v2, finding F6).** `scripts/office_runtime.py` has no landing, checkpoint, completion or family command today. T0 pins the exact CLI signature and exit semantics of every such command T4 will consume, so that T2 implements them and T4 only calls them.

Receipt: schema tests accept complete examples and reject missing identity/version/evidence; every pinned interface above has a committed schema or signature plus at least one accepting and one rejecting fixture; the acceptance matrix has no row missing a required field and no unexplained requirement. Any newly found material gap outside this plan becomes a versioned plan amendment, not silent additional scope.

### Wave 1 — parallel, disjoint foundations

Amendment v3 adds T2B here. Wave 1 write scopes remain disjoint: T1 owns instructions and specs, T2 owns `scripts/office_runtime.py`, `scripts/office_family.py`, `scripts/office_packets.py` and `config/config.default.yaml`, T2B owns `scripts/office_routing.py` and `scripts/office_scoring.py`, T3 owns the monitor and pane-hook files. No file has two owners.

**T1: Reconcile active instructions and portable intake.**
Depends on: T0.
Touches: `SKILL.md`, `protocol/*.md`, `skills/*/SKILL.md`, skill-local reference markdown, `references/OFFICE-SKILLS-V3-SPEC.md`, `references/OFFICE-SKILLS-V3-LIFECYCLE-SPEC.md`, `references/IMPLEMENTATION-NOTES.md`, `references/why-*.md`, new `tests/test_v3_instruction_contract.py`.

Replace obsolete authority rules with provisional orchestrator intake and interactive planner freeze; preserve executor disposition ownership and exceptional upline consultation. Define local/inline/integration review, amendment ownership, sticky focus and conditional compaction. Specify native Skill loading or complete direct-file loading with the same receipt; structured questions or batched plain text preserve intent coverage. No approval-hook enforcement requirement is reintroduced. Amendment v3: `protocol/routing.md`, `SKILL.md` and `skills/auto-routing/SKILL.md` currently describe the filter order without stating where each filter's input comes from. Update them so adapter trust, the capability floor and local reward are described as derived from recorded evidence, and so the recorded-override path is documented as the only way past a derived gate. Describe the semantics T0 pinned; do not invent different ones. Scope tests to critical contradictory instructions and contract examples, not prose snapshots.

Receipt: `python3 scripts/check_ecosystem.py` and `python3 -m pytest tests/test_v3_instruction_contract.py tests/test_budget.py -q` pass; targeted search finds no active contradictory planner prohibition or unconditional review escalation.

**T2: Implement family state, versions and scoped amendments.**
Depends on: T0, T1. (Amendment v2, finding F5: T1 owns the normative planner/authority semantics T2 implements, and T0 does not pin all of them, so T2 cannot run in parallel with T1 without implementing against a stale authority model.)
Touches: `scripts/office_runtime.py`, new `scripts/office_family.py` and `scripts/office_packets.py`, `config/config.default.yaml`, `tests/test_runtime.py`, new `tests/test_families.py`, new `tests/test_amendments.py`, new `tests/test_landings.py` for the command-level contract only.

Amendment v3: T2 additionally applies, verbatim and without redesign, the two shared-file blocks T0 pinned — the `roles.<role>.floor` block into `config/config.default.yaml`, and the `route()` delegation shim into `scripts/office_runtime.py`. T2 owns both files; T2B owns neither and must never edit them. If a pinned block does not apply cleanly, that is a PLAN DEFECT returned to T0's contract, not an improvised edit.

Amendment v2, finding F6: T2 also implements and tests every landing, checkpoint, completion and family command that T0 pinned for T4, because T2 is the sole owner of `scripts/office_runtime.py` and `scripts/office_packets.py`. T4 consumes these commands and must not need to edit either module; if T4 discovers a missing command, that is a plan defect returned to T2, not an out-of-scope edit by T4.

Reuse atomic writes, leases, hashing, state reconciliation and config layering. Add family/session tiers with precedence: dispatch > family/project > repo > session > user > default. Implement durable focus and scoped delta application. Provide advisory current/projected resource-demand summaries; do not block unrelated families for optimization. Integrate version changes with existing monotonic phase and approval validation instead of bypassing those checks. Legacy state must load read-only or migrate explicitly with a backup and recorded version defaults; never silently certify stale packets or grant ownership.

Receipt: tests prove routing-only deltas leave requirements/plan approval intact, stale concurrent deltas fail without partial writes, contract changes invalidate only affected work, ambiguous focus mutates nothing, two sessions do not collide, and restart reconstructs active families. Legacy state preserves unknown fields and evidence.

Additional receipt, amendment v2, finding F7 — sticky focus and soft projection. Using two concurrent families A and B, assert: an unqualified command mutates the current focus family only; a named command mutates the named family and, per issue 77, moves focus; an ambiguous command mutates nothing and says why; an explicitly global command applies to both. With finite quota figures, a projected collision emits a warning receipt while leaving unrelated families runnable. Naming two families in an integration test (T5) does not discharge this.

Additional receipt, amendment v2, finding F8 — amendment transition matrix. Assert as an explicit matrix, per issue 35 decision 5: a routing-only delta does not wake the planner; a requirements delta that still fits the plan does not wake the planner; a plan-contract delta wakes the planner and pauses only affected scopes; a running dispatch completes its current atomic unit rather than being interrupted, unless explicitly replaced. Each cell asserts the resulting version numbers and the recorded event, not merely a non-zero exit. Bumping every version on every delta must fail the matrix.

**T2B: Derive trust, capability floors and rewards from recorded evidence.**
Depends on: T0. Runs in parallel with T2 and T3; its write scope is disjoint from both.
Touches: new `scripts/office_routing.py`, new `scripts/office_scoring.py`, new `tests/test_scoring.py`, new `tests/test_derived_routing.py`.

Amendment v3. `route()` currently decides every dispatch from four caller-supplied values — `adapter_state` at `scripts/office_runtime.py:212`, `absolute_floor_pass` at `:229`, `advisory_pass` at `:272` and `local_reward` at `:282`. An orchestrator can pass any of them, so the trust gate constrains only an agent that chooses to be constrained. Move `route()` into `scripts/office_routing.py` behind the delegation shim T2 applies, and make those four values derived:

- **Adapter trust.** Compute from recorded dispatches and outcome labels against `adapter_trust.proven_min_successful_dispatches` and `min_task_shapes`, plus the absence of an unresolved adapter-attributed critical failure. Today nothing reads either constant, so `proven` is unreachable by any code path and no adapter can ever graduate. A caller-supplied `adapter_state` must be ignored, not trusted.
- **Capability floor.** Evaluate the declared per-role floor from `config/config.default.yaml` against the candidate's pinned catalog row. A caller-supplied `absolute_floor_pass` must be ignored. When a catalog field the floor needs is unknown, fail closed and say which field was missing; never treat unknown as passing.
- **Local reward.** Derive from labeled outcome rows for the exact routable triple. Absent labels, it is unknown and contributes nothing, rather than defaulting to zero as though the evidence were neutral.
- **Override.** An explicit override of a derived gate is permitted but must be recorded with user attribution, the scope it covers and its expiry, and must surface in `selection_disclosure`. An override present in the request without a recorded authorization is a hard stop.

Never lower a floor for cost or quota. Do not change any numeric value in `config/config.default.yaml`, `maturity_age()`'s curve, or the reward model's shape; parametric change is out of scope per the boundaries above.

Receipt: a candidate asserting `adapter_state: proven`, `absolute_floor_pass: true` or a high `local_reward` in the request is routed exactly as if it had asserted nothing, proven by a test that passes the assertion and asserts the rejection reason, not merely a non-zero exit. Given seeded dispatch and label rows meeting the configured bar, an adapter reaches `proven` and becomes routable for a mutable role; one row short, it does not. A floor whose required catalog field is unknown fails closed and names the field. An unknown reward does not rank as equal to a measured zero. An override without recorded authorization is rejected; with authorization it is honoured and appears in `selection_disclosure`. `replay` over a seeded dataset shows no decision flips attributable to this task, since this task changes where values come from and not what the policy does with them.

**T3: Implement Herdr event delivery and safe reclamation.**
Depends on: T0.
Touches: new `scripts/office_monitor.py`, `scripts/office_liveness.sh`, `scripts/hooks/close_finished_panes.mjs`, new `tests/test_monitor.py`.

Discover the actual available completion-delivery mechanism for each required orchestrator harness before choosing its binding. Reuse available capabilities; add a background bridge only where needed. Support finish, idle, blocked, unknown and disappeared observations, start receipt, durable event replay and monitor health. Distinguish reported done from verified completion; preserve blocked/unknown panes and require independent evidence before declaring death. Explicitly handle the previously observed agy false-done case.

Receipt: deterministic harness fakes demonstrate event delivery, duplicate suppression, reconnect/restart replay, monitor loss visibility, concurrent family isolation and no closure on false done. A background process merely producing a file is not sufficient acceptance for waking the orchestrator; live proof follows in T6.

Additional receipt, amendment v2, finding F9 — the agy false-done known-bad case. `scripts/hooks/close_finished_panes.mjs:50` places `done` in its `FINISHED` set and closes on it, which is exactly the behavior issue 93's amendment recorded agy triggering while still working. A generic "no closure on false done" assertion does not exercise this. Build a fixture carrying agy's actual observed status sequence and payload together with evidence the pane is still active (advancing output, live session), and assert the hook closes nothing until a durable terminal event plus independent completion evidence both exist. T6 must reproduce this same known-bad case against live agy.

### Wave 2 — integrate review and packet consumers

**T4: Wire real review, landings, checkpoints and dispatch.**
Depends on: T1, T2, T3.
Touches: `scripts/review_loop.sh`, `scripts/review_finding.sh`, `scripts/office_readback.sh`, `scripts/office_spawn.sh`, `scripts/hooks/pre_compact.sh`, `scripts/hooks/compact_advisor.sh`, `scripts/hooks/session_end.sh`, `scripts/hooks/install_hooks.sh` only if optional lifecycle binding needs it and only as a repository-file edit (amendment v2, finding F12: this run may edit that script but must never execute it, because `install_hooks.sh:154-179` writes `~/.claude`, `~/.codex` and `~/.gemini` and lines 189-195 write Hermes profiles, all of which are protected user-global paths; hook tests run against a temporary `HOME` and temporary config roots), `tests/test_review.py`, `tests/test_hooks.py`, new `tests/test_landings.py`, and new `scripts/pr_report.py` with new `tests/test_pr_report.py` (amendment v4).

Amendment v4: T4 additionally owns the issue-35 PR-report requirement, which no task previously owned. A deterministic script emits a compact before/after row per PR — shipped size, estimated and actual loaded tokens where available, eval delta, reward delta — comparing each PR both to the immediately previous version and to a fixed version baseline, with no agent-generated narrative. Determinism is the receipt: the same inputs must produce byte-identical output across two runs, asserted by test rather than asserted in prose.

Connect to actual independent review evidence or retire unused mock behavior according to T0. Never synthesize PASS from an unset environment variable. Local producers own findings; consultation routes unresolved disagreement upward without routine orchestrator override. Integration review is triggered by actual dependent/merging landings, not count alone. Readback retains process diagnostics while validating semantic landing evidence. Serialize checkpoints before requested compaction and resume the same role from the packet. Wire monitor lifecycle into dispatch and closeout; keep optional hooks optional. Amendment v3: closeout writes one outcome label per dispatch from the validation and review evidence it already holds, using the vocabulary T0 pinned, and `auto-maintenance` revises a label later when a post-merge defect surfaces. Nothing writes `outcome_labels` today, which is why every scoring path reads an empty table. A label must cite its evidence; a dispatch with no evidence is recorded as unlabeled rather than as a success.

Receipt: unset review source yields explicit unavailable/required review status, never independent PASS; stale tree/version or producer-as-reviewer evidence is rejected; inline paths remain valid.

Additional receipt, amendment v2, finding F3 — positive-path provenance. Negative-path coverage alone is insufficient while `scripts/review_loop.sh:52-56` defaults its mock reviewer to `PASS` and `scripts/review_finding.sh:40-55` converts any supplied summary into evidence with a fixed hash. A recorded independent-review PASS must be reachable only from a validated reviewer dispatch and readback bound to: producer identity not equal to reviewer identity; the current tree SHA; all three version numbers; the declared review scope; non-empty evidence; and the reviewer's routed identity. An unset, synthetic or unbound source must make PASS unavailable, never successful. Assert this on the success path, not only on the failure path. Single executor and independent parallel changes avoid extra final review; dependent landings receive it. Valid evidence-backed refutation lands; unresolved consultation stays unresolved. Exit 0 alone cannot produce a completed landing. Restart preserves checkpoint fields and review round.

### Wave 3 — full acceptance, self-improvement, and real portability

**T5: Integrated regression and coverage verification.**
Depends on: T4, T2B.
Touches: `tests/test_integration.py`, `tests/test_dogfood.py`, `docs/v3-acceptance.md`.

Amendment v2, finding F11: `.github/workflows/validate.yml` is removed from this task's write scope and is a protected path for the whole run. It defines the required validation job, and the account's billing lock already prevents that job from running, so an executor holding write access to it could weaken or route around the very gate that is blocked. Any CI change requires a separate plan amendment and explicit user authority, and must never weaken, bypass or alter billing-related enforcement.

Run the complete workflow on isolated fixtures: interactive requirements freeze, two concurrent families, local review, cross-scope integration review, routing-only delta, plan-contract delta, checkpoint/restart and event-driven completion. Assert reasons and state changes, not just exit codes. Verify the amendment-v3 derived-routing path end to end: a run that dispatches, labels at closeout, and on a later run routes differently because of what it recorded. Verify existing adaptive routing, quota floors, pinned snapshots, private telemetry, replay gates, proposal isolation and retirement coverage from T0 remain intact.

Receipt: `python3 scripts/check_ecosystem.py`; `python3 -m pytest tests/ -q`; every command in `.github/workflows/validate.yml` run locally against an unmodified workflow file; `git diff --check`. Capture exact tested head and concise results. Do not weaken checks to make the plan fit. The measured baseline to beat is the `213ba56` reading recorded in the planning envelope: ecosystem PASS, 106 tests plus 8 subtests passing. A lower test count at the final head is a regression to explain, not a new baseline.

**T6: Demonstrate each required orchestrator harness.**
Depends on: T5.
Touches: `docs/v3-portability-evidence.md`, sanitized evidence links in `docs/v3-acceptance.md`; live raw evidence stays outside the repository.

Run a bounded scratch-repository scenario under Claude, Codex and agy as orchestrators, not just workers. Each must load/mark a spoke, ask/receive user input through its supported path, dispatch via Herdr, remain responsive while work runs, receive terminal events, validate a landing, and restore state after a checkpoint. Exercise an interruption/blocked outcome without losing the pane. Reuse deterministic fakes for destructive/failure cases; never stop unrelated live workers. Record harness/model/version, invocation, commit, event/receipt IDs and outcome without private transcript content.

Receipt, amendment v2, finding F2. "Three passing lifecycle records" is a claim, not a receipt: an executor can author three passing JSON files. Each of the three harnesses produces a record containing all of: the resolved executable path and its `--version` output; the exact invocation including model and effort, with effort read back from the harness's own banner rather than from the command that was typed; the spoke `check-spoke`/`mark-spoke` receipt; the actual question asked and the answer received through that harness's supported user-input path; the Herdr pane, agent and session IDs; the start-receipt ID and the terminal event ID with its sequence number; the checkpoint written and the state restored after restart, shown as a before/after comparison; the landing-validation command and its output; and timestamps plus content hashes for each evidence artifact. An independent reviewer verifies these against the durable event store in T7. Deterministic fakes are permitted only for destructive and failure cases and are prohibited on the core success path; a record whose success path is backed by a fake fails this task. The agy false-done case from T3 is reproduced here against live agy. Missing credentials, missing delivery capability or unavailable user interaction is a reported acceptance blocker, never replaced by an adapter unit test or invented answer. Targeted repairs return to the owning task and rerun affected checks.

**T5S: Demonstrate one end-to-end self-improvement cycle.**
Depends on: T5.
Touches: new `docs/v3-self-improvement-evidence.md`, `docs/v3-acceptance.md` rows owned by this task, the graduation-bar artifact under `references/` or `config/`; the proposal itself lands in an isolated branch or worktree and never in this branch's history.

Amendment v2, finding F1. Issue 35's destination requires the loop to propose its own improvements to a standing PR against a graduation bar, and no task owned that. T5 verifying "proposal isolation" checks the guard rail, not the outcome, and `references/IMPLEMENTATION-NOTES.md:37-39` records PR creation as orchestration work rather than implemented behavior. Run one complete cycle through `skills/auto-self-improve`: compile a sanitized dream from this run's own private telemetry, produce a proposal in an isolated branch or worktree, append it to the standing PR or branch with recorded lineage, prove the append is idempotent when replayed, obtain independent review of the proposal, and stop at the maintainer-merge boundary without merging. Commit the graduation-bar artifact that decides when a proposal is eligible.

Receipt: privacy lint passes on the public proposal with no raw transcript, credential, personal-configuration or absolute-path leakage; evidence hashes are opaque; lineage links the proposal to this run without exposing it; replaying the same dream appends nothing new; an independent reviewer accepted or rejected the proposal on evidence; the proposal remains unmerged and the merge boundary is intact; the graduation bar is committed and referenced from the acceptance matrix. A proposal that only passes lint but was never actually appended to the standing PR or branch does not satisfy this task.

### Wave 4 — independent review, PR readiness and final merge

**T7: Review and close the coverage gaps.**
Depends on: T6, T5S.
Touches: `docs/v3-acceptance.md`, `docs/v3-portability-evidence.md`, PR/issue metadata; fixes return to the original file owners.

Have an independent reviewer inspect the frozen requirements, final diff, acceptance matrix, negative controls and real-harness receipts. Review shared-state races, version migration, false review success, lost completion events and unaffected-family behavior. Follow executor-owned disposition with exceptional consultation. Update the PR description to describe final implemented behavior and remove obsolete gap claims. Reconcile map/ticket summaries with authoritative comments without declaring success before proof.

External blocker: the observed GitHub Validate job did not start because the account is locked for billing. The account owner must resolve this; do not alter billing or bypass CI. After restoration, run CI at the final head. Local passes do not erase the external blocker.

Receipt: accepted review dispositions, complete acceptance matrix and green current-head checks; PR ready. All material unresolved items remain visible and block a completeness claim.

**T8: Merge and verify main.**
Depends on: T7 and explicit merge authority.
Touches: integration branch/main through PR merge; linked issue/map state; no unrelated code.

Refresh main and PR head. If main advanced, integrate it and repeat affected verification plus required full checks before merge. Use repository merge convention without force/admin bypass. Preserve private run state and avoid broad branch/worktree cleanup. After merge, verify the merge landed exactly the reviewed tree and nothing else, run ecosystem and test smoke checks on a clean main checkout, and record the PR/merge receipt.

Amendment v2, finding F10: containment is too weak a test, because it admits unreviewed additions, silent omissions and rewritten content. Record the reviewed PR head SHA, the approved integration tree hash, the merge SHA and the resulting main tree hash, and assert that the diff from the recorded pre-merge main to the post-merge main equals the approved diff exactly. Any inequality, any advancement of main after review, and any extra change all require re-review before merge rather than an explanation after it. Close covered issues and map only once their acceptance is demonstrated; retain any explicitly deferred item with approved scope and a durable pointer.

## Rollback and amendments

Keep implementation in reviewable commits grouped by wave. Record pre-migration state backup and schema versions in private run storage; restore only that test/run scope if migration fails. Before merge, revert the specific faulty wave commit and correct its owning task; do not reset shared worktrees. After merge, use a reviewed revert PR for a demonstrated regression, preserving telemetry and checkpoint evidence. Do not automatically delete state or revert unrelated work.

An accepted PLAN DEFECT must identify the contradicted assumption and evidence. Increment this plan version, update only affected contracts/tasks, invalidate their stale execution packets, and resume after the required review. Known external blockers do not justify lowering acceptance or inventing new requirements.

## Planning verification

Baseline inspected: runtime command/phase and config surfaces, current schemas/tests, review helper, authoritative issue amendments and PR status. Historical baseline was 106 tests plus 8 subtests passing; this plan makes no claim that implementation or current-head portability checks have run. Version 1 dispatched no agents or execution packets.

Version 2 adds: an independent plan review at `213ba56` (CHANGES REQUIRED, thirteen findings, all
accepted after the orchestrator independently verified each finding's cited evidence in source);
a re-measured baseline at `213ba56` (ecosystem PASS; 106 tests plus 8 subtests passing); and
confirmation that every file T0 through T6 is asked to create is currently absent, so this is a
from-scratch implementation rather than a reconcile. `origin/main` is unchanged at the recorded
`observed_main_sha`, and PR #99 is open, draft and mergeable. The amendment makes no claim that
any implementation task or portability check has run.
