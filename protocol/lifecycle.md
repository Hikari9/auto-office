# Lifecycle and gears

The lifecycle order is fixed: provisional intent → tracking issue creation/reuse and family recording → interactive planner discovery and requirements freeze → baseline → shape/risk → decisions → plan → plan review when required → execution packets → routed execution → self-verification → independent review when required → browser/runtime verification when user-facing → reconciliation → tracking issue update → closeout → telemetry/state → lazy maintenance → optional isolated proposals.

Every phase that is entered has a self-review checkpoint before the run advances. The phase owner checks the phase objective, done criteria, current state, evidence, and residual risks or decisions, then records a disposition to proceed, amend, or stop. Optional phases that are omitted are covered by self-review of the omission decision. The receipt, evidence floor, and distinction from independent approval are normative in lifecycle spec §8.0.

Intent capture is two-phase, not one freeze: the orchestrator captures only provisional intent at kickoff, and the planner interacts directly with the user, may reshape that intent with repository evidence, and freezes the five execution fields at the end of its own discovery — not before planning starts. See `protocol/roles-and-authority.md`.

Gears are funding/intensity presets over this order, never alternate lifecycles. Built-in seeds are `direct`, `direct+review`, `light`, `quick`, `express`, and `full`. A gear can waive an advisory quality anchor but never absolute competence/safety floors, packet validation, no-self-approval, policy pinning, fixed lifecycle order, or human merge-to-main.

Task playbooks are independent from gear and route: `Change`, `Restructure`, `Investigate`, `Prototype`, `Visual`. The runtime decision is `gear × playbook × route`.

Risk may automatically increase gear. Size classes are `S|M|L|XL`; size affects budgeting/review expectations but never suppresses required verification.
