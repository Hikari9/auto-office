---
name: auto-planning
description: Auto Office 3.0 reference spoke, not loaded by 3.1 runs (they use the office CLI). Internal Auto Office v3 planning spoke. Use when the auto-office orchestrator needs to run interactive requirements discovery directly with the user, freeze the five execution fields, resolve product decisions, classify playbook/blast radius/size, produce or refresh a serialized implementation plan, or revise a plan after an accepted PLAN DEFECT. Do not use as a separate lifecycle or to change frozen requirements silently after freeze.
---

# Auto Planning

> **Auto Office 3.1:** this is 3.0 reference material. A 3.1 run is driven by the `office` CLI and runtime-delivered
> role briefs; do not run the `office_runtime.py` helpers below for it. Follow `office status` and its `next:` line.

Receive the pinned run envelope and the orchestrator's provisional intent. Own **what** up to freeze, then **how**: run `skills/auto-intake/SKILL.md`'s interview directly with the user, reshape goal/scope/done-criteria/blast-radius/named-actions/non-goals when repository evidence contradicts the provisional framing, then freeze the five fields.

1. Reconcile repository/runtime evidence before planning.
2. Propose playbook (`Change|Restructure|Investigate|Prototype|Visual`); orchestrator freezes it.
3. Declare evidence-backed blast radius. Cite static call/data/interface boundaries; add runtime probes when static evidence is insufficient. Widen uncertainty rather than hiding it.
4. Classify `S|M|L|XL` from task count, touched interfaces, diff scope, migration/security involvement, and validation surface.
5. Serialize the plan with observable outcomes, protected paths, validation strategy, known-bad behavior tests, rollback/restore notes, and `model_assignments` for both orchestrator and planner. Each assignment names exact invocation model identifier when available, canonical `model_id`, effort, harness/version, selection rationale, and `inline|routed` status. Declare both roles even when one session owns both.
6. Group tasks into waves. Every task declares `Depends on:` and `Touches:`. A wave is the transitive closure of tasks whose dependencies are satisfied; no two tasks in one wave may `Touches:` the same file. Pin a shared interface (signature/schema/fixture) before dispatch, or make it its own earlier task — apparent parallelism over an unpinned shared contract becomes a serial integration task, not a wave. Every dispatch brief carries the pinned contract and `effective_config_hash`.
   Exception (3.2 `shared:`): an append-only registry that several tasks must extend (auth/gate manifest, endpoint or grant list, exhaustive policy map, shared mock table) is declared `shared:` on each task instead of serializing them. It is not double-scope ownership, and the compose step merges the appends. Put existing tests that a change predictably breaks (call-count assertions) in the task's scope up front.
7. Emit execution packets only after the plan is accepted at the required gate.
8. On accepted `PLAN DEFECT`, increment plan version and invalidate dependent packets before revising Increment it with `office_runtime.py amend --kind plan_contract` (fields in `protocol/families-and-amendments.md`), not by editing `plan.md` alone; a file edit leaves the runtime `plan_version` stale.

Planner seed preference comes from `roles.planner.preferred_seed` in resolved config (`protocol/routing.md` § advisory anchor); local evidence may supersede it. `auto-routing` selects the planner route when a dedicated planner is funded — planning never routes itself.
