# office-skills v3 — Adaptive Office Runtime Specification

**Status:** Proposed rewrite  
**Scope:** `auto-office` v3  
**Supersedes:** the design intent of the existing v3 Wayfinder map if this document is ratified  
**Primary objective:** make `auto-office` a durable, portable, self-improving orchestration runtime that can adapt routing as models, harnesses, pricing, quota conditions, and local evidence change — without requiring the maintainer to hand-edit routing tables for normal ecosystem churn.

---

## 1. Product statement

`/auto-office` runs the complete office engineering lifecycle from intent through planning, execution, verification, review, and closeout.

The system must:

1. route each role to an appropriate `harness × model × effort` triple;
2. preserve quality floors before optimizing for cost;
3. account for real local quota pressure, not only public API price;
4. keep model/catalog data fresh without using the network at route time;
5. learn from local outcomes without leaking private project information;
6. make every role portable across sessions and agents;
7. keep human merge-to-`main` as the permanent authority boundary;
8. improve itself through proposals that are isolated from the currently running policy;
9. remain reproducible: every routing decision can be reconstructed from immutable snapshots and recorded state;
10. degrade safely when data, quota, tools, or adapters are incomplete.

The system is successful when ordinary model releases, quota changes, and accumulated local evidence can alter future routing without requiring the maintainer to rewrite the plugin by hand.

---

## 2. Non-goals

v3 does **not**:

- auto-merge changes to `main`;
- assume public benchmark rank is equivalent to local usefulness;
- treat "merged" as proof of correctness;
- use model-generated prose as authoritative runtime state;
- require an always-running daemon;
- scrape arbitrary CLI registries to discover new agent harnesses;
- make unverified harness integrations fully trusted for mutable work;
- allow an agent to silently change the routing policy that is governing its own current run;
- make destructive production changes merely because the orchestration runtime can technically perform them.

---

## 3. Governing principles

### 3.1 Quality is a constraint; cost is an optimization

The router must first eliminate candidates that fail the role contract.

Only after capability, trust, safety, and quota constraints are satisfied may cost influence selection.

No cost optimization may lower an absolute capability or safety floor.

### 3.2 Local evidence outranks public priors

Public benchmark data is cold-start evidence.

Local evidence for the exact routable triple:

`harness@version × model × effort`

outranks generic leaderboard evidence when enough comparable local evidence exists.

### 3.3 The runtime policy is immutable for a run

Every run pins the exact policy state it began with.

A self-improvement branch may be modified while a run is active, but the running process must never begin executing those unmerged changes implicitly.

### 3.4 Private evidence stays private

Raw run telemetry, real repository identities, hosts, organizations, people, local paths, credentials, and other project-specific material remain local.

Only sanitized derived patterns may be proposed to the public repository.

### 3.5 Durable artifacts outrank conversation state

No role is allowed to depend on the original chat transcript for correctness.

A fresh agent must be able to reconstruct an in-flight run from durable state.

---

## 4. Terminology

### 4.1 Role

A lifecycle responsibility, initially:

- orchestrator
- planner
- plan reviewer
- executor
- worker
- code reviewer
- browser verifier
- closeout verifier

### 4.2 Harness

The execution environment or coding-agent CLI that hosts a model, such as a supported coding CLI or agent runtime.

A harness is adapter data, not a separate office lifecycle implementation.

### 4.3 Model identity

A model has one underlying canonical `model_id`.

The same model exposed through two harnesses shares that `model_id`.

Runtime evidence remains harness-specific.

Therefore:

- **benchmark identity:** `model_id × effort`
- **routable identity:** `harness@version × model_id × effort`

Two harnesses exposing the same model share public priors where appropriate, but never share runtime reliability, quota, dispatch-form, or adapter-trust evidence automatically.

### 4.4 Gear

A funding/intensity preset over the fixed lifecycle.

Built-in presets may include:

- `direct`
- `direct+review`
- `light`
- `quick`
- `express`
- `full`

These are configuration presets, not a closed plugin enum. Users may define additional presets.

### 4.5 Playbook

Task-shape procedure applied independently from gear and route.

Initial playbooks:

- Change
- Restructure
- Investigate
- Prototype
- Visual

The runtime decision is therefore:

`gear × playbook × route`

### 4.6 Family

One coherent implementation work unit, normally an issue or equivalent scoped change, including its plan, packets, role holders, Git state, review state, and validation evidence.

---

## 5. Runtime lifecycle

The lifecycle order is fixed. Gears fund or omit optional stages; they do not reorder the lifecycle.

1. Capture provisional user intent and scope.
2. Interactive planner discovery and requirements freeze (§6.2).
3. Establish repository/runtime baseline.
4. Classify task shape and risk.
5. Resolve unresolved product decisions.
6. Produce or refresh the implementation plan.
7. Review the plan when required by gear/risk.
8. Generate machine-checkable execution packets.
9. Route and dispatch executors/workers.
10. Self-verify changed work.
11. Run independent review when funded or required.
12. Perform browser/runtime verification when the acceptance path is user-facing.
13. Reconcile findings and amendments.
14. Run closeout checks.
15. Record telemetry and durable run state.
16. Perform lazy maintenance on eligible historical rows.
17. Optionally create isolated improvement proposals.

The lifecycle itself is not user-configurable.

Every lifecycle phase that is entered has a self-review checkpoint before advancement. The phase
owner checks the objective, done criteria, current state, evidence, and residual risks or
decisions, and records a disposition to proceed, amend, or stop. This includes phases that are
inline or optional when they are entered; an omitted optional phase is covered by self-review of
the omission decision. The receipt and evidence floor are defined in the lifecycle spec §8.0.

---

## 6. Role authority model

### 6.1 Orchestrator

The orchestrator owns:

- provisional user intent and scope, captured at kickoff as a hypothesis for the planner, not a frozen contract (issue-35#decision-1);
- explicit user choices;
- gear;
- playbook;
- route approval;
- all lifecycle gates;
- dispatch coordination;
- plan acceptance or rejection;
- the family registry and sticky conversational focus across concurrent families (§6.6);
- amendment classification for routing/requirements/plan-contract deltas (§6.6);
- final escalation to the user.

The orchestrator does not run the requirements interview and does not freeze the five execution fields itself — see §6.2. The orchestrator is the user's entry model. v3 may advise that another model/harness would be a better orchestrator, but it does not silently replace the selected entry model.

The orchestrator is still scored.

Every serialized plan declares the orchestrator's model assignment: canonical `model_id`, exact invocation model identifier when the entry harness exposes one, effort, harness/version, and why the entry model owns the role.

### 6.2 Planner

The planner owns **what**, up to freeze, as well as **how** (issue-35#decision-1, issue-77 §2 — this supersedes issue-47's rule that the planner never talks to the user and owns only implementation).

The planner:

- performs repository/runtime reconnaissance before interviewing;
- interacts directly with the user — including through a Herdr pane when available — running the twelve-item interview (`skills/auto-intake/SKILL.md`) as one or two batched question rounds, or a structured-question tool where the harness has one;
- may reshape goal, scope, done criteria, blast radius, named actions, non-goals, interfaces, and milestones when repository evidence shows the orchestrator's provisional framing was wrong; the provisional intent is a hypothesis, not an immutable contract;
- freezes the five execution fields only at the end of this interactive discovery, never before it;
- does not silently change frozen requirements after freeze;
- may be inline or separately routed;
- self-reviews the plan and runs its own plan adversary before returning it;
- emits a serialized plan artifact carrying the frozen requirements;
- must revise the plan when an accepted `PLAN DEFECT` invalidates an assumption.

The serialized plan contains a `model_assignments` block for both orchestrator and planner. Each entry records canonical `model_id`, exact invocation model identifier when available, effort, harness/version, selection rationale, and whether the planner is inline or separately routed. Sharing one session never makes either declaration implicit.

Default planner preference:

1. Opus Medium
2. Astra Low fallback
3. normal advisory-floor router thereafter

Local evidence may supersede these seed preferences.

### 6.3 Reviewers

A reviewer is never allowed to self-approve work from the same role session that produced it.

For non-trivial mutable work, reviewers should run in a different session from the producer whenever the harness permits it.

Reviewer defaults may prefer Luna XHigh where local evidence supports that choice.

### 6.4 Executor and worker

Executors and workers are selected by task shape plus current routing evidence, not by one static brand default.

Builder roles are subject to hard capability floors.

The executor owns disposition of its own review findings (issue-35#decision-3, issue-77 §4): it may accept a finding and fix it, or reject it with stronger evidence. It consults the orchestrator only exceptionally, when it and its reviewer cannot responsibly resolve a disagreement; this is not a mandatory approval chain. Ordinarily the orchestrator manages requirements, decision-passing, and routing rather than technical disagreements. If evidence is genuinely conflicting, the executor emits a structured `TRUE_CONFLICT`/`USER_DECISION_REQUIRED` state and the orchestrator surfaces it to the user rather than acting as a higher-tier technical judge.

### 6.5 Browser verifier

The browser verifier independently validates the user-observable acceptance path.

It may be a subagent or separate process according to config and gear, but browser verification itself is required whenever the acceptance criteria materially depend on rendered or interactive behavior and a reachable runtime can reasonably be produced.

### 6.6 Families, amendments, and compaction

One orchestrator may supervise multiple independent families concurrently through a durable family registry (`schemas/family-registry.schema.json`); families run independently by default with sticky conversational focus — an unqualified command targets the current focus family, naming another switches focus, an explicit global command applies to all, and a genuinely ambiguous command mutates nothing (issue-35#decision-6, issue-77).

`requirements_version`, `plan_version`, and `routing_version` amend independently (issue-35#decision-5): a routing-only delta never wakes the planner; a requirements delta that still fits the plan versions requirements and sends a delta packet without waking the planner; a plan-contract delta pauses only affected scopes and wakes the planner for interactive delta-planning. A running dispatch finishes its current atomic unit before a new route or plan applies unless the user explicitly requests immediate replacement.

Long-lived roles may compact at semantic phase boundaries after first serializing the state the next phase needs — most commonly an executor checkpointing before an adversarial reviewer is launched (issue-77 addendum); this is conditional, not mandatory for inline review or small tasks. Full detail and the routing-precedence order live in `protocol/families-and-amendments.md`.

---

## 7. Portable run state

Every role handoff uses a shared base envelope.

Minimum envelope:

```yaml
run_id: <uuid>
family_id: <stable-id>
dispatch_id: <uuid>
role: <role>
holder_id: <session-or-agent-id>
triple: <harness@version/model@effort>
mode: <gear>
playbook: <playbook>
base_sha: <git-sha>
policy_hash: <sha256>
catalog_snapshot_hash: <sha256>
adapter_snapshot_hash: <sha256>
effective_config_hash: <sha256>
requirements_version: <integer>  # optional: not yet decided at kickoff
plan_version: <integer>
routing_version: <integer>  # optional: not yet decided at kickoff
packet_version: <integer>
created_at: <iso8601>
```

Role-specific payloads are attached to this envelope.

Each routed envelope also carries the router's `selection_disclosure`: role, canonical model ID, exact invocation model identifier when available, effort, harness/version, selected triple, and evidence-backed reason. Before executor or reviewer invocation, the orchestrator surfaces that disclosure to the user; dispatch readback preserves it.

Examples:

- orchestrator: provisional intent;
- planner: frozen fields (from its own interview with the user) + plan artifact;
- executor: task scope, blast radius, allowed mutations, protected paths, validation commands, self-review requirements;
- reviewer: round number, prior findings, disposition state;
- verifier: acceptance criteria and exact runtime checks.

Missing or contradictory mandatory fields reject the packet before dispatch.

---

## 8. Pause, resume, and takeover

A fresh agent may resume a family without the original transcript.

Before continuation, it must reconcile:

1. family registry;
2. current plan and packet versions;
3. Git branch, worktree, base SHA, head SHA, and uncommitted state;
4. issue/PR state;
5. pending review findings and dispositions;
6. validation evidence;
7. currently held role/writer leases;
8. pinned policy/catalog/adapter/config hashes.

### 8.1 Ownership

A mutable role has one active holder for its write scope.

Takeover is a holder change, not conversational continuation.

The new holder must acquire the role lease before writing.

After acquisition, the previous holder has no authority to mutate that role/scope.

### 8.2 Stale state

A takeover must explicitly reconcile:

- stale packets;
- stale plans;
- killed dispatches;
- incomplete atomic edits;
- pending reviewers;
- uncommitted changes;
- changed external dependencies;
- changed base branch state.

No stale state is implicitly trusted.

### 8.3 Long pauses

If the pinned model catalog, harness version, base SHA, or external dependency boundary changed materially during the pause, the resuming agent must re-run the minimum reconnaissance and verification needed to establish that the plan is still valid.

---

## 9. Model catalog architecture

v3 uses two catalog layers.

### 9.1 Shipped seed catalog

Committed to the plugin repository.

Contains machine-agnostic:

`model_id × effort`

data, including:

- source identifier;
- source name;
- canonical effort;
- source effort label;
- effort confidence;
- benchmark indexes;
- price fields;
- speed fields;
- release date;
- source snapshot metadata;
- content hash;
- supersession metadata.

This seed lets the plugin operate immediately after install.

### 9.2 User-local immutable refresh snapshots

Stored privately outside the repository, for example:

`~/.local/share/auto-office/catalog/<content_hash>.yaml`

A refresh may use network access.

Routing never does.

Each successful refresh:

1. fetches source data;
2. validates schema;
3. normalizes effort;
4. writes a new immutable snapshot;
5. writes/updates local harness bindings;
6. records the snapshot hash;
7. leaves the previous snapshot untouched.

A run pins one snapshot hash at dispatch time.

### 9.3 Refresh trigger

Two independent triggers exist:

1. **Harness trigger:** local CLI fingerprint changed.
2. **Catalog trigger:** local catalog snapshot older than 72 hours.

Refresh is non-blocking for the current run unless the current run explicitly requires a newly discovered model.

If refresh fails, routing continues from the last valid snapshot and records staleness.

### 9.4 Publishing refreshed catalog data

Local freshness must not depend on waiting for a public repository merge.

Therefore runtime routing uses the user-local snapshot immediately.

Separately, a catalog promotion job may propose a repository PR that updates the shipped seed.

Catalog promotion PRs are distinct from learned-pattern self-heal PRs.

The maintainer merges them manually.

---

## 10. Effort normalization

Canonical sparse ladder:

```text
none | low | medium | high | xhigh | max
```

A model need not expose every rung.

Effort is parsed from authoritative source metadata, not guessed from a bare slug.

Each row records:

```yaml
effort: <canonical>
source_effort: <verbatim>
effort_confidence: exact | mapped | unknown
```

Unknown effort is stored but is not routable.

Vendor effort labels that do not map cleanly remain unroutable until explicitly mapped.

Effort is first-class. No formula assumes that effort levels are monotonic across models or harnesses.

---

## 11. Harness discovery

### 11.1 CLI axis

The harness list is maintained as a seed adapter list.

There is no attempt to discover all coding-agent CLIs from public package registries.

Installed harnesses are detected locally through PATH and adapter-specific fingerprints.

### 11.2 Model axis

The model axis is dynamic.

An adapter declares how its available model set is obtained:

```text
cli
manifest-url
package-file
static
catalog-plus-probe
```

`catalog-plus-probe` is allowed for harnesses that cannot list supported models locally.

### 11.3 Unconfirmed releases

If the public catalog discovers a new model but the local harness cannot prove it supports that model, the binding is:

`discovered-unconfirmed`

It may not receive normal mutable routing until one of these occurs:

- adapter metadata is updated;
- a safe capability probe succeeds;
- the user explicitly selects it;
- successful sandbox/reversible exploration produces qualifying local evidence.

This prevents a vendor catalog release from being mistaken for local CLI support.

---

## 12. Harness adapter contract

A valid adapter declares:

```yaml
id:
verified_state:
version_fingerprint:
model_source:
effort_mapping:
benchmark_slug_mapping:
invocation:
safe_prompt_passing:
dispatch_forms:
trusted_for:
quota_probe:
shallow_review:
agentic_capability:
failure_signatures:
conformance:
```

### 12.1 Mandatory semantics

The adapter must define:

- version/fingerprint logic;
- model source;
- effort mapping;
- model-to-benchmark mapping;
- invocation argv shape;
- safe prompt transport;
- supported dispatch forms;
- trust by evidence capability;
- quota probe behavior;
- model-level agentic capability or equivalent builder evidence;
- known machine-detectable failure signatures.

Unknown mandatory semantics make the affected triple unroutable.

### 12.2 Adapter trust states

Adapters use three states:

```text
invalid
valid-unverified
proven
```

#### invalid

Fails deterministic contract validation.

Never routable.

#### valid-unverified

Passes schema and conformance checks but lacks enough real runtime evidence.

Allowed by default for:

- model discovery;
- read-only investigation;
- prototypes;
- reversible sandbox workers;
- adapter conformance runs.

Not allowed by default for:

- mutable executor authority;
- final code review;
- destructive actions;
- production-facing browser verification.

A user may explicitly override this restriction.

#### proven

Reached only through an explicit recorded trust act; never computed from a dispatch count or query (amendment v5, made executable by v6).

Eligible for normal routing subject to role floors and quota.

### 12.3 Trust only falls automatically; it never rises without a recorded act

A query may lower adapter trust and may never raise it. Any unresolved adapter-attributed critical failure — an evidence-backed `recurrence_failure`/`material_post_merge_defect` label, or an `abandoned` dispatch carrying an accepted-material critical/high finding — quarantines the triple automatically from recorded dispatch/outcome-label evidence, with no human action, and that failure evidence latches: no later label of any kind, on the same dispatch, retracts it.

The floor absent any explicit trust act is `valid-unverified`, never `proven`, regardless of accumulated successful dispatches. The only way trust ever rises — `quarantined` → `valid-unverified`, or anything → `proven` — is an explicit `adapter_trust_acts` record carrying its own `actor_id` and a substantive reason; it is never inferred, backfilled, or computed from `outcome_labels`, `validations`, or any count over them.

`adapter_trust.proven_min_successful_dispatches` (5) and `proven_min_task_shapes` (2) remain in `config/config.default.yaml` as advisory reference numbers an actor may consult before recording a `proven` act. No query binds them, and that nothing reads them automatically is correct, not an unimplemented gap. `tests/test_trust_conformance.py` is the normative artifact for every property in this section; no SQL in this document is normative.

---

## 13. Capability floors

A floor is a compound role contract:

```text
benchmark anchor
+ required capabilities
+ adapter trust
+ hard exclusions
+ task-shape requirements
```

### 13.1 Absolute vs advisory floors

Every role has an **absolute competence floor**.

This floor is never waived by a gear preset.

Roles may also have a stronger **recommended quality anchor**.

The recommended anchor is advisory and may be undercut for cost when:

- the absolute floor still passes;
- the undercut is recorded;
- the current gear allows it.

### 13.2 Builder roles

Executor/worker eligibility may use:

- public intelligence/coding/agentic indexes where available;
- harness-specific coding-agent evidence;
- local proven runtime evidence;
- adapter-declared agentic capability only as a cold-start fallback.

Local negative evidence can hard-exclude a triple even when public scores are strong.

### 13.3 Router filter order

The router evaluates in this order:

1. explicit hard exclusions;
2. adapter validity and trust;
3. required capabilities;
4. absolute role floor;
5. task-shape requirements;
6. quota safety;
7. preferred/advisory quality anchor;
8. cost;
9. local tie-break evidence.

**Where each filter's input comes from (amendment v3, narrowed by v5/v6).** Stages 2, 4, and 9 consume values `route()` derives itself, never a caller-supplied `adapter_state`, `absolute_floor_pass`, or `local_reward` — a request asserting any of those three is routed exactly as if it had asserted nothing:

- Stage 2 (adapter trust): derived downward-only from recorded dispatch/outcome-label evidence (§12.3). Upward movement requires an explicit recorded trust act.
- Stage 4 (absolute role floor): evaluated against the candidate's pinned catalog row against the declared `roles.<role>.floor`; an unknown required catalog field fails closed and names the field.
- Stage 9 (local tie-break evidence): derived from labeled outcome rows for the exact routable triple; an unmeasured reward is `None`, never a measured zero.

An explicit override of stage 2, 4, or 7 is honoured only with a valid, unexpired `RecordedOverride` carrying user attribution, scope, and expiry, surfaced in `selection_disclosure`; an override asserted without one is a hard stop. See `docs/v3-runtime-contracts.md` §7 for the full contract and `tests/test_trust_conformance.py` for the normative trust properties.

---

## 14. User configuration

Precedence:

```text
prompt/CLI override
> repo config
> user config
> plugin default
```

Recommended locations:

```text
repo: .auto-office/config.yaml
user: ~/.config/auto-office/config.yaml
default: auto-office/config.default.yaml
```

Legacy config paths may be read for migration but must not remain the canonical location.

Config may define:

- enabled harnesses;
- planner internal/external preference;
- preferred planner/reviewer triples;
- worker multiplicity;
- browser verifier form;
- review round caps;
- gear presets;
- cost policy;
- quota reserve;
- exploration policy;
- adapter override permissions;
- per-repo routing preferences.

Hard safety invariants are not configurable:

- no self-approval;
- fixed lifecycle order;
- packet validation;
- policy snapshot pinning;
- human merge-to-main boundary.

Invalid keys warn and fall back to the next lower-precedence tier.

---

## 15. Gear presets

Built-in gears are seed presets only.

### `direct`

Minimal orchestration, self-verification only, no independent review unless risk forces one.

### `direct+review`

Direct execution plus a real independent code gate.

### `light`

Low-cost planning/execution with a shallow independent review path.

If a harness has no native shallow review, use a same-brand low-effort diff reviewer rather than silently degrading to `direct`.

### `quick`

Small reversible work; cost-bounded review.

### `express`

Dedicated planner may replace separate plan-review where policy allows.

### `full`

Separate planner, independent plan review, independent code review, funded verification, and full closeout.

Risk rules may increase gear automatically.

A gear may waive a recommended quality anchor, but never an absolute competence or verification floor.

---

## 16. Task shape and blast radius

The planner proposes a playbook.

The orchestrator freezes it.

### 16.1 Initial playbooks

#### Change

Localized behavior change.

#### Restructure

Refactor, migration, architectural reshaping, schema or interface movement.

#### Investigate

Evidence-gathering and diagnosis; may produce no code.

#### Prototype

Throwaway or experimental implementation used to test a decision.

#### Visual

Rendered UI, design, browser behavior, or visual regression work.

### 16.2 Blast radius

Blast radius must be evidence-backed.

The planner must cite static call/data/interface boundaries and add runtime probes where static evidence is insufficient.

Uncertainty widens the declared blast radius rather than shrinking it.

A widened blast radius may trigger:

- stronger gear;
- stronger reviewer;
- broader validation;
- different executor route.

---

## 17. Pre-dispatch size estimation

Size is a class:

```text
S | M | L | XL
```

Inputs include:

- task count;
- touched interfaces;
- file/diff scope;
- migration/security involvement;
- expected validation surface.

No point estimate is required.

No-history fallback uses fixed role defaults.

Each run records predicted versus actual size.

Calibration target:

- exact class at least 70%;
- otherwise adjacent class except for explicitly analyzed outliers.

Size classification influences budgeting and review expectations but never suppresses required verification.

---

## 18. Quota and cost

Quota is a first-class routing constraint.

### 18.1 Probe

Every enrolled harness with quota semantics must expose a cheap probe.

Probe result includes:

```yaml
status: ok | unknown | error
tightest_remaining_percent:
window_reset_at:
scope: account | workspace | session | unknown
error:
```

Unknown quota is not silently treated as unlimited.

### 18.2 Quota reserve

Default reserve:

```text
20%
```

A candidate whose projected dispatch would cross the reserve is excluded when another qualifying candidate exists.

If all qualifying candidates would cross the reserve, the orchestrator:

1. chooses a cheaper/smaller valid strategy when possible;
2. proposes a lower-cost route;
3. or asks the user if continuing requires consuming protected quota.

Capability floors are not lowered merely to preserve quota.

### 18.3 Effective cost policy

Because subscription quota and metered API dollars are not directly equivalent, v3 does not pretend they are one universal dollar number.

The default `balanced` policy:

1. eliminate quota-unsafe candidates;
2. estimate actual or published monetary cost;
3. among candidates within 20% of the cheapest monetary estimate, prefer lower projected quota burn;
4. then prefer lower expected wall clock;
5. then prefer stronger local reward.

Additional user policies:

```text
money_saver
quota_saver
balanced
```

Local measured account consumption outranks advertised pricing when both are available.

Every dispatch records both:

- monetary estimate/actual;
- quota delta/estimated burn.

---

## 19. Exploration policy

Exploration exists only to learn about unknown candidates without turning production work into an experiment.

Exploration is permitted only for:

- worker roles;
- Investigate or Prototype playbooks;
- reversible sandbox work;
- non-production-facing tasks.

Default limits:

- maximum one exploratory dispatch per run;
- maximum 10% of eligible worker dispatches over a rolling 20-dispatch window;
- candidate projected cost may not exceed 125% of the cheapest known qualifying candidate without explicit user approval.

Exploration never bypasses:

- adapter validity;
- protected-path rules;
- destructive-operation rules;
- required sandboxing.

An unexplored candidate may receive a selection preference only inside these limits.

---

## 20. Execution packet contract

Each executor packet includes at minimum:

```yaml
base_sha:
task_scope:
observable_outcome:
blast_radius:
allowed_mutations:
protected_paths:
validation_commands:
known_bad_behavior_to_exclude:
self_review:
rollback_or_restore_notes:
```

A packet must describe the observable outcome, not only the mechanism.

Where regression testing applies, the packet should state a wrong-but-passing implementation that the test must exclude.

A missing or contradictory mandatory field rejects the packet.

---

## 21. PLAN DEFECT and BRIEF DEFECT

A defect exit is a scored event only when supported by evidence.

### 21.1 Required proof

The raising role must show:

1. it followed the current plan/brief sufficiently to test the assumption;
2. concrete evidence contradicts a named plan/brief assumption;
3. continuing without amendment would violate the intended outcome or safety boundary.

### 21.2 State transition

On accepted defect:

1. execution pauses for the affected scope;
2. the defect is attributed;
3. plan/brief version increments;
4. stale packets are invalidated;
5. the appropriate owner amends the artifact;
6. dependent work resumes only from the new version.

A reviewer or executor may receive gate-like credit for catching an accepted defect even when that was not its nominal role.

Trivial or manufactured objections receive no credit.

---

## 22. Verification floor

Every mutable run performs at least self-verification.

Independent verification is required when risk, gear, playbook, or repository policy requires it.

Validation should prefer:

1. existing targeted tests;
2. regression tests;
3. type/lint/static checks;
4. build/package checks;
5. focused runtime validation;
6. broader suites when justified.

### 22.1 Browser work

User-facing changes require browser validation when a reachable local or preview environment can reasonably be produced.

Browser verification must execute the actual acceptance flow, not merely open the homepage.

### 22.2 Known-bad validation

A gate is not trusted merely because it passes.

Where practical, critical gates must be shown failing on known-bad or mutated input before their green result is accepted.

---

## 23. Review behavior

Reviewer findings use:

```text
accepted-material
accepted-minor
rejected-on-evidence
pending
```

Only accepted-material findings receive full gate credit.

Reviewer rounds should resume the same reviewer session when possible so the reviewer retains its prior uncertainty and findings.

A fresh reviewer session is preferred over the producer session.

Review prompts should ask pointed blast-radius and bypass questions rather than generic "review correctness" prompts.

### 23.1 Review tiers and disposition ownership (issue-35#decision-4, issue-77 §4-5,7)

Review funding is local first: each executor's own implementation and review loop (`review_mode: independent_adversary`) is the default for funded independent review; inline self-review/validation (`review_mode: labeled-inline`, never represented as independent) covers cheap/reversible/low-risk work. A final `integration_adversary` is triggered only when two or more executors produce dependent or merging landings that must compose across a shared interface — never by executor count alone; a single-executor family, or independent parallel changes with no cross-scope dependency, receives no mandatory second review, and its scope is the integrated diff rather than a re-review of each executor's code.

`disposition_owner` (`executor | planner | orchestrator`) records who is authoritative for a finding's next action. Ordinarily the executor decides: accept and fix, or reject with stronger evidence. It consults the orchestrator only exceptionally when it cannot responsibly resolve a disagreement with its reviewer — this is not a routine approval chain. A local code defect returns to its responsible executor; a plan/spec defect wakes the planner; an unresolved cross-executor conflict goes to the orchestrator for integration coordination; a genuinely unresolved evidence conflict or user-owned decision escalates to the user.

---

## 24. Run recorder

Private local database:

`~/.local/share/auto-office/runs.db`

SQLite, WAL mode.

Core tables:

- runs
- dispatches
- findings
- validations
- routing_decisions
- artifact_versions
- ownership_events
- outcome_labels
- lineage

Recorder writes are deterministic and append-oriented.

The agent should not need raw SQL access to make ordinary routing decisions.

---

## 25. Causal failure attribution

Failures must not automatically punish the model.

Each failure records one primary attribution plus optional contributing attributions:

```text
model
harness
adapter
quota/account
environment/network
planner
brief
repository
verification
unknown
```

Examples:

- credit wall before work begins → quota/account;
- stdin deadlock caused by adapter invocation → adapter;
- harness watchdog kills long silent work despite valid model output → harness/dispatch-form;
- logically wrong implementation despite healthy harness → model/executor;
- wrong plan assumption → planner.

Only model-attributed or role-attributed failures directly reduce the corresponding role/triple quality reward.

Other failures update reliability evidence for their own causal component.

---

## 26. Outcome labeling

The system does not equate "nothing bad was reported" with strong proof of correctness.

Labels:

```text
pending
verified_no_observed_failure
recurrence_failure
revert_failure
material_post_merge_defect
abandoned
environment_failure
```

### 26.1 Observation window

Window ends at the earlier of:

- 14 calendar days after merge;
- the next 3 runs touching the same repository and relevant surface.

### 26.2 Lazy maintenance

v3 requires no always-running daemon.

Historical rows are labeled during the first maintenance pass after they become eligible.

Maintenance runs:

- at the beginning or closeout of the next `auto-office` invocation;
- optionally through an external scheduler if the user installs one.

The spec therefore guarantees **eventual labeling on next runtime maintenance**, not exact wall-clock execution on day 14.

### 26.3 Positive vs negative weight

Recurrence, revert, and material post-merge defects are strong negative ground truth.

`verified_no_observed_failure` is weaker positive evidence, because absence of recurrence is not proof that no latent defect exists.

Calibration weights must preserve this asymmetry.

---

## 27. Reward model

Rewards are per role and per routable triple.

### 27.1 Producer terms

A producer gains from:

- verified acceptance evidence;
- clean first-round approval;
- low correction burden relative to size;
- no observed recurrence.

A producer loses from:

- accepted material defects;
- round-5 review failure;
- recurrence/revert attributable to its work;
- accepted `PLAN DEFECT` / `BRIEF DEFECT` when it owned the defective artifact.

### 27.2 Gate terms

A gate receives stronger credit than a producer for accepted-material findings.

Initial weight:

```text
accepted-material gate credit = 2 × equivalent producer credit
```

Accepted-minor findings receive reduced credit.

Rejected-on-evidence findings receive no positive credit and may reduce reviewer precision.

### 27.3 Cost terms

Record separately:

- monetary cost;
- quota burn;
- wall clock;
- dispatch count;
- review rounds.

Do not collapse them into one opaque reward value in storage.

A routing policy may derive a selection score from them, but raw terms remain inspectable.

### 27.4 Reward changes

Any change to:

- reward weights;
- capability floors;
- maturity weights;
- major routing thresholds

requires replay over all hot/warm comparable rows.

The replay must show old versus new decision outcomes and highlight routing or pass/fail flips.

---

## 28. Historical migration

Existing prose lessons are not converted into fake precise telemetry.

Migration rules:

1. hard exclusions already backed by concrete incidents may seed explicit veto/exclusion entries;
2. qualitative role preferences may seed low-weight priors;
3. historical prose does not invent token, reward, or outcome numbers that were never recorded;
4. v3 structured telemetry begins a new schema version;
5. old rows remain readable as historical evidence.

Seed qualitative priors have reduced authority and decay faster than structured v3 evidence.

---

## 29. Memory tiers

Structured local evidence has three active tiers:

### hot

Current triple/current generation.

Full routing authority.

### warm

Superseded once or otherwise stale but still structurally comparable.

Evidence-only; reduced authority.

### dreamt

Superseded twice, older than 90 days, or compacted due to row budget.

No numeric routing authority.

Only generalized patterns may survive.

Raw rows are not deleted solely because they become dreamt; authority changes by tier.

---

## 30. Dream compilation and privacy

The dream pass converts private structured evidence into generalized patterns.

Pipeline:

```text
private rows
→ deterministic redaction/sanitization
→ evidence capsule
→ pattern compiler
→ privacy lint
→ proposed public pattern
```

### 30.1 Deterministic sanitizer

Before model compilation, remove or replace:

- repository names;
- organization/person names;
- domains/hosts;
- emails;
- absolute local paths;
- credentials/tokens;
- issue-specific prose;
- long source-code spans;
- unique customer/project identifiers.

### 30.2 Evidence capsule

The compiler reads a redacted capsule, not raw private rows.

The capsule contains only the minimum structured facts needed to derive the pattern.

### 30.3 Public attribution

A public pattern cites a local opaque evidence hash, not a raw database row ID that outsiders cannot inspect.

Example:

```text
evidence_hash: sha256:...
source_count: 4
schema_version: 3
```

The local machine may map that hash back to private rows.

The public repository cannot.

### 30.4 Privacy lint

Before push, a deterministic scanner rejects suspicious:

- email forms;
- URLs/domains;
- absolute paths;
- unredacted repository/org/person identifiers;
- secrets;
- long verbatim source spans.

A model's self-certification is never the only privacy boundary.

---

## 31. Self-improvement isolation

A running family executes only from its pinned runtime policy.

Self-improvement work happens in a separate worktree/branch.

A run pins:

```text
plugin_commit
policy_hash
catalog_snapshot_hash
adapter_snapshot_hash
effective_config_hash
```

Unmerged proposed changes do not become active merely because they exist locally.

Activation occurs only after the configured authority event — by default, maintainer merge to `main` plus the next clean runtime load.

---

## 32. Self-heal proposal streams

There are two distinct automated proposal streams.

### 32.1 Learned-pattern stream

Scope:

- sanitized dreamt pattern lines;
- non-binding documentation/evidence references.

It may **not** directly change:

- capability floors;
- reward definitions;
- hard exclusions;
- destructive permissions;
- maturity policy;
- security boundaries.

### 32.2 Catalog/policy proposal stream

May propose:

- shipped catalog refreshes;
- adapter updates;
- config-default changes;
- routing policy changes;
- prompt/skill improvements.

Any behavior-changing proposal must include:

- replay where required;
- eval results;
- diff explanation;
- policy hash change;
- independent review before merge.

---

## 33. Pull request lifecycle

A merged GitHub pull request is terminal.

Therefore v3 does **not** attempt to "reopen the same merged PR."

Instead, each proposal stream uses:

- a stable branch naming convention or branch family;
- a new PR after the previous PR merges;
- lineage metadata linking successor PRs.

Example:

```text
auto/self-heal-current
auto/catalog-refresh-current
```

After merge:

1. local branch is reconciled to new `main`;
2. no PR exists until new content is available;
3. next proposal opens a new PR;
4. the new PR references the previous proposal lineage.

Human merge remains permanent.

---

## 34. Concurrent proposal safety

No global lock is required if writes are idempotent.

Every generated pattern or catalog proposal receives a deterministic identity hash.

Before append/retry:

1. fetch latest branch;
2. check whether the identity already exists;
3. apply only if absent;
4. commit;
5. push;
6. on non-fast-forward, retry once from fresh state.

Duplicate content must not be created by retry races.

---

## 35. Maturity model

Maturity is earned evidence, not permission to stop learning.

Each component and the overall runtime has an age from 0 to <100.

### 35.1 Evidence points

Initial difficulty weights:

```text
S  = 0.5
M  = 1.0
L  = 2.0
XL = 3.0
```

Initial event weights:

```text
verified_no_observed_failure   +1.0 × difficulty
accepted material reviewer catch before merge
                               -0.75 × difficulty
material_post_merge_defect     -2.0 × difficulty
recurrence_failure             -3.0 × difficulty
revert_failure                 -4.0 × difficulty
```

Events are applied only to the relevant component lineage.

### 35.2 Age curve

Let:

```text
P = max(0, cumulative evidence points)
```

Then:

```text
age = 100 × (1 - exp(-P / 60))
```

Interpretation:

- approximately 30+ = Adult;
- approximately 60+ = Seasoned;
- 100 is asymptotic and never reached.

Negative events reduce `P` and therefore age backward through the same curve.

### 35.3 Evidence bar

Maturity may not reduce scrutiny unless the component has at least:

- 30 labeled runs;
- across at least 3 repositories or task shapes;
- and held-out reward meaningfully predicts lower recurrence.

Before that threshold, displayed age is informational only.

### 35.4 Ecosystem decay

Evidence is lineage-scoped.

Default decay multipliers:

```text
minor harness release affecting no declared contract field: 0.9
major harness release or invocation change:                0.6
new model generation for that model lineage:               0.5
unrelated model/harness change:                             1.0
```

Decay applies only to affected evidence.

These policy values are replay-gated.

### 35.5 Maturity authority

Maturity may affect:

- default gear recommendation;
- independent verifier funding;
- confidence display;
- exploration allowance;
- amount of evidence demanded for a proposal.

Maturity may never remove:

- absolute capability floors;
- no-self-approval;
- human merge-to-main;
- destructive-action safeguards.

---

## 36. Calibration and replay

A policy refit is allowed only after at least 20 labeled structured v3 rows globally.

Replay process:

1. score all comparable hot/warm rows under old policy;
2. score them under proposed policy;
3. list every changed routing decision;
4. list every pass/fail boundary flip;
5. fit on oldest ~80%;
6. evaluate on newest ~20%;
7. never tune on the hold-out;
8. require a written argument for any material boundary flip;
9. treat zero flips across all rows as a warning that the policy change may be meaningless.

Index versions that are not comparable are never averaged together.

---

## 37. Adapter conformance

`add-adapter` scaffolds a candidate adapter.

Conformance has two levels.

### deterministic

Required before `valid-unverified`:

- schema;
- argv generation;
- safe prompt transport fixture;
- model mapping fixture;
- effort mapping;
- dispatch-form declaration;
- quota parser fixture;
- failure-signature parser;
- privacy-safe logging;
- unknown-field behavior.

### live

Required before `proven` unless equivalent runtime evidence already exists:

- no-hang launch;
- successful prompt transfer;
- expected output capture;
- liveness detection;
- model selection works;
- quota probe works or fails as declared;
- declared evidence capability is independently checked.

Per-harness cases belong in the shared `auto-office/evals` suite.

Brands do not own separate lifecycle eval suites.

---

## 38. Orchestrator scoring

The orchestrator is scored even though the router does not choose it.

Terms include:

- intent preservation;
- defect rate attributable to brief/orchestration;
- total wall clock;
- total cost;
- unnecessary dispatch/review churn;
- recurrence/revert outcomes.

A user-selected orchestrator may receive a recommendation warning when local evidence shows it is poorly matched to the task shape.

The runtime does not silently replace it.

---

## 39. Word-count and prompt-size guardrails

Prompt/instruction size is measured because bloated office instructions have real cost.

Metrics include:

- loaded instruction tokens;
- role packet size;
- PR prose word count.

These are reported and may contribute weakly to efficiency analysis. They are not CI gates.

**Exception — per-file line budgets.** Issue #35 constraint 11 originally held that word/size
metrics are reported, not gated. The maintainer (Rico Tiongson) explicitly reopened that constraint
for `scripts/check_ecosystem.py`, which now enforces a per-skill-file line budget as a hard CI gate
(fails the check when a file has no configured budget or exceeds it; see `SKILL_LINE_BUDGETS` in
that script). This is a deliberate, scoped reopening, not a silent reversal: it applies only to the
line-budget check, not to the other metrics listed above, which remain non-gating.

Clarity and correctness outrank minimal size.

---

## 40. Failure behavior

The runtime fails soft where safe and hard where ambiguity risks correctness.

### hard stop

- missing mandatory packet fields;
- unknown mandatory adapter semantics;
- violated protected paths;
- stale ownership lease;
- plan version mismatch;
- destructive action without required authority;
- privacy lint failure before public push;
- self-approval attempt.

### fail soft

- stale catalog refresh;
- missing public speed data;
- unknown non-binding benchmark field;
- unavailable optional shallow-review command;
- non-critical telemetry write failure.

Fail-soft events are surfaced and recorded.

---

## 41. Completion evidence

A family may report implementation complete only when the required evidence exists.

Completion report contains:

```text
Outcome
Key implementation
Validation
Browser/runtime evidence when applicable
Review result
PR/branch state
Remaining real blockers
Pinned runtime hashes
```

No completion claim may rely on model confidence alone.

---

## 42. Breaking changes from v2

v3 retires branded office variants.

The canonical office lifecycle becomes `auto-office`.

Harness-specific primitive skills remain directly invocable.

Branded lifecycle copies are removed after:

- all unique evals are migrated or explicitly retired;
- migration guidance is published;
- v3 compatibility notes are written.

In-flight runs finish using the skill snapshot they already loaded.

No compatibility promise exists after reload.

---

## 43. Public-repository privacy rule

This repository ships publicly.

Committed examples, fixtures, specs, telemetry samples, and self-heal proposals must never contain real:

- customer/project repositories;
- organizations;
- hosts;
- people;
- email addresses;
- credentials;
- private local paths.

Use opaque slugs and synthetic fixtures.

This rule applies to all generated PR content.

---

## 44. V3 acceptance criteria

v3 is implementation-complete only when all of the following are demonstrated end-to-end.

### Routing

- route unit is `harness@version × model × effort`;
- same underlying model across harnesses is represented correctly;
- hard floors filter before cost;
- local evidence can change a route;
- quota reserve can change a route;
- advisory anchor may be undercut without violating absolute floor;
- unknown mandatory adapter data prevents unsafe routing.

### Freshness

- stale catalog triggers asynchronous refresh;
- route-time uses no network;
- current run remains reproducible from a pinned snapshot;
- a newly refreshed local model can become routable without first merging a repository commit;
- seed catalog promotion produces a separate human-merged PR.

### Adapters

- invalid, valid-unverified, and proven states are enforced;
- a new adapter can be added from the contract without modifying lifecycle code;
- unverified adapters cannot silently receive mutable executor authority by default.

### Portability

- a fresh session can take over a family from durable state;
- stale owner cannot continue writing after takeover;
- plan/packet/version mismatches block unsafe continuation.

### Verification

- all mutable work self-verifies;
- required independent review cannot self-approve;
- browser-facing acceptance criteria are tested in a reachable runtime where feasible.

### Telemetry

- every dispatch records route, size, cost, quota, outcome, and causal attribution;
- failure attribution distinguishes model, harness, adapter, quota, and environment;
- historical rows are eventually labeled through lazy maintenance.

### Learning

- public benchmark seeds cold start;
- local evidence can supersede the seed;
- recurrence/revert are stronger negative signals than no-observed-failure is positive;
- policy changes requiring replay cannot bypass replay.

### Privacy

- raw hot/warm evidence never leaves the machine;
- dream compilation receives a sanitized capsule;
- deterministic privacy lint can block a bad public proposal;
- public attribution uses opaque evidence hashes.

### Self-improvement

- current run remains on its pinned policy while improvement work happens elsewhere;
- merged PRs are never "reopened";
- successor proposal PRs preserve lineage;
- duplicate retries are idempotent;
- maintainer merge to `main` remains mandatory.

### Maturity

- age is deterministically reproduced from the same run database and policy file;
- failures move age backward;
- relevant ecosystem changes decay only relevant evidence;
- maturity cannot remove absolute safety gates.

---

## 45. V3 implementation sequence

Recommended implementation order:

1. durable run/family/packet schemas;
2. policy/config snapshot hashing;
3. catalog seed + local immutable refresh snapshots;
4. adapter contract + conformance;
5. route filtering and absolute floors;
6. quota probe and cost policies;
7. gear/playbook/size classification;
8. execution and reviewer packet generation;
9. recorder + causal attribution;
10. lazy outcome maintenance;
11. local-evidence routing;
12. replay/calibration;
13. maturity engine;
14. dream sanitizer/compiler/privacy lint;
15. isolated self-improvement worktrees;
16. proposal PR streams;
17. v2 branded-office retirement;
18. full acceptance-suite rehearsal.

Do not begin with self-healing. The system must first be reproducible, attributable, and safe to learn from.

---

## 46. Final authority model

The runtime may automate aggressively before merge.

It may:

- refresh catalogs;
- discover locally installed harnesses;
- route roles;
- change local learned priors;
- propose adapter/config/prompt/routing improvements;
- create PRs;
- run replay/evals;
- review its own proposals with an independent agent.

It may not:

- merge its own policy changes to `main`;
- silently activate an unmerged policy into the current run;
- lower absolute safety floors to save cost;
- publish raw private evidence;
- treat missing evidence as success.

The permanent boundary is simple:

> **Agents may propose and prove. The maintainer decides what becomes the shipped policy.**

---

## 47. Definition of done

`office-skills` v3 is done when a normal user can run `auto-office` repeatedly across changing models and harness versions and observe all of the following without hand-maintaining routing tables:

- the runtime notices ecosystem changes;
- routing remains reproducible;
- local outcomes influence future route choice;
- quota pressure changes route choice;
- weak/unverified integrations are contained;
- a run can survive session replacement;
- user-facing work is actually verified;
- failures are attributed to the right layer;
- learning remains private;
- improvement proposals are isolated, reviewable, replayable, and idempotent;
- no autonomous path exists from model-written policy to merged `main`.

At that point, v3 is not "finished learning."

It is finished becoming a system that can **learn safely**.
