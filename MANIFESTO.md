# Auto Office Manifesto

> The method should survive the replacement of every agent and tool.

Auto Office is an opinionated, model-agnostic engineering lifecycle. It turns human intent into bounded work, assigns capable agents, challenges their results, verifies the whole, and closes with evidence. The lifecycle is essential; the models, harnesses, vendors, and runtime machinery are replaceable.

## Core beliefs

1. **Understand before dispatch.** Treat the initial request as intent to investigate, not a complete specification. Inspect the actual system, establish success criteria and constraints, and ask humans to settle choices only they can make.
2. **Plan the seams before the work.** Substantial changes need explicit outcomes, dependencies, interfaces, ownership boundaries, and checks. Challenge the initial plan when warranted. Trust responsible owners to make ordinary in-scope amendments without restarting a review ritual; change the plan when reality disproves it.
3. **Parallelism must be earned.** Delegate work concurrently only when scopes can progress independently. Coordinate dependencies and shared interfaces. Every mutable scope needs one accountable owner, not competing writers.
4. **Route by fitness, not reputation.** Planning, investigation, building, review, and specialist verification can demand different agents. Require competence, trust, and safety first; then weigh task fit, availability, quota, speed, and expected cost **to a correct result**, including rework. Benchmarks are cold-start evidence; sufficiently comparable local outcomes are stronger evidence. No model or harness owns a role by default.
5. **Producers improve their own work; they do not independently approve it.** Before handoff, producers simplify, test, and adversarially self-review against the goal. This is part of production, not an independent gate. When independent judgment is required, it must come from outside the producing session or role.
6. **Review where it matters.** Challenge consequential plans and composed changes at meaningful boundaries, not every isolated fragment by reflex. Use code, security, visual, or browser/runtime specialists when the risk or acceptance criteria call for them. Proportionate self-checks for trivial work are valid, but never pretend they were independent review.
7. **Integration is a deliverable.** Locally correct pieces can be wrong together. Reconcile interfaces, dependencies, and shared assumptions; verify the assembled result and actual user-visible behavior where relevant. A task marked done is not the same as the original goal being met.
8. **Evidence must outlive the session.** Record decisions, routes and reasons, revisions, findings, tests, verification, and outcomes so others can inspect, resume, or challenge them. Attribute failure accurately: a bad plan, environment, quota, adapter, reviewer, or model are different causes. Missing or stale evidence never becomes a pass.
9. **Authority stays human; rigor stays proportional.** Humans own requirements changes and consequential, destructive, or external decisions, including final landing where policy requires consent. Scale planning, delegation, and review to risk and size; a safe one-line edit does not need an artificial committee.

## The lifecycle

1. **Discover:** understand the real objective, system, constraints, risks, and definition of done.
2. **Plan:** identify bounded tasks, owners, dependencies, interfaces, and verification; challenge the initial plan as needed.
3. **Route and delegate:** select fit-for-purpose agents for each role; parallelize only genuinely separable work.
4. **Produce and self-review:** implement or investigate within scope, simplify, check, and repair before handoff.
5. **Independently review where warranted:** judge the right composed boundary, return findings to their owners, and preserve the revision trail.
6. **Integrate and verify:** combine accepted work, test the seams and actual behavior, and check the whole against the original goal.
7. **Land, learn, and close:** obtain the required authority, deliver or hand off, disclose residual risks, and preserve comparable outcomes.

At each boundary, reassess the evidence against the goal: proceed, amend, ask the human, or stop. Completion means a verified outcome, not merely that every agent reported completion.

## What Auto Office refuses to be

- One giant autonomous session masquerading as an engineering team.
- Blind fan-out, overlapping write ownership, or parallelism for its own sake.
- “Always use the smartest model,” “always use the cheapest model,” or benchmark worship.
- An opaque router that cannot explain a choice or learn from comparable outcomes.
- Producer self-approval disguised as independent review.
- Isolated green checks standing in for integration or observable behavior.
- Automation that invents requirements, consent, or evidence.
- Ceremony added to trivial work merely because a full lifecycle exists.
- A philosophy locked to today's models, vendors, harnesses, or runtime.

## The portability test

Replace every agent, model, tool, and implementation detail. If the new system still understands before acting, plans bounded work, routes by fitness, separates self-review from independent judgment, verifies integration, respects human authority, preserves evidence, and learns from real outcomes, it is practicing Auto Office.
