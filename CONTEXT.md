# Auto Office

Adaptive office engineering runtime: one lifecycle that plans, routes, executes, reviews, and lands work through independent agents under user authority.

## Plan review and amendment

**Plan defect**:
An evidence-backed review finding that the plan contradicts frozen requirements or acceptance, rests on a false interface/contract assumption, contains an unsafe, irreversible, or authority-exceeding action, or lets two writers own one scope. Blocks dispatch or pauses the affected scope until cleared.
_Avoid_: blocker, critical finding

**Brief defect**:
A plan defect confined to one task brief.

**Ordinary amendment**:
An in-contract change to decomposition, ordering, routing, acceptance detail, or tests that the orchestrator may make without waking the planner.
_Avoid_: refinement, tweak, replan

**Contract amendment**:
A planner-owned change to cross-scope interfaces, ownership boundaries, authority, or irreversible actions.
_Avoid_: plan defect fix (a contract amendment may clear a plan defect but is not one)

**Requirements change**:
A change to frozen intent or acceptance criteria; only the user may make one.

**Defect clearance**:
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

**Escalation**:
The single bounded extra step (diagnosis or a better-qualified reviewer) allowed after a round budget is spent, before work pauses for the user.

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
One required check on a revision: deterministic checks, code review, UI review, or integration review.

**Acceptance**:
The state of a checkpoint whose required gates all passed on one revision.
_Avoid_: pass, green, done

**Integration review**:
A review of composed output, required only where one checkpoint consumes another's unmerged output or a landing combines scopes that share an interface or file.
