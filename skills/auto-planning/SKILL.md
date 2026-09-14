---
name: auto-planning
description: Internal Auto Office v3 planning spoke. Use when the auto-office orchestrator needs to resolve product decisions, classify playbook/blast radius/size, produce or refresh a serialized implementation plan, freeze observable outcomes, or revise a plan after an accepted PLAN DEFECT. Do not use as a separate lifecycle or to change user intent silently.
---

# Auto Planning

Receive the pinned run envelope and orchestrator-frozen intent. Own **how**, never silently change **what**.

1. Reconcile repository/runtime evidence before planning.
2. Propose playbook (`Change|Restructure|Investigate|Prototype|Visual`); orchestrator freezes it.
3. Declare evidence-backed blast radius. Cite static call/data/interface boundaries; add runtime probes when static evidence is insufficient. Widen uncertainty rather than hiding it.
4. Classify `S|M|L|XL` from task count, touched interfaces, diff scope, migration/security involvement, and validation surface.
5. Serialize the plan with observable outcomes, protected paths, validation strategy, known-bad behavior tests, rollback/restore notes, and `model_assignments` for both orchestrator and planner. Each assignment names exact invocation model identifier when available, canonical `model_id`, effort, harness/version, selection rationale, and `inline|routed` status. Declare both roles even when one session owns both.
6. Group tasks into waves. Every task declares `Depends on:` and `Touches:`. A wave is the transitive closure of tasks whose dependencies are satisfied; no two tasks in one wave may `Touches:` the same file. Pin a shared interface (signature/schema/fixture) before dispatch, or make it its own earlier task — apparent parallelism over an unpinned shared contract becomes a serial integration task, not a wave. Every dispatch brief carries the pinned contract and `effective_config_hash`.
7. Emit execution packets only after the plan is accepted at the required gate.
8. On accepted `PLAN DEFECT`, increment plan version and invalidate dependent packets before revising.

Seed planner preference is Opus Medium then Astra Low; local evidence may supersede. Never route yourself; ask `auto-routing` for the planner route when a dedicated planner is funded.
