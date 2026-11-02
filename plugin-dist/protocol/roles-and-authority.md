# Roles and authority

## Orchestrator
Own the provisional intent captured at kickoff, gear, playbook, route approval, lifecycle gates, dispatch coordination, plan acceptance/rejection, family registry and sticky focus, amendment classification, and final escalation. The kickoff intent is a hypothesis handed to the planner, not a frozen contract — the orchestrator does not run the requirements interview and does not freeze the five execution fields itself; see `skills/auto-planning/SKILL.md`. Score the orchestrator, but do not silently replace the user's entry model. Every serialized plan declares the orchestrator's exact model identity and why it owns the role.

## Planner
Own **what** as well as **how**, up to the moment requirements freeze: the planner performs repository reconnaissance, interacts directly with the user (including through a Herdr pane when available), asks deeper repo-informed questions, and may reshape goal, scope, done criteria, blast radius, named actions, non-goals, interfaces, and milestones when evidence shows the orchestrator's provisional framing was wrong. Requirements freeze only at the end of this interactive phase, not before it starts. After freezing, the planner writes the plan, self-reviews it, and runs its own plan adversary before returning a plan packet to the orchestrator. Emit a serialized plan that declares both planner and orchestrator model assignments, including inline/routed status and selection rationale. Revise when an accepted `PLAN DEFECT` invalidates an assumption. Seed preference comes from `roles.planner.preferred_seed` in resolved config; local evidence may supersede it.

## Reviewers
Never self-approve work from the same producer session. Prefer a fresh/different session for non-trivial mutable work where the harness permits it. Reviewer seed preference comes from `roles.reviewer.preferred_seed` in resolved config. Review funding is local-first: inline/labeled-inline review for cheap, reversible, low-risk work; an integration adversary only when multiple executors produce dependent or merging landings. See `protocol/verification-review.md` for the tiers and `disposition_owner`.

## Executor/worker
Select by task shape and routing evidence, not one static brand. Builder roles are subject to hard capability floors. The executor owns review disposition for its own work: it may accept a finding and fix it, or reject it with stronger evidence. It consults the orchestrator only exceptionally, when producer and reviewer cannot responsibly resolve a disagreement themselves — this is not a mandatory approval chain, and the orchestrator may itself escalate a genuine evidence conflict to the user rather than acting as a higher-tier technical judge.

## Browser verifier
Independently validate the user-observable acceptance path whenever acceptance materially depends on rendered/interactive behavior and a reachable runtime can reasonably be produced.

## Authority boundaries
One mutable role has one active holder for its write scope. The orchestrator retains the permanent merge-to-main boundary unless the human explicitly performs/authorizes the merge through the supported environment. No producer can act as its own independent gate. Exceptional upline consultation (executor → orchestrator → user) resolves a genuine evidence conflict; it is not a routine second-guessing path and does not make the orchestrator a technical reviewer of the executor's decision.
