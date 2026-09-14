---
name: auto-loop
description: Internal Auto Office v3 loop-driver spoke. Use after plan approval to dispatch waves without blocking, integrate dispatch branches through a validated merge, adjudicate contract disagreements, and hold the autonomy ceiling — proceeding end to end while never merging to main without an explicit user statement.
---

# Auto Loop

Dispatch every task in the current wave before waiting on any of them. One worktree per
dispatch, created off the run's pinned base SHA; workers never share a tree and never run git —
the orchestrator commits and merges. Two tasks in one wave never touch the same write scope; a
wave is only real if it can be drawn as disjoint.

Poll, never block. While a dispatch runs, the orchestrator may only touch write scopes no live
dispatch holds: planning later waves, reviewing returned output, preparing briefs, reading state.
A single blocking wait on one worker is a defect the moment another worker has already returned.
A wave ends when every dispatch in it has returned or been declared dead by two independent
liveness signals — one signal alone is not a death.

Arm a background monitor over every dispatch at or before dispatch time, kept armed for the whole
run, not per wave — completion is an event from it, never a blocking wait, and it must fire on
every terminal state (finished, idle, blocked, unknown, disappeared), not just success. Talking to
the user never suspends it. Lost or unarmed monitor: re-poll before asserting any dispatch state.
On each completion event, collect the result, close that agent's pane, and update the spawn
ledger — see spec section 6 for the full rule and the failure it closes.

At integration: commit each worker's tree on its own dispatch branch, authored by the
orchestrator; merge dispatch branches into the run's integration branch in wave order; resolve
conflicts at the orchestrator, never by re-dispatching a worker into a tree it does not own; then
run the plan's validation commands on the merged result. A green worker tree is not evidence
about the merge — the first moment N parallel trees have ever existed together is after it.

When a test written against the pinned contract disagrees with an implementation, name which one
is wrong and why: repo convention outranks an ambiguous contract clause, and a contract clause
outranks an implementation's convenience. This is an adjudication, not a re-plan.

Round caps for review live in `auto-review`; do not restate them here.

After approval the run proceeds end to end with no further go-aheads. It bootstraps a plan-only
commit, named branch, and draft PR before the first task; commits each wave as it goes green;
runs verification and review rounds; removes the plan file and marks the PR ready for review at
closeout; and may merge dispatch branches into its own working branch. It never merges to `main`
unless the user stated so explicitly, in the approval or in the conversation — absent that
sentence, the run stops at a ready PR.

Whether this run may merge to `main` is the user's decision to make, not the run's to rule on.
Their most recent explicit statement governs over any earlier default or preset. Report blockers
plainly, then carry out the authorized action; a defect exit pauses and reports, and is not a way
to decline an authority decision. A defect must concern the artifact it names — a scope objection
raised to refuse an authority call disguises the real disagreement. When the harness itself blocks
an action, ask the user rather than rephrasing past the guard. Full statement: spec §9.1, §9.2.

It stops for exactly two things: an external send, and a user-owned decision the plan did not
anticipate. Everything the plan named — production applies included — it executes without asking
again.
