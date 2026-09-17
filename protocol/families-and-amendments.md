# Families, amendments, and compaction

## Concurrent families and sticky focus

One orchestrator may supervise multiple independent planner/executor/reviewer families across
projects/repositories. A durable family registry (`schemas/family-registry.schema.json`) holds,
per family: repo/issue, lifecycle phase, `requirements_version`/`plan_version`/`routing_version`,
current ownership, dependencies, active dispatches, latest landing, and pending user decisions.
Full child transcripts are never orchestrator state — the registry is.

Exactly one family is the current conversational focus:

- an unqualified command mutates the current focus family only;
- naming another family switches focus and mutates the named family;
- an explicitly global command applies session-wide, across every family;
- a genuinely ambiguous command mutates nothing and states why, rather than guessing.

Families run independently by default. The orchestrator projects quota/resource collisions across
families and warns the user, but does not delay unrelated work merely to optimize quota; explicit
user routing commands always outrank a projected-collision warning.

Live routing precedence: `explicit dispatch command > family/project override > repo config >
orchestrator-session policy > user-global config > plugin default`.

## Amendment ownership

`requirements_version`, `plan_version`, and `routing_version` amend independently
(`schemas/amendment.schema.json`, `kind: routing | requirements | plan_contract`):

| Amendment kind | Wakes the planner? | Effect |
|---|---|---|
| Routing-only (model/reviewer/parallelism/depth change) | No | Orchestrator updates routing state and informs affected executors; a not-yet-started dispatch uses the new route immediately. |
| Requirements delta that still fits the plan | No | Orchestrator versions requirements and sends a delta packet to affected executor(s); architecture, interfaces, dependency order, milestones, and done criteria remain valid. |
| Plan-contract delta (architecture, interfaces, dependency ordering, milestones, done criteria, or a plan assumption invalidated) | Yes | Pause only affected scopes; planner runs interactive delta-planning, self-reviews, runs its plan adversary, and emits a new plan version; orchestrator redistributes only what changed. |

A running dispatch normally completes its current atomic unit/round before a new route or plan
applies; immediate replacement requires an explicit user request (`protocol/state-and-takeover.md`
§ replacement authority; `docs/v3-runtime-contracts.md` §3.1). Bumping every version on every delta
is itself a defect — a routing-only change must never invalidate requirements or plan approval.

## Conditional compaction

Long-lived roles may compact at semantic phase boundaries, after first serializing the state the
next phase needs (`schemas/checkpoint.schema.json`). This is conditional, not mandatory for every
task:

- inline review or a tiny task needs no compaction;
- an executor with substantial implementation history serializes a checkpoint (requirements/plan/
  routing versions, task scope, current diff/changed files, key decisions and tradeoffs,
  validation already run, interfaces touched, deviations, unresolved concerns, current review
  round) before an adversarial reviewer is launched, then compacts;
- a long review/fix loop may compact again at later semantic boundaries;
- the reviewer receives a fresh independent packet (plan/requirements, repository diff, validation
  evidence, review scope) rather than the executor's transcript, preserving adversarial
  independence.

The repository, diff, tests, and serialized packet remain the source of truth; a role does not pay
to retain a transcript of exploratory reads, failed attempts, or routine tool history to answer a
reviewer later.
