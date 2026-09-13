# Roles and authority

## Orchestrator
Own user intent, scope, explicit choices, frozen execution fields, gear, playbook, route approval, lifecycle gates, dispatch coordination, plan acceptance/rejection, and final escalation. Score the orchestrator, but do not silently replace the user's entry model. Every serialized plan declares the orchestrator's exact model identity and why it owns the role.

## Planner
Own how to implement frozen intent. Never silently change product requirements. Emit a serialized plan that declares both planner and orchestrator model assignments, including inline/routed status and selection rationale. Revise when an accepted `PLAN DEFECT` invalidates an assumption. Seed preference: Opus Medium, then Astra Low, then the normal router; local evidence may supersede these priors.

## Reviewers
Never self-approve work from the same producer session. Prefer a fresh/different session for non-trivial mutable work where the harness permits it. Reviewer seed may prefer Luna XHigh when local evidence supports it.

## Executor/worker
Select by task shape and routing evidence, not one static brand. Builder roles are subject to hard capability floors.

## Browser verifier
Independently validate the user-observable acceptance path whenever acceptance materially depends on rendered/interactive behavior and a reachable runtime can reasonably be produced.

## Authority boundaries
One mutable role has one active holder for its write scope. The orchestrator retains the permanent merge-to-main boundary unless the human explicitly performs/authorizes the merge through the supported environment. No producer can act as its own independent gate.
