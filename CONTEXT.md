# Auto Office

Adaptive office engineering runtime: one lifecycle that plans, routes, executes, reviews, and lands work through independent agents under user authority.

## Plan review and amendment

**Plan defect** (v3.1 contract):
An evidence-backed review finding that the plan contradicts frozen requirements or acceptance, rests on a false interface/contract assumption, contains an unsafe, irreversible, or authority-exceeding action, or lets two writers own one scope. Blocks dispatch or pauses the affected scope until cleared. Under convergence-v1 it is not a verdict; `root-cause:` metadata on a finding keeps the class.
_Avoid_: blocker, critical finding

**Brief defect** (v3.1 contract):
A plan defect confined to one task brief. Under convergence-v1, root-cause metadata only.

**Ordinary amendment**:
An in-contract change to decomposition, ordering, routing, acceptance detail, or tests that the orchestrator may make without waking the planner.
_Avoid_: refinement, tweak, replan

**Contract amendment**:
A planner-owned change to cross-scope interfaces, ownership boundaries, authority, or irreversible actions.
_Avoid_: plan defect fix (a contract amendment may clear a plan defect but is not one)

**Requirements change**:
A change to frozen intent or acceptance criteria; only the user may make one.

**Defect clearance** (v3.1 contract):
An independent plan reviewer's confirmation, by finding identity, that a revision containing the fix resolves a plan defect.
_Avoid_: resolving, waiving

**Authorization**:
The user's approval of a requirements version and its authority envelope. Independent of plan-review status.
_Avoid_: approval (unqualified), plan approval

**Authority envelope**:
The authorized list of irreversible or external actions and their targets.

**Plan-review status**:
The current technical verdict on the plan, separate from authorization.

**Delivered**:
An amendment delta has reached an executor. Says nothing about whether it is followed.

**Applied**:
An executor has acknowledged adopting an amendment's version at a safe boundary.
_Avoid_: acknowledged (unqualified), received

**Superseded**:
An unapplied amendment or finding replaced by a newer one; never applied or enforced afterward.

**Retracted**:
A finding withdrawn on evidence by a later review of the current revision.
_Avoid_: resolved, dismissed

**Stale result**:
A gate or review result for a revision other than the current one; audit evidence only.

**Lease**:
Exclusive mutable ownership of one scope by one holder; recorded in every revision it produces.
_Avoid_: lock, claim

**Escalation** (v3.1 contract meaning):
The single bounded extra step (diagnosis or a better-qualified reviewer) allowed after a round budget is spent, before work pauses for the user. Under convergence-v1, `escalate` is one of the operator's round-cap choices (see Substantive round).

## Review contracts (#337)

**Review contract**:
The review semantics a run is pinned to at `office start` (`gates_json.review_contract`): `convergence-v1` for new runs, `v3.1` for runs with no recorded contract. `review.contract` in config picks it for future runs only. Reference: `docs/review-convergence.md`.
_Avoid_: review mode, review policy (unqualified)

**APPROVED**:
No blocking finding; the work advances now. Remaining findings are fixed or dispositioned without another independent review.
_Avoid_: PASS (the v3.1 word)

**RECHECK**:
At least one blocking finding. The owning producers repair it and the same reviewer, when available, reviews again.
_Avoid_: CHANGES_REQUIRED (the v3.1 word)

**INTAKE_GAP**:
The right answer depends on a missing or conflicting user-owned decision that requirements and repository evidence cannot settle. The reply names the decision, why evidence cannot settle it, and what it affects; the orchestrator asks the user at once.
_Avoid_: plan defect, blocker

**Review status**:
Whether a review attempt produced a verdict: `COMPLETED`, `UNAVAILABLE`, `EVIDENCE_BLOCKED` or `INVALID_RESULT`. Only `COMPLETED` carries a verdict. Separate from severity and from the verdict.

**Ownership envelope**:
A task's `scope:` under convergence-v1: the module or domain directories it owns plus their tests. Exact files are named only at collision points or cross-task dependencies. Inside it the executor owns files, helpers, internal APIs, refactors and technique.
_Avoid_: file list, write list

**Hard seam**:
Requirements, authority, ownership, dependency, interface, or acceptance. A repair or plan revision that moves one is never APPROVED cleanup.

**Lane**:
An ownership/composition lane: tasks joined by `depends` or sharing a `lane:` name, composed together once all are accepted. The unit of convergence review. Id `L-<lane name or first task>`.
_Avoid_: wave, Herdr lane (a takeover worker lane)

**Shared scope**:
Lanes that share a `converge:` name, an interface, a `shared:` registry, or changed files, reviewed once more together after the member lanes converge. Id `S-<lanes>`; `S-rebase` after `office land --rebase`.

**Convergence review**:
The one independent review of a lane's or shared scope's composed commit, funded by the gear's `independent_code_review`. A lane with user-visible acceptance also gets a specialist visual review on the same commit. Replaces per-task code review and integration review.
_Avoid_: code review (unqualified), integration review

**Substantive round**:
A completed review in the current cycle. Each RECHECK sequence stops after 3; the user then chooses `escalate`, `continue`, `waive` or `stop` (`office decide`). Reviewer failures, malformed replies, capture retries and evidence recovery spend no round.

**Disposition**:
The recorded outcome of a non-blocking finding: `fixed`, `dismissed`, `follow-up`, or `fix` (route an APPROVED cleanup repair that is not re-reviewed unless it moves a hard seam). Landing and close wait until every APPROVED finding has one.

**Waiver** (landing-authority bound):
Acceptance of an unmet required convergence or visual review. Only landing authority grants one: the user, or the orchestrator when the user delegated landing (end state `merge`/`e2e` or a recorded merge authorization). It keeps the underlying verdict, binds to the scope's composed commit, and appears on the archive receipt.
_Avoid_: override, pass

**Degraded fallback review**:
The orchestrator's own review of a convergence gate after every specialist reviewer route was UNAVAILABLE (`office review`). Recorded with independence `degraded-orchestrator`; grants no waiver or landing authority. For visual it requires inspecting every screenshot.

## Checkpoints and gates

**Snapshot**:
A durable save of unfinished work. Proves nothing about behavior.
_Avoid_: checkpoint (for saves), commit

**Checkpoint**:
A planned acceptance slice with its own gate contract.
_Avoid_: microtask, wave, save point

**Revision**:
The immutable identity a gate judges: the runtime-made commit of submitted work plus the applied requirements, plan, and acceptance versions and any relevant environment fingerprint.
_Avoid_: HEAD, version

**Gate**:
One required check on a revision or composed commit. Under convergence-v1: a task's deterministic checks, plan review, and each lane's or shared scope's convergence and visual review. Under v3.1: deterministic checks, per-task code review, UI review, and integration review.

**Acceptance**:
The state of a checkpoint whose required gates all passed on one revision.
_Avoid_: pass, green, done

**Integration review** (v3.1 contract):
A review of composed output, required only where one checkpoint consumes another's unmerged output or a landing combines scopes that share an interface or file. Convergence-v1 has none: shared scopes cover those boundaries.

**Plan diagram**:
The runtime's rendering of a plan before approval: waves, stacking derived from `depends`, a non-binding route preview with its why, and the checkpoint chain through the end state.
_Avoid_: plan graph, flowchart

**Task PR**:
The draft GitHub PR for one task's branch. A root task targets the default branch; a dependent targets the branch it stacks on. Its head is always the latest submitted revision.
_Avoid_: integration PR

**End state**:
How far the user asked the run to go after the task PRs: `ask`, `preview`, `merge`, or `e2e` (merge, prod deploy, verify). Recorded in the plan requirements, so plan authorization covers it.
_Avoid_: deploy mode, landing mode

## Runtime (3.1+)

**Office version**:
The exact PEP 440 identity of the runtime that owns a run (`3.1.0`, or `3.1.0+g<sha>` from a source checkout). Pinned on the run and every packet; a mismatch is rejected.
_Avoid_: plugin version, release (unqualified)

**Front door**:
The `office` command on PATH. It resolves the target run and runs that run's pinned runtime.

**Outbox job**:
A durable unit of external work (a review, a capture, a delivery) committed in the same transaction as the state change that needs it, then run by a short-lived `office _job` process.
_Avoid_: background task, daemon

**Evidence status**:
Whether a visual capture can be judged: `COMPARABLE`, `INVALID_COMPARISON`, `NOT_APPLICABLE` or `CAPTURE_BLOCKED`. Separate from the verdict.

**Vision proof**:
A cached result of the image-capability probe for one exact harness/model/effort/adapter route. Only a passing proof qualifies a route for visual judgment.

**Model family floor**:
The minimum model version per family for default routing (`model_family_floors`, Gemini 3.7 by default). An explicit `--route` is exempt.

**Tombstone**:
The row `office prune -f` leaves for a removed run: identity, terminal reason, archive digest.
